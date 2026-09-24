"""光谱和良率测量的确定性科学计算。"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class SpectrumSummary:
    count: int
    peak_wavelength_nm: float
    peak_response: float
    mean_response: float
    noise_rms: float
    pass_band_nm: tuple[float, float]


def _pairs(wavelengths: Sequence[float], response: Sequence[float]) -> list[tuple[float, float]]:
    if len(wavelengths) != len(response) or len(wavelengths) < 3:
        raise ValueError("at least three wavelength/response pairs are required")
    pairs = sorted((float(w), float(r)) for w, r in zip(wavelengths, response))
    if any(not math.isfinite(w) or not math.isfinite(r) for w, r in pairs):
        raise ValueError("measurements must be finite")
    return pairs


def summarize_spectrum(wavelengths: Sequence[float], response: Sequence[float], threshold: float = 0.8) -> SpectrumSummary:
    pairs = _pairs(wavelengths, response)
    peak_w, peak_r = max(pairs, key=lambda p: p[1])
    values = [r for _, r in pairs]
    mean = statistics.fmean(values)
    noise = math.sqrt(statistics.fmean((r - mean) ** 2 for r in values))
    band = [w for w, r in pairs if r >= peak_r * threshold]
    return SpectrumSummary(len(pairs), peak_w, peak_r, mean, noise, (min(band), max(band)))


def confidence_interval(values: Iterable[float], confidence: float = 0.95) -> tuple[float, float]:
    data = [float(v) for v in values]
    if not data or not 0 < confidence < 1:
        raise ValueError("values and confidence are invalid")
    mean = statistics.fmean(data)
    if len(data) == 1:
        return mean, mean
    z = 1.96 if confidence >= 0.95 else 1.645
    margin = z * statistics.stdev(data) / math.sqrt(len(data))
    return mean - margin, mean + margin


def yield_rate(total: int, passed: int, rejected: int = 0) -> dict[str, float]:
    if total <= 0 or passed < 0 or rejected < 0 or passed + rejected > total:
        raise ValueError("inconsistent lot counts")
    return {"yield": passed / total, "reject_rate": rejected / total, "unknown_rate": (total - passed - rejected) / total}


# --- 响应度：单位声明、版本化换算与边界校验 ---------------------------------
# 光电流输入单位固定为 mA；光功率单位必须由调用方显式声明为 "uw"（微瓦）
# 或 "mw"（毫瓦）。1 mW = 1000 µW，结果统一以 SI 单位 A/W 给出。
MILLIAMP_TO_AMP = 1e-3
MICROWATT_TO_WATT = 1e-6
MILLIWATT_TO_WATT = 1e-3
POWER_UNITS: dict[str, float] = {"uw": MICROWATT_TO_WATT, "mw": MILLIWATT_TO_WATT}
POWER_UNIT_LABELS: dict[str, str] = {"uw": "µW", "mw": "mW"}

# 当前换算版本。任何换算规则变更都必须引入新版本号，不能就地修改。
CONVERSION_VERSION = "photon-power-units-v1"
# 历史版本：接口把工程师提交的光功率数值一律按 mW 解释（µW 提交即产生
# 1000 倍偏差）。仅用于旧测量记录的复算，保证历史结果不被静默改写。
LEGACY_CONVERSION_VERSION = "legacy-mw-v0"

UNIT_CONVERSION_DECLARATION = {
    "version": CONVERSION_VERSION,
    "photocurrent": {"input_unit": "mA", "si_unit": "A", "to_si_factor": MILLIAMP_TO_AMP},
    "optical_power": {
        "uw": {"label": "µW", "si_unit": "W", "to_si_factor": MICROWATT_TO_WATT},
        "mw": {"label": "mW", "si_unit": "W", "to_si_factor": MILLIWATT_TO_WATT},
    },
    "uw_per_mw": 1000,
    "mw_per_uw": 0.001,
}


@dataclass(frozen=True)
class ResponsivityResult:
    responsivity_aw: float
    photocurrent_ma: float
    optical_power: float
    power_unit: str
    photocurrent_unit: str = "mA"
    optical_power_unit_label: str = ""
    result_unit: str = "A/W"
    conversion_version: str = CONVERSION_VERSION
    legacy: bool = False


def _finite(value: float, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a real number") from exc
    if not math.isfinite(number):
        # 显式拦截 NaN / +Inf / -Inf，避免它们经由比较或除法污染结果。
        raise ValueError(f"{label} must be finite")
    return number


def responsivity_aw(
    current_ma: float,
    optical_power: float,
    power_unit: str,
    conversion_version: str = CONVERSION_VERSION,
) -> ResponsivityResult:
    """计算光电探测器响应度 R = I / P，结果单位为 A/W。

    ``power_unit`` 必须显式声明为 ``"uw"``（微瓦）或 ``"mw"``（毫瓦），
    换算按 ``conversion_version`` 指定的版本执行。零功率、负功率以及
    非有限输入一律抛出 ValueError，绝不返回无穷大或 NaN。
    """
    current = _finite(current_ma, "photocurrent")
    power = _finite(optical_power, "optical power")
    if power_unit not in POWER_UNITS:
        raise ValueError("power_unit must be declared as 'uw' (microwatt) or 'mw' (milliwatt)")
    if current < 0:
        raise ValueError("photocurrent must be non-negative")
    if power <= 0:
        # 零值和负值在此被拦截：既不会出现除零，也不会出现负/无穷响应度。
        raise ValueError("optical power must be strictly positive; zero and negative power are rejected")
    if conversion_version == LEGACY_CONVERSION_VERSION:
        # 历史行为：无论工程师实际使用什么单位，数值一律按 mW 解释。
        power_to_w = MILLIWATT_TO_WATT
        legacy = True
    elif conversion_version == CONVERSION_VERSION:
        power_to_w = POWER_UNITS[power_unit]
        legacy = False
    else:
        raise ValueError(f"unknown conversion version: {conversion_version!r}")
    value = (current * MILLIAMP_TO_AMP) / (power * power_to_w)
    return ResponsivityResult(
        responsivity_aw=value,
        photocurrent_ma=current,
        optical_power=power,
        power_unit=power_unit,
        optical_power_unit_label=POWER_UNIT_LABELS[power_unit],
        conversion_version=conversion_version,
        legacy=legacy,
    )


def responsivity(current_ma: float, optical_power_mw: float) -> float:
    """兼容旧签名的浮点封装：调用方显式保证光功率单位为 mW。

    需要单位声明或换算版本信息时请改用 :func:`responsivity_aw`。
    """
    return responsivity_aw(current_ma, optical_power_mw, "mw").responsivity_aw
