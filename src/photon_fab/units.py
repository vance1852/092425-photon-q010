"""物理量单位声明与确定性换算。

所有换算系数集中在本模块，并以 ``CONVERSION_VERSION`` 标注版本。
分析结果必须回传所用版本，旧记录重新分析时才能与新计算区分，
不允许在调用处各自隐式乘除。
"""

from __future__ import annotations

import math

# 换算规则版本：任何系数或支持单位发生变化时必须升版。
CONVERSION_VERSION = "unit-conversion-v1"

# 单位代码 -> (规范名, 相对毫单位的系数)，即 value_milli = value * factor。
# 1 uW = 0.001 mW；1 uA = 0.001 mA。
POWER_UNITS: dict[str, tuple[str, float]] = {
    "uw": ("microwatt", 1e-3),
    "mw": ("milliwatt", 1.0),
}
CURRENT_UNITS: dict[str, tuple[str, float]] = {
    "ua": ("microampere", 1e-3),
    "ma": ("milliampere", 1.0),
}

MILLIWATT = "mw"
MILLIAMP = "ma"


def _finite(name: str, value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(number):
        # 显式拒绝 NaN/Infinity，避免它们流入除法后污染 JSON 响应。
        raise ValueError(f"{name} must be a finite number")
    return number


def normalize_unit(unit: object) -> str:
    """归一化单位代码：去空白、小写，并把 µ/μ 统一为 ASCII ``u``。"""
    if not isinstance(unit, str):
        raise ValueError("unit must be a string")
    normalized = unit.strip().lower().replace("μ", "u").replace("µ", "u")
    return normalized


def _convert(name: str, value: object, unit: str, table: dict[str, tuple[str, float]]) -> float:
    number = _finite(name, value)
    unit = normalize_unit(unit)
    if unit not in table:
        supported = ", ".join(sorted(table))
        raise ValueError(f"unsupported {name} unit {unit!r}; supported units: {supported}")
    return number * table[unit][1]


def power_to_mw(value: object, unit: str) -> float:
    """把光功率换算为毫瓦 (mW)。"""
    return _convert("optical power", value, unit, POWER_UNITS)


def current_to_ma(value: object, unit: str) -> float:
    """把光电流换算为毫安 (mA)。"""
    return _convert("current", value, unit, CURRENT_UNITS)
