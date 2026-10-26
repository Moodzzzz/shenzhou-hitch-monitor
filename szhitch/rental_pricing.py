"""长租价格证据与金额解析。

价格层级必须分开保存，禁止互相反推：

* ``list_reference``：``chooseCar/v3`` 返回的套餐日均，只是列表参考；
* ``calendar_rent``：价格日历逐日价求和，只代表该日租口径；
* ``order_total``：订单确认页的应付总额，目前没有免登录证据，不伪造。

所有金额统一用整数分保存。服务端返回整数元或小数元时，只接受最多两位
小数；非法值和超过两位小数的输入直接报错，不做静默截断。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

PRICE_CALENDAR_URI = "/resource/carrctapi/order/chooseCarPriceCalendar/v1"
LIST_REFERENCE_URI = "/resource/carrctapi/order/chooseCar/v3"

EVIDENCE_LIST_REFERENCE = "list_reference"
EVIDENCE_CALENDAR_RENT = "calendar_rent"
EVIDENCE_ORDER_TOTAL = "order_total"
EVIDENCE_LEGACY = "legacy_unknown"
EVIDENCE_CODES = {
    EVIDENCE_LEGACY: 0,
    EVIDENCE_LIST_REFERENCE: 1,
    EVIDENCE_CALENDAR_RENT: 2,
    EVIDENCE_ORDER_TOTAL: 3,
}
EVIDENCE_NAMES = {code: name for name, code in EVIDENCE_CODES.items()}

LIST_REFERENCE_NOTE = (
    "列表套餐日均只是参考值，不能乘以天数当作套餐价或应付总额。"
)
CALENDAR_NOTE = (
    "价格日历返回逐日价与 totalDailyPrice；这里只保存并核对租金日历口径，"
    "不等于订单应付，也不包含未取得的服务费、优惠和押金。"
)


class PriceContractError(ValueError):
    """价格字段不符合已核验契约。"""


def money_to_fen(value: Any, *, field: str = "金额") -> int:
    """把元金额转换为整数分，拒绝负数、NaN、无穷和超过两位小数。"""
    if value is None or value == "":
        raise PriceContractError(f"{field}为空")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise PriceContractError(f"{field}不是合法数字: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise PriceContractError(f"{field}必须是非负有限数字: {value!r}")
    if amount.as_tuple().exponent < -2:
        raise PriceContractError(f"{field}超过两位小数: {value!r}")
    return int(amount * 100)


def optional_money_to_fen(value: Any, *, field: str = "金额") -> int | None:
    if value in (None, ""):
        return None
    return money_to_fen(value, field=field)


def fen_to_yuan(value: int | None) -> float | None:
    return None if value is None else round(value / 100, 2)


def _parse_day(value: str) -> date:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError as exc:
        raise PriceContractError(f"价格日历日期格式错误: {value!r}") from exc


@dataclass(frozen=True)
class PriceCalendar:
    """一次价格日历核对结果（只描述日历租金口径）。"""

    rent_term: int
    start_day: date
    end_day: date
    total_fen: int
    day_prices: tuple[tuple[date, int], ...]
    evidence: str = EVIDENCE_CALENDAR_RENT

    def covered_days(self) -> int:
        return (self.end_day - self.start_day).days + 1


def parse_price_calendar(
    content: dict[str, Any] | None,
    *,
    expected_days: int | None = None,
) -> PriceCalendar:
    """解析并校验 ``chooseCarPriceCalendar/v1`` 的 content。

    已核验契约（2026-10-26，匿名移动站）：

    * ``rentStartDay`` / ``rentEndDay`` 为 YYYY-MM-DD；
    * ``rentTerm`` 为租期天数；
    * ``totalDailyPrice`` 是日历口径的租金数字；
    * ``dayPrices[].priceDate/dayPrice`` 覆盖 rentStartDay..rentEndDay。

    校验逐日价覆盖完整租期且求和等于 ``totalDailyPrice``。任何不一致都
    抛错，不做修复或猜测。
    """
    if not isinstance(content, dict):
        raise PriceContractError("价格日历 content 不是对象")
    try:
        term = int(str(content.get("rentTerm")))
    except (TypeError, ValueError) as exc:
        raise PriceContractError("价格日历缺少合法 rentTerm") from exc
    if term <= 0:
        raise PriceContractError("rentTerm 必须为正数")
    if expected_days is not None and term != int(expected_days):
        raise PriceContractError(f"rentTerm={term} 与请求天数 {expected_days} 不一致")

    start = _parse_day(str(content.get("rentStartDay") or ""))
    end = _parse_day(str(content.get("rentEndDay") or ""))
    expected_end = start + timedelta(days=term - 1)
    if end != expected_end:
        raise PriceContractError(
            f"rentEndDay={end} 与 rentStartDay={start}+{term - 1} 天不一致"
        )
    total = money_to_fen(content.get("totalDailyPrice"), field="totalDailyPrice")

    raw_days = content.get("dayPrices")
    if not isinstance(raw_days, list):
        raise PriceContractError("价格日历缺少 dayPrices 数组")
    prices: list[tuple[date, int]] = []
    seen: set[date] = set()
    for item in raw_days:
        if not isinstance(item, dict):
            raise PriceContractError("dayPrices 中存在非对象")
        day = _parse_day(str(item.get("priceDate") or ""))
        if day in seen:
            raise PriceContractError(f"dayPrices 日期重复: {day}")
        seen.add(day)
        prices.append((day, money_to_fen(item.get("dayPrice"), field=f"{day} dayPrice")))
    window = [(day, fen) for day, fen in prices if start <= day <= end]
    if len(window) != term:
        raise PriceContractError(
            f"dayPrices 覆盖 {len(window)} 天，与租期 {term} 天不一致"
        )
    covered = sum(fen for _, fen in window)
    if covered != total:
        raise PriceContractError(
            f"逐日价合计 {covered} 分与 totalDailyPrice {total} 分不一致"
        )
    return PriceCalendar(
        rent_term=term,
        start_day=start,
        end_day=end,
        total_fen=total,
        day_prices=tuple(window),
    )


def evidence_name(code: int | str | None) -> str:
    if isinstance(code, str) and code in EVIDENCE_CODES:
        return code
    try:
        return EVIDENCE_NAMES.get(int(code or 0), EVIDENCE_LEGACY)
    except (TypeError, ValueError):
        return EVIDENCE_LEGACY


def require_ordered_days(days: Iterable[int]) -> Sequence[int]:
    ordered = tuple(sorted({int(day) for day in days if int(day) > 0}))
    if not ordered:
        raise PriceContractError("至少需要一个正数租期")
    return ordered
