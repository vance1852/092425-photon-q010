"""光谱和良率测量的确定性科学计算。"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence

from .units import (
    CONVERSION_VERSION,
    MILLIAMP,
    MILLIWATT,
    current_to_ma,
    normalize_unit,
    power_to_mw,
)


@dataclass(frozen=True)
class ResponsivityResult:
    """光电响应度 R = I / P。

    result_unit 为输出单位 (mA/mW)；current_unit/power_unit 为输入声明单位；
    conversion_version 记录换算规则版本，旧测量重新分析时据此区分。
    """

    responsivity: float
    result_unit: str
    current_unit: str
    power_unit: str
    current_ma: float
    power_mw: float
    conversion_version: str


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


def responsivity(
    current: float,
    optical_power: float,
    current_unit: str = MILLIAMP,
    power_unit: str = MILLIWATT,
) -> ResponsivityResult:
    """计算光电响应度 R = I / P，结果以 mA/mW（等价 A/W）表示。

    输入电流和光功率必须带显式单位声明，先按 :mod:`photon_fab.units`
    的固定系数换算到 mA、mW 后再做除法。光功率为零或负值没有物理意义，
    抛出 ValueError 而不是返回 +/-Infinity。
    """
    current_ma = current_to_ma(current, current_unit)
    power_mw = power_to_mw(optical_power, power_unit)
    if power_mw <= 0.0:
        # 在换算后校验，保证 uW 与 mW 口径一致，并拦截带符号零。
        raise ValueError("optical power must be strictly positive after unit conversion")
    value = current_ma / power_mw
    if not math.isfinite(value):
        raise ValueError("responsivity result is not finite")
    return ResponsivityResult(
        responsivity=value,
        result_unit="mA/mW",
        current_unit=normalize_unit(current_unit),
        power_unit=normalize_unit(power_unit),
        current_ma=current_ma,
        power_mw=power_mw,
        conversion_version=CONVERSION_VERSION,
    )
