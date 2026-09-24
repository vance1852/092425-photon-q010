from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from photon_fab.api import Handler
from photon_fab.service import PhotonService


class _Server:
    def __init__(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        Handler.service = self.service
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.token = self.post("/login", {"user_id": "admin", "password": "photon-admin"})["token"]

    def post(self, path: str, body: dict, token: str | None = None) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                self.status = response.status
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            self.status = exc.code
            return json.loads(exc.read())

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


class ResponsivityHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _Server()
        self.post = lambda body: self.server.post("/responsivity", body, self.server.token)

    def tearDown(self) -> None:
        self.server.stop()

    def test_microwatt_power_is_scaled_by_one_thousand(self) -> None:
        body = self.post({"photocurrent": 0.4, "optical_power": 500, "current_unit": "ma", "power_unit": "uw"})
        self.assertEqual(self.server.status, 200)
        self.assertAlmostEqual(body["responsivity"], 0.8)
        self.assertEqual(body["result_unit"], "mA/mW")
        self.assertEqual(body["power_unit"], "uw")
        self.assertAlmostEqual(body["power_mw"], 0.5)
        self.assertTrue(body["conversion_version"].startswith("unit-conversion-"))

    def test_milliwatt_power(self) -> None:
        body = self.post({"photocurrent": 0.4, "optical_power": 0.5, "power_unit": "mw"})
        self.assertEqual(self.server.status, 200)
        self.assertAlmostEqual(body["responsivity"], 0.8)

    def test_micro_units_pair(self) -> None:
        body = self.post({"photocurrent": 400, "optical_power": 500,
                          "current_unit": "ua", "power_unit": "uw"})
        self.assertEqual(self.server.status, 200)
        self.assertAlmostEqual(body["responsivity"], 0.8)

    def test_zero_power_returns_400_not_infinity(self) -> None:
        body = self.post({"photocurrent": 0.4, "optical_power": 0, "power_unit": "mw"})
        self.assertEqual(self.server.status, 400)
        self.assertNotIn("Infinity", json.dumps(body))
        body_u = self.post({"photocurrent": 400, "optical_power": 0, "power_unit": "uw"})
        self.assertEqual(self.server.status, 400)
        self.assertNotIn("Infinity", json.dumps(body_u))

    def test_negative_power_returns_400(self) -> None:
        body = self.post({"photocurrent": 0.4, "optical_power": -500, "power_unit": "uw"})
        self.assertEqual(self.server.status, 400)
        self.assertNotIn("Infinity", json.dumps(body))

    def test_measurement_with_microwatt_unit_flows_into_analysis(self) -> None:
        self.server.post("/lots", {
            "lot_id": "LOT-UW", "product": "PD", "process_rev": "R1", "wafer_count": 4,
        }, self.server.token)
        for wavelength, response, current, power in (
            (450, 0.71, 400, 500),
            (520, 0.93, 460, 500),
            (650, 0.84, 420, 500),
        ):
            result = self.server.post("/lots/LOT-UW/measurements", {
                "wavelength_nm": wavelength, "response": response, "instrument": "pd-bench",
                "photocurrent": current, "optical_power": power,
                "current_unit": "ua", "power_unit": "uw",
            }, self.server.token)
            self.assertEqual(self.server.status, 201, result)
        analysis = self.server.post("/lots/LOT-UW/analysis", {}, self.server.token)
        self.assertEqual(self.server.status, 200)
        self.assertEqual(len(analysis["responsivity"]), 3)
        self.assertAlmostEqual(analysis["responsivity"][0]["responsivity"], 0.8)
        self.assertEqual(analysis["units"]["responsivity_unit"], "mA/mW")
        self.assertTrue(analysis["units"]["conversion_version"].startswith("unit-conversion-"))
        self.assertFalse(analysis["units"]["legacy_measurements_without_units"])

    def test_zero_power_measurement_is_excluded_from_analysis(self) -> None:
        self.server.post("/lots", {
            "lot_id": "LOT-ZERO", "product": "PD", "process_rev": "R1", "wafer_count": 4,
        }, self.server.token)
        for wavelength, response, power in ((450, 0.7, 500), (520, 0.9, 0), (650, 0.8, -10)):
            self.server.post("/lots/LOT-ZERO/measurements", {
                "wavelength_nm": wavelength, "response": response, "instrument": "pd-bench",
                "photocurrent": 400, "optical_power": power,
                "current_unit": "ua", "power_unit": "uw",
            }, self.server.token)
        analysis = self.server.post("/lots/LOT-ZERO/analysis", {}, self.server.token)
        self.assertEqual(len(analysis["responsivity"]), 1)
        self.assertEqual(len(analysis["responsivity_excluded"]), 2)
        payload = json.dumps(analysis)
        self.assertNotIn("Infinity", payload)
        self.assertNotIn("NaN", payload)


if __name__ == "__main__":
    unittest.main()
