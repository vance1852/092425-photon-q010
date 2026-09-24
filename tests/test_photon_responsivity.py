from __future__ import annotations

import json
import math
import unittest

from photon_fab.analytics import responsivity
from photon_fab.units import CONVERSION_VERSION


class ResponsivityUnitTests(unittest.TestCase):
    def test_milliwatt_input(self) -> None:
        result = responsivity(0.4, 0.5, "ma", "mw")
        self.assertAlmostEqual(result.responsivity, 0.8)
        self.assertEqual(result.result_unit, "mA/mW")
        self.assertEqual(result.power_unit, "mw")
        self.assertEqual(result.conversion_version, CONVERSION_VERSION)

    def test_microwatt_input_is_converted_to_milliwatt(self) -> None:
        # 500 uW = 0.5 mW：若按毫瓦直算会得到 0.0008（小一千倍）。
        result = responsivity(0.4, 500, "ma", "uw")
        self.assertAlmostEqual(result.responsivity, 0.8)
        self.assertAlmostEqual(result.power_mw, 0.5)
        self.assertEqual(result.power_unit, "uw")

    def test_micro_current_and_micro_power_pair(self) -> None:
        # 400 uA / 500 uW 与 0.4 mA / 0.5 mW 等价。
        result = responsivity(400, 500, "ua", "uw")
        self.assertAlmostEqual(result.responsivity, 0.8)
        self.assertAlmostEqual(result.current_ma, 0.4)

    def test_unicode_micro_prefix_is_normalized(self) -> None:
        self.assertAlmostEqual(responsivity(0.4, 500, "mA", "µW").responsivity, 0.8)
        self.assertAlmostEqual(responsivity(0.4, 500, "mA", "μW").responsivity, 0.8)

    def test_zero_power_is_rejected_in_every_unit(self) -> None:
        for unit in ("mw", "uw"):
            with self.subTest(unit=unit):
                with self.assertRaises(ValueError):
                    responsivity(0.4, 0, "ma", unit)

    def test_negative_power_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            responsivity(0.4, -1.0, "ma", "mw")
        with self.assertRaises(ValueError):
            responsivity(0.4, -100, "ma", "uw")

    def test_non_finite_inputs_are_rejected(self) -> None:
        for power in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(power=power):
                with self.assertRaises(ValueError):
                    responsivity(0.4, power, "ma", "mw")
        with self.assertRaises(ValueError):
            responsivity(float("inf"), 1.0, "ma", "mw")

    def test_unsupported_unit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            responsivity(0.4, 500, "ma", "w")
        with self.assertRaises(ValueError):
            responsivity(0.4, 0.5, "a", "mw")

    def test_result_is_always_finite(self) -> None:
        result = responsivity(1e-300, 1e300, "ma", "mw")
        self.assertTrue(math.isfinite(result.responsivity))


if __name__ == "__main__":
    unittest.main()
