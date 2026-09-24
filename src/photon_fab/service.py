"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import math
import uuid
from typing import Sequence

from .analytics import (
    CONVERSION_VERSION,
    LEGACY_CONVERSION_VERSION,
    POWER_UNITS,
    UNIT_CONVERSION_DECLARATION,
    confidence_interval,
    responsivity_aw,
    summarize_spectrum,
    yield_rate,
)
from .auth import Auth
from .storage import connect, event, transaction, utcnow


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

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    @staticmethod
    def _validate_power_unit(power_unit: object) -> str:
        if not isinstance(power_unit, str) or power_unit.lower() not in POWER_UNITS:
            raise ValueError("power_unit is required and must be 'uw' (microwatt) or 'mw' (milliwatt)")
        return power_unit.lower()

    def _require_lot(self, lot_id: str) -> None:
        if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
            raise KeyError(lot_id)

    def responsivity_units(self, token: str) -> dict:
        """返回当前单位声明与换算规则版本，供调用方在请求中显式声明单位。"""
        self.auth.require(token, "read")
        return dict(UNIT_CONVERSION_DECLARATION)

    def add_responsivity_measurement(
        self,
        token: str,
        lot_id: str,
        photocurrent_ma: float,
        optical_power: float,
        power_unit: str,
        instrument: str,
        conversion_version: str = CONVERSION_VERSION,
    ) -> dict:
        actor = self.auth.require(token, "measure")
        unit = self._validate_power_unit(power_unit)
        # 计算同时完成全部边界校验（有限性、零/负功率、未知版本）。
        result = responsivity_aw(photocurrent_ma, optical_power, unit, conversion_version)
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            self._require_lot(lot_id)
            self.db.execute(
                "INSERT INTO responsivity_measurements VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    measurement_id, lot_id, result.photocurrent_ma, result.optical_power,
                    result.power_unit, result.conversion_version, result.responsivity_aw,
                    instrument, actor.user_id, utcnow(),
                ),
            )
            event(
                self.db, lot_id, "responsivity_measurement", actor.user_id,
                {
                    "measurement_id": measurement_id,
                    "photocurrent_ma": result.photocurrent_ma,
                    "optical_power": result.optical_power,
                    "power_unit": result.power_unit,
                    "conversion_version": result.conversion_version,
                    "responsivity_aw": result.responsivity_aw,
                },
            )
        return self._responsivity_record(
            measurement_id, lot_id, result.photocurrent_ma, result.optical_power,
            result.power_unit, result.conversion_version, result.responsivity_aw,
            instrument, actor.user_id,
        )

    @staticmethod
    def _responsivity_record(
        measurement_id: str, lot_id: str, photocurrent_ma: float, optical_power: float,
        power_unit: str, conversion_version: str, responsivity_aw_value: float,
        instrument: str, operator: str,
    ) -> dict:
        return {
            "measurement_id": measurement_id,
            "lot_id": lot_id,
            "photocurrent_ma": photocurrent_ma,
            "photocurrent_unit": "mA",
            "optical_power": optical_power,
            "power_unit": power_unit,
            "responsivity_aw": responsivity_aw_value,
            "result_unit": "A/W",
            "conversion_version": conversion_version,
            "instrument": instrument,
            "operator": operator,
        }

    def list_responsivity_measurements(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        self._require_lot(lot_id)
        rows = self.db.execute(
            "SELECT measurement_id,lot_id,photocurrent_ma,optical_power,power_unit,"
            "conversion_version,responsivity_aw,instrument,operator "
            "FROM responsivity_measurements WHERE lot_id=? ORDER BY measured_at,measurement_id",
            (lot_id,),
        ).fetchall()
        return {
            "lot_id": lot_id,
            "conversion_version": CONVERSION_VERSION,
            "records": [
                self._responsivity_record(
                    r["measurement_id"], r["lot_id"], r["photocurrent_ma"], r["optical_power"],
                    r["power_unit"], r["conversion_version"], r["responsivity_aw"],
                    r["instrument"], r["operator"],
                )
                for r in rows
            ],
        }

    def reanalyze_responsivity(
        self, token: str, lot_id: str, conversion_version: str = CONVERSION_VERSION
    ) -> dict:
        """以只读方式按指定换算版本重新计算批次的响应度记录。

        重要：原始测量记录（数值、提交时声明的单位、固化的换算版本、当时算出的
        响应度）永远不会被修改或覆盖；重新分析结果与冻结的历史值并列返回，
        差异逐条标注，旧记录不会被静默改写。
        """
        self.auth.require(token, "analyze")
        self._require_lot(lot_id)
        if conversion_version not in {CONVERSION_VERSION, LEGACY_CONVERSION_VERSION}:
            raise ValueError(
                f"conversion_version must be {CONVERSION_VERSION!r} or {LEGACY_CONVERSION_VERSION!r}"
            )
        rows = self.db.execute(
            "SELECT measurement_id,photocurrent_ma,optical_power,power_unit,"
            "conversion_version,responsivity_aw FROM responsivity_measurements "
            "WHERE lot_id=? ORDER BY measured_at,measurement_id",
            (lot_id,),
        ).fetchall()
        items = []
        changed = 0
        for r in rows:
            recomputed = responsivity_aw(
                r["photocurrent_ma"], r["optical_power"], r["power_unit"], conversion_version
            )
            differs = not math.isclose(
                r["responsivity_aw"], recomputed.responsivity_aw, rel_tol=1e-12, abs_tol=0.0
            )
            changed += int(differs)
            items.append({
                "measurement_id": r["measurement_id"],
                "photocurrent_ma": r["photocurrent_ma"],
                "optical_power": r["optical_power"],
                "power_unit": r["power_unit"],
                "frozen": {
                    "conversion_version": r["conversion_version"],
                    "responsivity_aw": r["responsivity_aw"],
                    "result_unit": "A/W",
                },
                "reanalyzed": {
                    "conversion_version": conversion_version,
                    "responsivity_aw": recomputed.responsivity_aw,
                    "result_unit": "A/W",
                },
                "changed": differs,
                "ratio_reanalyzed_to_frozen": (
                    recomputed.responsivity_aw / r["responsivity_aw"]
                    if r["responsivity_aw"] != 0.0
                    else None
                ),
            })
        return {
            "lot_id": lot_id,
            "records_examined": len(items),
            "records_changed": changed,
            "requested_conversion_version": conversion_version,
            "current_conversion_version": CONVERSION_VERSION,
            "records_modified": False,
            "units": {
                "photocurrent": "mA",
                "optical_power": "declared per record ('uw' or 'mw')",
                "responsivity": "A/W",
                "uw_per_mw": 1000,
            },
            "items": items,
        }

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        rows = self.db.execute("SELECT wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY wavelength_nm", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["wafer_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "spectrum": summary.__dict__, "yield": rates, "response_ci": ci}

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
