"""数据模型：把网关返回的原始字典整理成稳定的、便于规则判断的对象。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

_DATE_FORMATS = ("%Y.%m.%d", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d")
_CN_DATE = re.compile(r"^(\d{1,2})月(\d{1,2})日$")


def parse_date(value: Any, *, fallback_year: int | None = None) -> date | None:
    """兼容 ``2026.10.20`` / ``2026-10-20 20:32`` / ``10月22日`` 三种写法。"""
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    cn = _CN_DATE.match(text)
    if cn:
        year = fallback_year or date.today().year
        try:
            return date(year, int(cn.group(1)), int(cn.group(2)))
        except ValueError:
            return None
    return None


def parse_int(value: Any) -> int | None:
    """价格等字段服务端给的是字符串（"100" / "1" / "3048"），也可能带小数。"""
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class HitchItem:
    """一条顺风车。（顺风车 = 需要异地调回的车，用户以极低价格把车开到指定城市）"""

    hitch_id: str
    model_name: str
    price: int
    pickup_city: str
    return_city: str
    begin_date: date | None = None
    end_date: date | None = None
    limit_days: int | None = None
    model_id: int | None = None
    pic: str = ""
    original_price: int | None = None
    model_desc: str = ""
    pickup_dept: str = ""
    return_dept: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    # ------------------------------------------------------------------ 解析

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "HitchItem":
        """由 hitchList 的一条记录构造。"""
        begin = parse_date(payload.get("beginTime"))
        end = parse_date(payload.get("endTime"), fallback_year=begin.year if begin else None)
        if end is None:
            end = parse_date(payload.get("endDate"), fallback_year=begin.year if begin else None)

        return cls(
            hitch_id=str(payload.get("hitchId") or "").strip(),
            model_name=str(payload.get("modelName") or "未知车型").strip(),
            price=parse_int(payload.get("realTotalPrice")) or 0,
            pickup_city=str(payload.get("pickupCityName") or "").strip(),
            return_city=str(payload.get("returnCityName") or "").strip(),
            begin_date=begin,
            end_date=end,
            limit_days=parse_int(payload.get("limitTenancyDay")),
            model_id=parse_int(payload.get("modelId")),
            pic=str(payload.get("modelPic") or ""),
            raw=payload,
        )

    def merge_detail(self, detail: dict[str, Any]) -> "HitchItem":
        """用 hitchDetail 的结果补全门店、原价、车型描述等字段。"""
        if not detail:
            return self
        pickup_dept = (detail.get("pickupDept") or {}).get("deptName") or ""
        return_dept = (detail.get("returnDept") or {}).get("deptName") or ""
        return HitchItem(
            hitch_id=self.hitch_id,
            model_name=self.model_name or str(detail.get("modelName") or ""),
            price=parse_int(detail.get("realTotalPrice")) or self.price,
            pickup_city=self.pickup_city or str(detail.get("pickupCityName") or ""),
            return_city=self.return_city or str(detail.get("returnCityName") or ""),
            begin_date=self.begin_date or parse_date(detail.get("beginTime")),
            end_date=self.end_date or parse_date(detail.get("endTime")),
            limit_days=self.limit_days or parse_int(detail.get("limitTenancyDay")),
            model_id=self.model_id or parse_int(detail.get("modelId")),
            pic=self.pic or str(detail.get("modelPic") or ""),
            original_price=parse_int(detail.get("estTotalPrice")),
            model_desc=str(detail.get("modelDesc") or detail.get("modelDescNoSP") or ""),
            pickup_dept=str(pickup_dept),
            return_dept=str(return_dept),
            raw={**self.raw, "_detail": detail},
        )

    # ------------------------------------------------------------------ 派生

    @property
    def days(self) -> int | None:
        if self.begin_date and self.end_date:
            delta = (self.end_date - self.begin_date).days
            if delta > 0:
                return delta
        return None

    @property
    def price_per_day(self) -> float | None:
        d = self.days
        return round(self.price / d, 1) if d and self.price else None

    @property
    def discount(self) -> str:
        """形如 ``100 元 / 原价 3048``。"""
        if self.original_price and self.original_price > self.price:
            return f"{self.price} 元 / 原价 {self.original_price}"
        return f"{self.price} 元"

    def date_range(self) -> str:
        if self.begin_date and self.end_date:
            return f"{self.begin_date:%m-%d} → {self.end_date:%m-%d}"
        return ""

    def signature(self) -> str:
        """用于「同一条车但信息变了」的判重。"""
        return f"{self.price}|{self.begin_date}|{self.end_date}|{self.pickup_city}|{self.return_city}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "hitchId": self.hitch_id,
            "modelName": self.model_name,
            "price": self.price,
            "originalPrice": self.original_price,
            "pickupCity": self.pickup_city,
            "returnCity": self.return_city,
            "beginDate": self.begin_date.isoformat() if self.begin_date else None,
            "endDate": self.end_date.isoformat() if self.end_date else None,
            "days": self.days,
            "limitDays": self.limit_days,
            "modelId": self.model_id,
            "modelDesc": self.model_desc,
            "pickupDept": self.pickup_dept,
            "returnDept": self.return_dept,
        }

    def __str__(self) -> str:
        return f"{self.pickup_city}→{self.return_city} {self.model_name} {self.price}元 {self.date_range()}"
