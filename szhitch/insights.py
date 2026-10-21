"""可视面板数据：把当前清单 + 生命周期状态 + 历史到达记录整理成面板用的 JSON。

「预报」的含义：基于监控期间观测到的**新车上线节奏**做统计外推。
车源上架没有固定时刻，因此用泊松到达模型给出「明天至少出现 1 条」的概率，
以及未来 N 天的期望条数。观测天数越久，数字越可信。
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any, Iterable

from .catalog import load_brands, load_cities, parse_model
from .models import HitchItem
from .store import STATUS_AVAILABLE, STATUS_LOCKED, ListingState

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _poisson_at_least_one(lam: float) -> float:
    """P(X >= 1) = 1 - e^(-λ)"""
    return 1.0 - math.exp(-max(0.0, lam))


def build_forecast(
    states: Iterable[ListingState],
    arrivals: Iterable[tuple[str, str]] | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """按取车城市统计到达节奏，并给出未来 7 天预测。

    arrivals 为 (ISO时间, 城市) 列表，来自状态库的到达日志；
    缺省时退回用 listings 的 first_seen 估算（精度略低但可用）。
    """
    states = list(states)
    today = today or date.today()

    first_seen_dates = [d for d in (_parse_iso(s.first_seen) for s in states) if d]
    if not first_seen_dates:
        return {"available": False, "reason": "尚无历史数据", "cities": {}, "daily": {}}

    baseline_day = min(first_seen_dates).date()
    observed_days = (today - baseline_day).days

    # 逐城市逐日的「新车到达」计数（建立基线那一批不算到达）
    per_city_day: dict[str, Counter] = defaultdict(Counter)

    rows = list(arrivals or [])
    if rows:
        for ts, city in rows:
            seen = _parse_iso(ts)
            if not seen:
                continue
            d = seen.date()
            if d <= baseline_day:
                continue
            per_city_day[city or "未知"][d] += 1
    else:
        for s in states:
            seen = _parse_iso(s.first_seen)
            if not seen:
                continue
            d = seen.date()
            if d <= baseline_day:
                continue
            per_city_day[s.pickup_city or "未知"][d] += 1

    daily_total: Counter = Counter()
    for counter in per_city_day.values():
        daily_total.update(counter)

    cities: dict[str, Any] = {}
    for city, counter in per_city_day.items():
        arrivals_n = sum(counter.values())
        if arrivals_n <= 0:
            continue
        rate = arrivals_n / observed_days if observed_days > 0 else None
        weekday = [0] * 7
        for d, n in counter.items():
            weekday[d.weekday()] += n
        cities[city] = {
            "arrivals": arrivals_n,
            "rate": round(rate, 2) if rate is not None else None,
            "p_next_day": round(_poisson_at_least_one(rate), 3) if rate is not None else None,
            "expect_7d": round(rate * 7, 1) if rate is not None else None,
            "by_weekday": weekday,
            "last_arrival": max(counter).isoformat(),
        }

    return {
        "available": observed_days >= 1,
        "reason": "" if observed_days >= 1 else "观测不足 1 天，先跑几天再来看预报",
        "baseline_date": baseline_day.isoformat(),
        "observed_days": observed_days,
        "total_arrivals": sum(daily_total.values()),
        "cities": cities,
        "daily": {
            "dates": sorted(d.isoformat() for d in daily_total),
            "counts": [daily_total[d] for d in sorted(daily_total)],
        },
    }


def build_payload(
    items: list[HitchItem],
    states: dict[str, ListingState],
    *,
    arrivals: Iterable[tuple[str, str]] | None = None,
    generated_at: datetime | None = None,
    profiles: Any = None,
) -> dict[str, Any]:
    """组装 dashboard.json。

    面板是纯静态页，所有检索/筛选所需的数据都在这里备好：
    ``city_dict``（471 城全量 + 拼音 + 首字母 + 省份）、``brands``、
    ``routes``，前端只做过滤，不再依赖「当前有车的城市」。
    """
    now = generated_at or datetime.now()
    listings: list[dict[str, Any]] = []
    city_stats: dict[str, dict[str, Any]] = {}
    model_counter: Counter = Counter()
    brand_counter: Counter = Counter()
    route_counter: Counter = Counter()
    route_price: dict[tuple[str, str], int] = {}
    price_buckets: Counter = Counter()
    status_counts: Counter = Counter()
    pickup_counts: Counter = Counter()
    return_counts: Counter = Counter()
    catalog = load_brands()

    def add_listing(entry: dict[str, Any], price: int, city: str, target: str = "") -> None:
        # 面板是静态 JSON，空字段直接不下发（每条约省 50 字节）
        entry = {
            k: v for k, v in entry.items()
            if v not in ("", None) and not (k in ("orig", "days", "hit") and not v)
        }
        listings.append(entry)
        status_counts[entry["status"]] += 1
        model_counter[entry["model"]] += 1
        if entry.get("brand"):
            brand_counter[entry["brand"]] += 1
        bucket = str(price) if price in (1, 50, 100) else "其他"
        price_buckets[bucket] += 1
        if city:
            pickup_counts[city] += 1
        if target:
            return_counts[target] += 1
        pair = (city, target)
        if city and target:
            route_counter[pair] += 1
            if price:
                route_price[pair] = min(route_price.get(pair, 10**9), price)
        stat = city_stats.setdefault(city, {"name": city, "count": 0, "min": 10**9, "sum": 0})
        stat["count"] += 1
        stat["min"] = min(stat["min"], price or 10**9)
        stat["sum"] += price or 0

    for it in items:
        cached = states.get(it.hitch_id)
        orig = it.original_price or (cached.orig_price if cached else 0) or 0
        info = parse_model(it.model_name, catalog)
        hit, _, _ = profiles.evaluate(it) if profiles is not None else (False, "", "")
        add_listing(
            {
                "id": it.hitch_id,
                "model": it.model_name,
                "brand": info.brand,
                "series": info.series,
                "desc": it.model_desc,
                "from": it.pickup_city,
                "to": it.return_city,
                "price": it.price,
                "orig": orig,
                "days": it.days or it.limit_days or 0,
                "begin": it.begin_date.isoformat() if it.begin_date else "",
                "end": it.end_date.isoformat() if it.end_date else "",
                "deptFrom": it.pickup_dept,
                "deptTo": it.return_dept,
                "pic": it.pic,
                "status": STATUS_AVAILABLE,
                "hit": bool(hit),
            },
            it.price,
            it.pickup_city,
            it.return_city,
        )

    # 被锁定的车已不在列表里，用状态库里最后已知的信息补进面板
    for st in states.values():
        if st.status != STATUS_LOCKED:
            continue
        info = parse_model(st.model_name or "", catalog)
        add_listing(
            {
                "id": st.hitch_id,
                "model": st.model_name or "未知车型",
                "brand": info.brand,
                "series": info.series,
                "desc": "",
                "from": st.pickup_city,
                "to": st.return_city,
                "price": st.price,
                "orig": 0,
                "days": 0,
                "begin": st.begin_date,
                "end": st.end_date,
                "deptFrom": "",
                "deptTo": "",
                "pic": "",
                "status": STATUS_LOCKED,
                "goneSince": st.gone_since,
                "hit": False,
            },
            st.price,
            st.pickup_city,
            st.return_city,
        )

    for stat in city_stats.values():
        stat["avg"] = round(stat["sum"] / stat["count"], 1) if stat["count"] else 0
        stat.pop("sum", None)
        if stat["min"] == 10**9:
            stat["min"] = 0

    prices = [it.price for it in items if it.price]
    savings = [
        (1 - row["price"] / row["orig"]) * 100
        for row in listings
        if row.get("orig") and row.get("price") and row["orig"] > row["price"]
    ]

    forecast = build_forecast(states.values(), arrivals, today=now.date())
    for city, info in forecast.get("cities", {}).items():
        stat = city_stats.get(city)
        if stat:
            info["live"] = stat["count"]
            info["min_price"] = stat["min"]
            info["avg_price"] = stat["avg"]

    # 城市全量字典：471 城 + 在售/到达条数。列表里出现但字典还没收录的城市也补进来，
    # 保证面板永远不会「少城市」。
    index = load_cities()
    picked_names = set(pickup_counts) | set(return_counts)
    city_dict: list[list[Any]] = []
    known: set[str] = set()
    for city in index.cities:
        name = str(city["cityName"])
        known.add(name)
        city_dict.append([
            str(city.get("cityId") or ""),
            name,
            city.get("pinyin", ""),
            city.get("initials", ""),
            city.get("province", ""),
            city.get("letter", "#"),
            pickup_counts.get(name, 0),
            return_counts.get(name, 0),
        ])
    for name in sorted(picked_names - known):
        city_dict.append(["", name, "", "", "", "#",
                          pickup_counts.get(name, 0), return_counts.get(name, 0)])
    city_dict.sort(key=lambda row: (row[5], row[2] or row[1]))

    routes = [
        {"from": a, "to": b, "count": n, "min": route_price.get((a, b), 0)}
        for (a, b), n in route_counter.most_common(60)
    ]

    watch: dict[str, Any] = {"active": False, "profiles": []}
    if profiles is not None and len(profiles):
        watch = {
            "active": True,
            "profiles": [
                {"name": p.name or f"订阅{i + 1}", "desc": p.describe()}
                for i, p in enumerate(profiles.profiles)
            ],
        }

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "totals": {
            "live": len(items),
            "locked": status_counts.get(STATUS_LOCKED, 0),
            "cities": len(city_stats),
            "city_dict": len(city_dict),
            "models": len(model_counter),
            "brands": len(brand_counter),
            "routes": len(route_counter),
            "min_price": min(prices) if prices else 0,
            "avg_price": round(sum(prices) / len(prices), 1) if prices else 0,
            "avg_save": round(sum(savings) / len(savings), 1) if savings else 0,
            "priced": len(savings),
            "special": sum(1 for p in prices if p <= 100),
            "hit": sum(1 for row in listings if row.get("hit")),
        },
        "status_counts": dict(status_counts),
        "price_buckets": {k: price_buckets.get(k, 0) for k in ("1", "50", "100", "其他")},
        "listings": listings,
        "cities": sorted(city_stats.values(), key=lambda x: (-x["count"], x["name"])),
        "city_dict": city_dict,
        "brands": [{"name": k, "count": v} for k, v in brand_counter.most_common()],
        "routes": routes,
        "models": [{"name": k, "count": v} for k, v in model_counter.most_common(60)],
        "watch": watch,
        "forecast": forecast,
    }
