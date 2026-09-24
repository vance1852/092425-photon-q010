"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from typing import Sequence

from .analytics import confidence_interval, responsivity, summarize_spectrum, yield_rate
from .auth import Auth
from .storage import connect, event, transaction, utcnow
from .units import CONVERSION_VERSION, MILLIAMP, MILLIWATT, normalize_unit


from .units import (
    CONVERSION_VERSION,
    CURRENT_UNITS,
    MILLIAMP,
    MILLIWATT,
    POWER_UNITS,
    current_to_ma,
    normalize_unit,
    power_to_mw,
)

# 分析算法版本：分析口径变化时升版，并参与快照指纹，
# 这样旧批次的历史分析结果永远保留、不会被新算法静默覆盖。
ANALYSIS_VERSION = "photon-analysis-v1"


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, wafer_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now))
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def add_measurement(
        self,
        token: str,
        lot_id: str,
        wavelength_nm: float,
        response: float,
        noise: float,
        instrument: str,
        photocurrent: float | None = None,
        optical_power: float | None = None,
        current_unit: str = MILLIAMP,
        power_unit: str = MILLIWATT,
    ) -> dict:
        actor = self.auth.require(token, "measure")
        if (photocurrent is None) != (optical_power is None):
            raise ValueError("photocurrent and optical_power must be provided together")
        current_unit_n = power_unit_n = None
        if photocurrent is not None:
            current_unit_n = normalize_unit(current_unit)
            power_unit_n = normalize_unit(power_unit)
            if current_unit_n not in CURRENT_UNITS or power_unit_n not in POWER_UNITS:
                raise ValueError("photocurrent or optical power uses an unsupported unit")
            # 入库前换算一次仅用于校验数值与单位；库里保留原始值和单位声明。
            current_to_ma(photocurrent, current_unit_n)
            power_to_mw(optical_power, power_unit_n)
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute(
                "INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    measurement_id, lot_id, float(wavelength_nm), float(response), float(noise),
                    instrument, actor.user_id, utcnow(),
                    None if photocurrent is None else float(photocurrent), current_unit_n,
                    None if optical_power is None else float(optical_power), power_unit_n,
                    CONVERSION_VERSION,
                ),
            )
            event(self.db, lot_id, "measurement", actor.user_id, {
                "measurement_id": measurement_id,
                "wavelength_nm": wavelength_nm,
                "photocurrent_unit": current_unit_n,
                "optical_power_unit": power_unit_n,
                "conversion_version": CONVERSION_VERSION,
            })
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def compute_responsivity(
        self,
        token: str,
        photocurrent: float,
        optical_power: float,
        current_unit: str = MILLIAMP,
        power_unit: str = MILLIWATT,
    ) -> dict:
        """单次响应度计算入口；单位必填，零/负功率在换算后被拒绝。"""
        self.auth.require(token, "analyze")
        return asdict(responsivity(photocurrent, optical_power, current_unit, power_unit))

    def _responsivity_breakdown(self, rows: Sequence) -> tuple[list[dict], list[dict], bool]:
        values: list[dict] = []
        excluded: list[dict] = []
        legacy = False
        for row in rows:
            if row["conversion_version"] is None:
                # 修复前写入的旧记录：单位未声明，绝不静默按毫瓦解读，
                # 也不改写该行，只在本次分析结果中显式标识并排除。
                legacy = True
                excluded.append({
                    "measurement_id": row["measurement_id"],
                    "reason": "optical_power_unit_undeclared",
                })
                continue
            if row["photocurrent"] is None or row["optical_power"] is None:
                continue  # 新记录本就没有光电流/功率：不参与响应度，不属于旧数据。
            units = (row["photocurrent_unit"], row["optical_power_unit"])
            if None in units:
                legacy = True
                excluded.append({
                    "measurement_id": row["measurement_id"],
                    "reason": "optical_power_unit_undeclared",
                })
                continue
            try:
                result = asdict(responsivity(
                    row["photocurrent"], row["optical_power"], units[0], units[1],
                ))
            except ValueError as exc:
                # 零功率、负功率、非有限值在此被隔离，绝不产生 inf/NaN。
                excluded.append({"measurement_id": row["measurement_id"], "reason": str(exc)})
            else:
                result["measurement_id"] = row["measurement_id"]
                values.append(result)
        return values, excluded, legacy

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        rows = self.db.execute(
            "SELECT * FROM measurements WHERE lot_id=? ORDER BY wavelength_nm,measurement_id",
            (lot_id,),
        ).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        responsivities, excluded, legacy = self._responsivity_breakdown(rows)
        summary = summarize_spectrum([r["wavelength_nm"] for r in rows], [r["response"] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["wafer_count"], sum(1 for r in rows if r["response"] >= 0.8), 0)
        ci = confidence_interval([r["response"] for r in rows])
        result = {
            "lot_id": lot_id,
            "spectrum": summary.__dict__,
            "yield": rates,
            "response_ci": ci,
            "responsivity": responsivities,
            "responsivity_excluded": excluded,
            "units": {
                "analysis_version": ANALYSIS_VERSION,
                "conversion_version": CONVERSION_VERSION,
                "responsivity_unit": "mA/mW",
                "legacy_measurements_without_units": legacy,
            },
        }
        fingerprint = hashlib.sha256(json.dumps({
            "lot_id": lot_id,
            "analysis_version": ANALYSIS_VERSION,
            "conversion_version": CONVERSION_VERSION,
            "measurements": [
                [r["measurement_id"], r["wavelength_nm"], r["response"], r["noise"],
                 r["photocurrent"], r["photocurrent_unit"], r["optical_power"],
                 r["optical_power_unit"], r["conversion_version"]]
                for r in rows
            ],
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        with transaction(self.db):
            existing = self.db.execute(
                "SELECT run_id,result_json FROM analysis_runs WHERE input_fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if existing:
                # 输入与算法版本完全一致：重放历史快照，绝不 UPDATE 或覆盖。
                stored = json.loads(existing["result_json"])
                stored["run_id"] = existing["run_id"]
                stored["replayed"] = True
                return stored
            run_id = uuid.uuid4().hex
            result["run_id"] = run_id
            result["replayed"] = False
            self.db.execute(
                "INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?)",
                (run_id, lot_id, CONVERSION_VERSION, fingerprint,
                 json.dumps(result, sort_keys=True, ensure_ascii=False),
                 self.auth.current(token).user_id, utcnow()),
            )
            event(self.db, lot_id, "analysis", self.auth.current(token).user_id, {
                "run_id": run_id,
                "analysis_version": ANALYSIS_VERSION,
                "conversion_version": CONVERSION_VERSION,
                "fingerprint": fingerprint,
            })
        return result

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
