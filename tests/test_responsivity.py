from __future__ import annotations

import json
import math
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from photon_fab.analytics import (
    CONVERSION_VERSION,
    LEGACY_CONVERSION_VERSION,
    responsivity,
    responsivity_aw,
)
from photon_fab.api import Handler
from photon_fab.service import PhotonService


class ResponsivityNumericTests(unittest.TestCase):
    def test_milliwatt_basis(self) -> None:
        result = responsivity_aw(1.0, 2.0, "mw")
        self.assertAlmostEqual(result.responsivity_aw, 0.5)
        self.assertEqual(result.power_unit, "mw")
        self.assertEqual(result.result_unit, "A/W")
        self.assertEqual(result.conversion_version, CONVERSION_VERSION)
        self.assertFalse(result.legacy)

    def test_microwatt_and_milliwatt_same_physical_power_agree(self) -> None:
        in_mw = responsivity_aw(1.0, 2.0, "mw")
        in_uw = responsivity_aw(1.0, 2000.0, "uw")
        self.assertAlmostEqual(in_mw.responsivity_aw, in_uw.responsivity_aw)
        self.assertEqual(in_uw.optical_power_unit_label, "µW")

    def test_microwatt_was_1000x_bug(self) -> None:
        # 1 mA / 1000 µW 物理上等于 1 A/W；旧接口按 mW 解读只会得到 0.001 A/W。
        fixed = responsivity_aw(1.0, 1000.0, "uw")
        self.assertAlmostEqual(fixed.responsivity_aw, 1.0)
        legacy = responsivity_aw(1.0, 1000.0, "uw", LEGACY_CONVERSION_VERSION)
        self.assertAlmostEqual(legacy.responsivity_aw, 0.001)
        self.assertTrue(legacy.legacy)
        self.assertAlmostEqual(fixed.responsivity_aw / legacy.responsivity_aw, 1000.0)

    def test_legacy_float_wrapper_still_mw(self) -> None:
        self.assertAlmostEqual(responsivity(1.0, 2.0), 0.5)

    def test_zero_power_rejected_no_infinity(self) -> None:
        with self.assertRaises(ValueError):
            responsivity_aw(1.0, 0.0, "uw")
        with self.assertRaises(ValueError):
            responsivity_aw(1.0, 0.0, "mw")

    def test_negative_power_rejected(self) -> None:
        with self.assertRaises(ValueError):
            responsivity_aw(1.0, -100.0, "uw")
        with self.assertRaises(ValueError):
            responsivity_aw(1.0, -0.1, "mw")

    def test_negative_current_rejected(self) -> None:
        with self.assertRaises(ValueError):
            responsivity_aw(-1.0, 1.0, "mw")

    def test_non_finite_inputs_rejected(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                responsivity_aw(bad, 1.0, "mw")
            with self.assertRaises(ValueError):
                responsivity_aw(1.0, bad, "mw")
            self.assertFalse(math.isfinite(bad) if not math.isnan(bad) else False)

    def test_unit_must_be_declared(self) -> None:
        with self.assertRaises(ValueError):
            responsivity_aw(1.0, 1.0, "")
        with self.assertRaises(ValueError):
            responsivity_aw(1.0, 1.0, "W")

    def test_unknown_conversion_version_rejected(self) -> None:
        with self.assertRaises(ValueError):
            responsivity_aw(1.0, 1.0, "mw", "future-v9")


class ResponsivityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "photon-admin")
        self.service.create_lot(self.token, "LOT-R", "PD array", "P1.0", 4)

    def test_record_freezes_units_and_version(self) -> None:
        record = self.service.add_responsivity_measurement(
            self.token, "LOT-R", 1.0, 1000.0, "uw", "probe-1"
        )
        self.assertAlmostEqual(record["responsivity_aw"], 1.0)
        self.assertEqual(record["power_unit"], "uw")
        self.assertEqual(record["result_unit"], "A/W")
        self.assertEqual(record["conversion_version"], CONVERSION_VERSION)
        stored = self.service.list_responsivity_measurements(self.token, "LOT-R")
        self.assertEqual(len(stored["records"]), 1)
        self.assertEqual(stored["records"][0]["photocurrent_unit"], "mA")

    def test_reanalysis_does_not_rewrite_records(self) -> None:
        self.service.add_responsivity_measurement(
            self.token, "LOT-R", 1.0, 1000.0, "uw", "probe-1"
        )
        report = self.service.reanalyze_responsivity(
            self.token, "LOT-R", LEGACY_CONVERSION_VERSION
        )
        self.assertEqual(report["records_examined"], 1)
        self.assertEqual(report["records_changed"], 1)
        self.assertFalse(report["records_modified"])
        item = report["items"][0]
        self.assertAlmostEqual(item["frozen"]["responsivity_aw"], 1.0)
        self.assertAlmostEqual(item["reanalyzed"]["responsivity_aw"], 0.001)
        self.assertAlmostEqual(item["ratio_reanalyzed_to_frozen"], 0.001)
        self.assertEqual(item["frozen"]["conversion_version"], CONVERSION_VERSION)
        # 存储中的原始记录保持不变。
        rows = self.service.db.execute(
            "SELECT power_unit,conversion_version,responsivity_aw FROM responsivity_measurements"
        ).fetchall()
        self.assertEqual(rows[0]["power_unit"], "uw")
        self.assertEqual(rows[0]["conversion_version"], CONVERSION_VERSION)
        self.assertAlmostEqual(rows[0]["responsivity_aw"], 1.0)

    def test_reanalyze_same_version_marks_unchanged(self) -> None:
        self.service.add_responsivity_measurement(
            self.token, "LOT-R", 1.0, 2.0, "mw", "probe-1"
        )
        report = self.service.reanalyze_responsivity(self.token, "LOT-R")
        self.assertEqual(report["records_changed"], 0)
        self.assertFalse(report["items"][0]["changed"])


class ResponsivityHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        Handler.service = PhotonService()
        Handler.service.bootstrap_admin()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/login",
            data=json.dumps({"user_id": "admin", "password": "photon-admin"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            self.token = json.loads(resp.read())["token"]
        self._post("/lots", {"lot_id": "LOT-H", "product": "PD", "process_rev": "P1", "wafer_count": 2})

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, method: str, path: str, body: dict | None, expected: int) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, expected)
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, expected)
            return json.loads(exc.read())

    def _post(self, path: str, body: dict, expected: int = 201) -> dict:
        return self._request("POST", path, body, expected)

    def _get(self, path: str, expected: int = 200) -> dict:
        return self._request("GET", path, None, expected)

    def test_units_declaration(self) -> None:
        body = self._get("/responsivity/units")
        self.assertEqual(body["version"], CONVERSION_VERSION)
        self.assertEqual(body["uw_per_mw"], 1000)
        self.assertEqual(body["optical_power"]["uw"]["si_unit"], "W")

    def test_microwatt_and_milliwatt_scenarios(self) -> None:
        mw = self._post("/lots/LOT-H/responsivity", {
            "photocurrent_ma": 1.0, "optical_power": 2.0, "power_unit": "mw", "instrument": "p",
        })
        self.assertAlmostEqual(mw["responsivity_aw"], 0.5)
        uw = self._post("/lots/LOT-H/responsivity", {
            "photocurrent_ma": 1.0, "optical_power": 2000.0, "power_unit": "uw", "instrument": "p",
        })
        self.assertAlmostEqual(uw["responsivity_aw"], 0.5)
        self.assertEqual(uw["conversion_version"], CONVERSION_VERSION)
        listing = self._get("/lots/LOT-H/responsivity")
        self.assertEqual(len(listing["records"]), 2)

    def test_zero_power_is_400(self) -> None:
        body = self._post("/lots/LOT-H/responsivity", {
            "photocurrent_ma": 1.0, "optical_power": 0.0, "power_unit": "uw", "instrument": "p",
        }, expected=400)
        self.assertIn("optical power", body["error"])

    def test_negative_power_is_400(self) -> None:
        body = self._post("/lots/LOT-H/responsivity", {
            "photocurrent_ma": 1.0, "optical_power": -5.0, "power_unit": "mw", "instrument": "p",
        }, expected=400)
        self.assertIn("optical power", body["error"])

    def test_missing_unit_is_400(self) -> None:
        body = self._post("/lots/LOT-H/responsivity", {
            "photocurrent_ma": 1.0, "optical_power": 1.0, "instrument": "p",
        }, expected=400)
        self.assertIn("power_unit", body["error"])

    def test_reanalyze_endpoint_preserves_records(self) -> None:
        self._post("/lots/LOT-H/responsivity", {
            "photocurrent_ma": 1.0, "optical_power": 1000.0, "power_unit": "uw", "instrument": "p",
        })
        report = self._request("POST", "/lots/LOT-H/responsivity/reanalyze",
                               {"conversion_version": LEGACY_CONVERSION_VERSION}, 200)
        self.assertEqual(report["records_changed"], 1)
        self.assertFalse(report["records_modified"])
        self.assertAlmostEqual(report["items"][0]["frozen"]["responsivity_aw"], 1.0)
        self.assertAlmostEqual(report["items"][0]["reanalyzed"]["responsivity_aw"], 0.001)


if __name__ == "__main__":
    unittest.main()
