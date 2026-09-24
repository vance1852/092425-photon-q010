from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from photon_fab.service import PhotonService
from photon_fab.storage import connect


class ReanalysisImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "photon-admin")
        self.service.create_lot(self.token, "LOT-1", "PD", "R1", 6)
        for wavelength, response in ((450, .71), (520, .93), (650, .84)):
            self.service.add_measurement(
                self.token, "LOT-1", wavelength, response, .01, "bench-1",
                photocurrent=400, optical_power=500, current_unit="ua", power_unit="uw",
            )

    def test_repeated_analysis_replays_same_run(self) -> None:
        first = self.service.analyze(self.token, "LOT-1")
        second = self.service.analyze(self.token, "LOT-1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["responsivity"], first["responsivity"])
        rows = self.service.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()
        self.assertEqual(rows[0], 1)

    def test_new_measurement_creates_new_run_without_touching_old_one(self) -> None:
        first = self.service.analyze(self.token, "LOT-1")
        before = self.service.db.execute(
            "SELECT result_json,created_at FROM analysis_runs WHERE run_id=?", (first["run_id"],),
        ).fetchone()
        measurement_rows_before = [
            tuple(r) for r in self.service.db.execute(
                "SELECT * FROM measurements ORDER BY measurement_id").fetchall()
        ]

        self.service.add_measurement(
            self.token, "LOT-1", 700, .6, .02, "bench-1",
            photocurrent=300, optical_power=500, current_unit="ua", power_unit="uw",
        )
        second = self.service.analyze(self.token, "LOT-1")
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertFalse(second["replayed"])

        after = self.service.db.execute(
            "SELECT result_json,created_at FROM analysis_runs WHERE run_id=?", (first["run_id"],),
        ).fetchone()
        # 历史分析快照原样保留。
        self.assertEqual(tuple(before), tuple(after))
        # 旧测量行本身没有被改写：前三行逐字节一致，只新增了一行。
        measurement_rows_after = [
            tuple(r) for r in self.service.db.execute(
                "SELECT * FROM measurements ORDER BY measurement_id").fetchall()
        ]
        self.assertEqual(len(measurement_rows_after), len(measurement_rows_before) + 1)
        self.assertTrue(
            set(map(tuple, measurement_rows_before))
            <= set(map(tuple, measurement_rows_after))
        )
        runs = self.service.db.execute("SELECT run_id FROM analysis_runs ORDER BY created_at").fetchall()
        self.assertEqual([r[0] for r in runs], [first["run_id"], second["run_id"]])

    def test_legacy_rows_without_units_are_flagged_not_silently_mw(self) -> None:
        # 直接构造一条修复前格式的旧测量行（单位列为 NULL）。
        self.service.db.execute(
            "INSERT INTO measurements(measurement_id,lot_id,wavelength_nm,response,noise,"
            "instrument,operator,measured_at) VALUES(?,?,?,?,?,?,?,?)",
            ("legacy-1", "LOT-1", 800, 0.5, 0.01, "old-bench", "admin", "2026-01-01T00:00:00+00:00"),
        )
        self.service.db.commit()
        result = self.service.analyze(self.token, "LOT-1")
        self.assertTrue(result["units"]["legacy_measurements_without_units"])
        excluded = {e["measurement_id"]: e["reason"] for e in result["responsivity_excluded"]}
        self.assertEqual(excluded.get("legacy-1"), "optical_power_unit_undeclared")
        # 合法微瓦记录仍按正确换算计入。
        self.assertEqual(len(result["responsivity"]), 3)
        for item in result["responsivity"]:
            self.assertAlmostEqual(item["responsivity"], 0.8)
            self.assertEqual(item["conversion_version"], "unit-conversion-v1")


class StorageMigrationTests(unittest.TestCase):
    def test_legacy_database_file_keeps_unit_columns_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            # 用修复前的旧结构建库并写入数据。
            legacy = sqlite3.connect(path)
            legacy.execute(
                "CREATE TABLE chip_lots(lot_id TEXT PRIMARY KEY, product TEXT NOT NULL,"
                " process_rev TEXT NOT NULL, wafer_count INTEGER NOT NULL, status TEXT NOT NULL,"
                " owner TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            legacy.execute(
                "CREATE TABLE measurements(measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL,"
                " wavelength_nm REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,"
                " instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL)")
            legacy.execute(
                "INSERT INTO measurements VALUES('m1','LOT-1',520,0.9,0.01,'old','admin','t')")
            legacy.commit()
            legacy.close()

            db = connect(str(path))
            try:
                row = db.execute("SELECT * FROM measurements WHERE measurement_id='m1'").fetchone()
                self.assertIsNone(row["optical_power"])
                self.assertIsNone(row["optical_power_unit"])
                self.assertIsNone(row["conversion_version"])
                # 原始测量值未被迁移逻辑改写。
                self.assertEqual(row["wavelength_nm"], 520)
                self.assertEqual(row["response"], 0.9)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
