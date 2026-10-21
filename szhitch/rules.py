"""筛选规则：决定哪些顺风车值得推送。

规则全部为「与」关系；同一个列表内部的多个值为「或」关系。
空列表 / None 表示不限制。

两种用法：

1. 单一规则集（``watch``）：所有条件写在配置的 ``watch`` 段里；
2. **订阅分组**（``watch_profiles``）：写多组带名字的规则，任一命中即推送，
   消息里会标注「命中：成都出发」，方便同时盯几条不相干的线路。

路线写法：
    ``成都→丽江``   方向敏感（``丽江→成都`` 不会命中）
    ``成都→*``      只限定出发城市
    ``*→丽江``      只限定到达城市
    ``成都``        单城，等价于只限制取车城市
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Iterable

from .catalog import BrandCatalog, load_brands, normalize_city, parse_model
from .models import HitchItem, parse_date

#: 兼容旧引用：``from .rules import _norm_city``
_norm_city = normalize_city

ROUTE_SEPARATORS = ("→", "->", "—>", ">", "到", "-", "－")
WILDCARD = ("*", "全部", "不限", "any")


def _city_match(city: str, patterns: Iterable[str]) -> bool:
    """城市与关注列表的匹配：忽略「市/地区/县」后缀，允许包含关系。"""
    target = normalize_city(city)
    if not target:
        return False
    for raw in patterns:
        probe = normalize_city(str(raw))
        if not probe:
            continue
        if target == probe or probe in target or target in probe:
            return True
    return False


def parse_route(text: str) -> tuple[str, str] | None:
    """把 ``成都→丽江`` / ``成都到丽江`` / ``成都-丽江`` 拆成 (出发, 到达)。

    单个城市（没有分隔符）视为「只限制出发城市」。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    for sep in ROUTE_SEPARATORS:
        if sep in raw:
            left, _, right = raw.partition(sep)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return raw, "*"


def _is_wildcard(text: str) -> bool:
    return str(text).strip().lower() in WILDCARD


def route_match(item: HitchItem, route: str) -> bool:
    """方向敏感的路线匹配。``成都→*`` / ``*→丽江`` 支持通配。"""
    parts = parse_route(route)
    if not parts:
        return False
    src, dst = parts
    src_ok = _is_wildcard(src) or _city_match(item.pickup_city, [src])
    dst_ok = _is_wildcard(dst) or _city_match(item.return_city, [dst])
    return src_ok and dst_ok


@dataclass
class RuleSet:
    name: str = ""
    pickup_cities: list[str] = field(default_factory=list)
    return_cities: list[str] = field(default_factory=list)
    any_cities: list[str] = field(default_factory=list)   # 取车或还车任一命中
    routes: list[str] = field(default_factory=list)       # 形如 "成都→丽江"、"成都→*"
    brands: list[str] = field(default_factory=list)       # 品牌白名单（大众/理想/宝马…）
    exclude_brands: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)       # 车型关键词白名单（含车系）
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
    catalog: BrandCatalog | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_config(cls, cfg: dict, *, name: str = "") -> "RuleSet":
        cfg = cfg or {}
        return cls(
            name=str(cfg.get("name") or name or ""),
            pickup_cities=list(cfg.get("pickup_cities") or []),
            return_cities=list(cfg.get("return_cities") or []),
            any_cities=list(cfg.get("any_cities") or []),
            routes=list(cfg.get("routes") or []),
            brands=list(cfg.get("brands") or []),
            exclude_brands=list(cfg.get("exclude_brands") or []),
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

    def with_catalog(self, catalog: BrandCatalog | None) -> "RuleSet":
        return replace(self, catalog=catalog)

    def enabled_profile(self) -> bool:
        return True

    # ------------------------------------------------------------------ 判断

    def evaluate(self, item: HitchItem) -> tuple[bool, str]:
        """返回 (是否命中, 未命中的原因)。"""
        if self.pickup_cities and not _city_match(item.pickup_city, self.pickup_cities):
            return False, f"取车城市 {item.pickup_city} 不在关注列表"
        if self.return_cities and not _city_match(item.return_city, self.return_cities):
            return False, f"还车城市 {item.return_city} 不在关注列表"
        if self.any_cities and not (
            _city_match(item.pickup_city, self.any_cities)
            or _city_match(item.return_city, self.any_cities)
        ):
            return False, f"取还车城市都不在 {','.join(self.any_cities)}"

        if self.routes:
            if not any(route_match(item, r) for r in self.routes):
                return False, f"路线 {item.pickup_city}→{item.return_city} 未命中"

        info = parse_model(item.model_name, self.catalog)
        if self.brands and info.brand not in self.brands:
            return False, f"品牌 {info.brand or item.model_name} 不在白名单"
        if self.exclude_brands and info.brand in self.exclude_brands:
            return False, f"品牌 {info.brand} 在排除列表"

        if self.models:
            name = f"{item.model_name}{info.brand}{info.series}".lower()
            if not any(str(k).lower() in name for k in self.models):
                return False, f"车型 {item.model_name} 不在白名单"
        if self.exclude_models:
            name = f"{item.model_name}{info.brand}{info.series}".lower()
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
        if self.name:
            parts.append(f"「{self.name}」")
        if self.pickup_cities:
            parts.append("取车=" + ",".join(self.pickup_cities))
        if self.return_cities:
            parts.append("还车=" + ",".join(self.return_cities))
        if self.any_cities:
            parts.append("城市=" + ",".join(self.any_cities))
        if self.routes:
            parts.append("路线=" + ",".join(self.routes))
        if self.brands:
            parts.append("品牌=" + ",".join(self.brands))
        if self.exclude_brands:
            parts.append("排除品牌=" + ",".join(self.exclude_brands))
        if self.models:
            parts.append("车型=" + ",".join(self.models))
        if self.only_special:
            parts.append(f"特价≤{self.special_price}元")
        if self.max_price is not None:
            parts.append(f"价格≤{self.max_price}元")
        if self.min_days or self.max_days:
            parts.append(f"租期{self.min_days or '?'}-{self.max_days or '?'}天")
        return " · ".join(parts) if parts else "不限条件（全国全量）"


class ProfileSet:
    """一组订阅。任一命中即推送，并回传命中的订阅名。"""

    def __init__(self, profiles: Iterable[RuleSet], *, fallback: RuleSet | None = None) -> None:
        self.profiles = [p for p in profiles if p is not None]
        self.fallback = fallback or RuleSet()

    def __len__(self) -> int:
        return len(self.profiles)

    def evaluate(self, item: HitchItem) -> tuple[bool, str, str]:
        """返回 (是否命中, 命中的订阅名, 未命中原因)。"""
        if not self.profiles:
            ok, reason = self.fallback.evaluate(item)
            return ok, "" if ok else "", reason
        first_reason = ""
        for profile in self.profiles:
            ok, reason = profile.evaluate(item)
            if ok:
                return True, profile.name, ""
            first_reason = first_reason or reason
        return False, "", first_reason

    def describe(self) -> str:
        if not self.profiles:
            return self.fallback.describe()
        return " | ".join(p.describe() for p in self.profiles)


def build_profiles(cfg: dict[str, Any]) -> ProfileSet:
    """从配置构造订阅集合。

    优先 ``watch_profiles``；为空时退回单一 ``watch``，保持旧配置可用。
    """
    catalog = load_brands()
    raw = cfg.get("watch_profiles") or []
    profiles: list[RuleSet] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        name = str(item.get("name") or f"订阅{idx + 1}")
        profiles.append(RuleSet.from_config(item, name=name).with_catalog(catalog))
    fallback = RuleSet.from_config(cfg.get("watch") or {}).with_catalog(catalog)
    return ProfileSet(profiles, fallback=fallback)
