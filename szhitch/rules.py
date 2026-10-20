"""筛选规则：决定哪些顺风车值得推送。

规则全部为「与」关系；同一个列表内部的多个值为「或」关系。
空列表 / None 表示不限制。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import HitchItem, parse_date

_SUFFIXES = ("市", "地区", "自治州", "盟")


def _norm_city(name: str) -> str:
    text = (name or "").strip()
    for suffix in _SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            text = text[: -len(suffix)]
    return text


def _city_match(city: str, patterns: list[str]) -> bool:
    target = _norm_city(city)
    if not target:
        return False
    for raw in patterns:
        probe = _norm_city(str(raw))
        if not probe:
            continue
        if target == probe or probe in target or target in probe:
            return True
    return False


@dataclass
class RuleSet:
    pickup_cities: list[str] = field(default_factory=list)
    return_cities: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)       # 形如 "成都→丽江"
    models: list[str] = field(default_factory=list)       # 车型白名单（关键词）
    exclude_models: list[str] = field(default_factory=list)
    max_price: int | None = None
    min_price: int | None = None
    max_price_per_day: float | None = None
    only_special: bool = False
    special_price: int = 100
    min_days: int | None = None
    max_days: int | None = None
    date_from: date | None = None
    date_to: date | None = None

    @classmethod
    def from_config(cls, cfg: dict) -> "RuleSet":
        cfg = cfg or {}
        return cls(
            pickup_cities=list(cfg.get("pickup_cities") or []),
            return_cities=list(cfg.get("return_cities") or []),
            routes=list(cfg.get("routes") or []),
            models=list(cfg.get("models") or []),
            exclude_models=list(cfg.get("exclude_models") or []),
            max_price=cfg.get("max_price"),
            min_price=cfg.get("min_price"),
            max_price_per_day=cfg.get("max_price_per_day"),
            only_special=bool(cfg.get("only_special", False)),
            special_price=int(cfg.get("special_price", 100)),
            min_days=cfg.get("min_days"),
            max_days=cfg.get("max_days"),
            date_from=parse_date(cfg.get("date_from")),
            date_to=parse_date(cfg.get("date_to")),
        )

    # ------------------------------------------------------------------ 判断

    def evaluate(self, item: HitchItem) -> tuple[bool, str]:
        """返回 (是否命中, 未命中的原因)。"""
        if self.pickup_cities and not _city_match(item.pickup_city, self.pickup_cities):
            return False, f"取车城市 {item.pickup_city} 不在关注列表"
        if self.return_cities and not _city_match(item.return_city, self.return_cities):
            return False, f"还车城市 {item.return_city} 不在关注列表"

        if self.routes:
            target = f"{_norm_city(item.pickup_city)}→{_norm_city(item.return_city)}"
            ok = False
            for route in self.routes:
                parts = str(route).replace("-", "→").replace(">", "→").split("→")
                if len(parts) != 2:
                    continue
                if _norm_city(parts[0]) in target and _norm_city(parts[1]) in target:
                    ok = True
                    break
            if not ok:
                return False, f"路线 {item.pickup_city}→{item.return_city} 未命中"

        if self.models:
            name = item.model_name.lower()
            if not any(str(k).lower() in name for k in self.models):
                return False, f"车型 {item.model_name} 不在白名单"
        if self.exclude_models:
            name = item.model_name.lower()
            if any(str(k).lower() in name for k in self.exclude_models):
                return False, f"车型 {item.model_name} 在排除列表"

        if self.only_special and item.price > self.special_price:
            return False, f"价格 {item.price} 高于特价阈值 {self.special_price}"
        if self.max_price is not None and item.price > self.max_price:
            return False, f"价格 {item.price} 高于上限 {self.max_price}"
        if self.min_price is not None and item.price < self.min_price:
            return False, f"价格 {item.price} 低于下限 {self.min_price}"

        per_day = item.price_per_day
        if self.max_price_per_day is not None and per_day is not None:
            if per_day > self.max_price_per_day:
                return False, f"日均 {per_day} 元高于上限 {self.max_price_per_day}"

        if self.min_days is not None and (item.days or 0) < self.min_days:
            return False, f"租期 {item.days} 天少于 {self.min_days} 天"
        if self.max_days is not None and item.days and item.days > self.max_days:
            return False, f"租期 {item.days} 天多于 {self.max_days} 天"

        if self.date_from and item.begin_date and item.begin_date < self.date_from:
            return False, f"取车日期 {item.begin_date} 早于 {self.date_from}"
        if self.date_to and item.begin_date and item.begin_date > self.date_to:
            return False, f"取车日期 {item.begin_date} 晚于 {self.date_to}"

        return True, ""

    def describe(self) -> str:
        parts = []
        if self.pickup_cities:
            parts.append("取车=" + ",".join(self.pickup_cities))
        if self.return_cities:
            parts.append("还车=" + ",".join(self.return_cities))
        if self.routes:
            parts.append("路线=" + ",".join(self.routes))
        if self.models:
            parts.append("车型=" + ",".join(self.models))
        if self.only_special:
            parts.append(f"特价≤{self.special_price}元")
        if self.max_price is not None:
            parts.append(f"价格≤{self.max_price}元")
        if self.min_days or self.max_days:
            parts.append(f"租期{self.min_days or '?'}-{self.max_days or '?'}天")
        return " · ".join(parts) if parts else "不限条件（全国全量）"
