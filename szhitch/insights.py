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
) -> dict[str, Any]:
    """组装 dashboard.json。"""
    now = generated_at or datetime.now()
    listings: list[dict[str, Any]] = []
    city_stats: dict[str, dict[str, Any]] = {}
    model_counter: Counter = Counter()
    price_buckets: Counter = Counter()
    status_counts: Counter = Counter()

    def add_listing(entry: dict[str, Any], price: int, city: str) -> None:
        listings.append(entry)
        status_counts[entry["status"]] += 1
        model_counter[entry["model"]] += 1
        bucket = str(price) if price in (1, 50, 100) else "其他"
        price_buckets[bucket] += 1
        stat = city_stats.setdefault(city, {"name": city, "count": 0, "min": 10**9, "sum": 0})
        stat["count"] += 1
        stat["min"] = min(stat["min"], price or 10**9)
        stat["sum"] += price or 0

    for it in items:
        cached = states.get(it.hitch_id)
        orig = it.original_price or (cached.orig_price if cached else 0) or 0
        add_listing(
            {
                "id": it.hitch_id,
                "model": it.model_name,
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
            },
            it.price,
            it.pickup_city,
        )

    # 被锁定的车已不在列表里，用状态库里最后已知的信息补进面板
    for st in states.values():
        if st.status != STATUS_LOCKED:
            continue
        add_listing(
            {
                "id": st.hitch_id,
                "model": st.model_name or "未知车型",
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
            },
            st.price,
            st.pickup_city,
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
        if row["orig"] and row["price"] and row["orig"] > row["price"]
    ]

    forecast = build_forecast(states.values(), arrivals, today=now.date())
    for city, info in forecast.get("cities", {}).items():
        stat = city_stats.get(city)
        if stat:
            info["live"] = stat["count"]
            info["min_price"] = stat["min"]
            info["avg_price"] = stat["avg"]

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "totals": {
            "live": len(items),
            "locked": status_counts.get(STATUS_LOCKED, 0),
            "cities": len(city_stats),
            "models": len(model_counter),
            "min_price": min(prices) if prices else 0,
            "avg_price": round(sum(prices) / len(prices), 1) if prices else 0,
            "avg_save": round(sum(savings) / len(savings), 1) if savings else 0,
            "priced": len(savings),
            "special": sum(1 for p in prices if p <= 100),
        },
        "status_counts": dict(status_counts),
        "price_buckets": {k: price_buckets.get(k, 0) for k in ("1", "50", "100", "其他")},
        "listings": listings,
        "cities": sorted(city_stats.values(), key=lambda x: (-x["count"], x["name"])),
        "models": [{"name": k, "count": v} for k, v in model_counter.most_common(40)],
        "forecast": forecast,
    }
