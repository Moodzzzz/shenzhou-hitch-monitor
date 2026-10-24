"""普通租车长租行情：周租 / 半月租 / 月租的低价与折扣。

数据来自移动端「选车」接口（免登录）：
``POST /api/gw.do?uri=/resource/carrctapi/order/chooseCar/v3``

同一城市、同一取车时间，分别按 7 / 15 / 30 天查一次，接口返回的
``packagePrice`` 就是当前租期的套餐日价，``dailyPrice`` 是门市日价。
两者都只包含租金，不含基础服务费、整备费、异地还车等，因此这里统一
标注「租金参考」，不伪装成最终到手价。

产物是独立的 ``dashboard/rental.json``，与顺风车 ``dashboard.json`` 分开刷新：
顺风车每 15 分钟、长租每 6 小时，互不拖慢。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .catalog import load_brands, load_cities, load_city_centers, parse_model
from .gateway import GatewayClient, GatewayError
from .models import parse_int

ROOT = Path(__file__).resolve().parents[1]
RENTAL_CITY_FILE = ROOT / "data" / "rental_cities.json"
DEFAULT_PERIODS = (7, 15, 30)
CN_TZ = timezone(timedelta(hours=8))
MAX_MODELS_PER_CITY = 10
MAX_OUTPUT_MODELS = 6
#: 没有官方月租起价可校准时的兜底服务费（元/天，按车型组）。
#: 2 经济 / 9 舒适 / 16 SUV / 23 商务 / 30 豪华 / 44 新能源。
DEFAULT_SERVICE_FEE = {2: 50, 9: 60, 16: 80, 23: 80, 30: 100, 44: 50}
DEFAULT_SERVICE_FEE_OTHER = 50


@dataclass
class RentalDeal:
    """某城市、某租期下的一款车。

    官方选车接口只给租金。``出行服务费`` 由同城同车型组的官方月租起价
    与 30 天租金差额校准，包含基础服务/车辆整备等必缴项。
    """

    city: str
    city_id: str
    days: int
    model_id: int
    group_id: int
    model: str
    brand: str
    series: str
    desc: str
    pic: str
    dept: str
    distance: str
    rent_daily: int
    service_daily: float
    daily: float
    standard_daily: int
    rent_total: int
    service_total: int
    total: int
    save_pct: int

    @property
    def tier(self) -> str:
        if self.save_pct >= 50:
            return "great"
        if self.save_pct >= 30:
            return "good"
        if self.save_pct >= 15:
            return "fair"
        return "normal"

    def as_dict(self) -> dict[str, Any]:
        return {
            "city": self.city,
            "cityId": self.city_id,
            "days": self.days,
            "modelId": self.model_id,
            "groupId": self.group_id,
            "model": self.model,
            "brand": self.brand,
            "series": self.series,
            "desc": self.desc,
            "pic": self.pic,
            "dept": self.dept,
            "distance": self.distance,
            "rentDaily": self.rent_daily,
            "serviceDaily": round(self.service_daily, 1),
            "daily": round(self.daily, 1),
            "standardDaily": self.standard_daily,
            "rentTotal": self.rent_total,
            "serviceTotal": self.service_total,
            "total": self.total,
            "save": self.save_pct,
            "tier": self.tier,
        }


@dataclass
class RentalCity:
    name: str
    city_id: str
    depts: int = 0
    model_count: int = 0
    deals: dict[int, list[RentalDeal]] = field(default_factory=dict)
    group_prices: dict[int, int] = field(default_factory=dict)

    def available_days(self) -> list[int]:
        return sorted(d for d, rows in self.deals.items() if rows)

    def summary(self) -> dict[str, Any]:
        periods: dict[str, Any] = {}
        for days, rows in sorted(self.deals.items()):
            if not rows:
                continue
            cheapest = min(rows, key=lambda x: (x.daily, x.total))
            best_save = max(rows, key=lambda x: (x.save_pct, -x.daily))
            periods[str(days)] = {
                "minDaily": round(cheapest.daily, 1),
                "minRentDaily": cheapest.rent_daily,
                "minServiceDaily": round(cheapest.service_daily, 1),
                "minTotal": cheapest.total,
                "minRentTotal": cheapest.rent_total,
                "minServiceTotal": cheapest.service_total,
                "minModel": cheapest.model,
                "bestSave": best_save.save_pct,
                "bestModel": best_save.model,
                "count": len(rows),
            }
        all_deals = [d for rows in self.deals.values() for d in rows]
        best = min(all_deals, key=lambda x: (x.daily, x.total), default=None)
        return {
            "name": self.name,
            "cityId": self.city_id,
            "depts": self.depts,
            "models": self.model_count,
            "minDaily": round(best.daily, 1) if best else 0,
            "minRentDaily": best.rent_daily if best else 0,
            "minServiceDaily": round(best.service_daily, 1) if best else 0,
            "periods": periods,
        }


def _tier_ok(value: Any) -> bool:
    return value not in (False, 0, "0", None)


def parse_city_deals(
    content: dict[str, Any],
    *,
    city: str,
    city_id: str,
    days: int,
    catalog: Any = None,
) -> tuple[list[RentalDeal], int, int]:
    """解析一次 chooseCar 响应，返回 (去重后的车型, 门店数, 车型总数)。"""
    catalog = catalog or load_brands()
    depts = (content or {}).get("deptHangModels") or []
    merged: dict[int, RentalDeal] = {}
    model_total = 0

    for dept in depts:
        if not isinstance(dept, dict):
            continue
        dept_name = str(dept.get("deptName") or "").strip()
        distance = str(dept.get("deptDistance") or "").strip()
        for raw in dept.get("models") or []:
            if not isinstance(raw, dict) or not _tier_ok(raw.get("bookFlag", True)):
                continue
            model_id = parse_int(raw.get("modelId")) or 0
            daily = parse_int(raw.get("packagePrice")) or parse_int(raw.get("lowPrice")) or 0
            if not model_id or daily <= 0:
                continue
            model_total += 1
            standard = parse_int(raw.get("dailyPrice")) or daily
            model_name = str(raw.get("modelName") or "未知车型").strip()
            info = parse_model(model_name, catalog)
            save = 0
            if standard > daily:
                save = int(round((1 - daily / standard) * 100))
            rent_total = daily * days
            deal = RentalDeal(
                city=city,
                city_id=str(city_id),
                days=days,
                model_id=model_id,
                group_id=parse_int(raw.get("modelGroupId")) or 0,
                model=model_name,
                brand=info.brand,
                series=info.series,
                desc=str(raw.get("modelDesc") or "").strip(),
                pic=str(raw.get("modelImgUrl") or "").strip(),
                dept=dept_name,
                distance=distance,
                rent_daily=daily,
                service_daily=0.0,
                daily=float(daily),
                standard_daily=standard,
                rent_total=rent_total,
                service_total=0,
                total=rent_total,
                save_pct=save,
            )
            old = merged.get(model_id)
            if old is None or (deal.daily, deal.total) < (old.daily, old.total):
                merged[model_id] = deal

    rows = sorted(merged.values(), key=lambda x: (x.rent_total, x.rent_daily, x.model))[:MAX_MODELS_PER_CITY]
    return rows, len(depts), max(model_total, len(merged))


def apply_service_fees(
    city: RentalCity,
    *,
    fallback: dict[int, int] | None = None,
) -> None:
    """把「租金 + 出行服务费」补成总价，并按总日均取前 6 款。

    校准口径：同城同车型组的官方月租起价（longRent/modelGroupsList）≈
    30 天租金 + 30 天出行服务费。差额除以 30 得到服务费日价。
    拿不到官方月租时退回公开必缴结构（基础服务/整备）的兜底档位。
    """
    fallback = fallback or DEFAULT_SERVICE_FEE
    rates: dict[int, float] = {}
    for deal in city.deals.get(30, []):
        low = city.group_prices.get(deal.group_id)
        if not low:
            continue
        peers = [d for d in city.deals.get(30, []) if d.group_id == deal.group_id]
        if not peers:
            continue
        min_rent = min(d.rent_total for d in peers)
        diff = low - min_rent
        if diff > 0:
            rates[deal.group_id] = diff / 30.0

    for days, rows in city.deals.items():
        for deal in rows:
            rate = rates.get(deal.group_id)
            if rate is None:
                rate = float(fallback.get(deal.group_id, DEFAULT_SERVICE_FEE_OTHER))
            service_total = int(round(rate * days))
            deal.service_daily = round(rate, 1)
            deal.service_total = service_total
            deal.rent_total = deal.rent_daily * days
            deal.total = deal.rent_total + service_total
            deal.daily = round(deal.total / days, 1)
            standard_all_in = deal.standard_daily + deal.service_daily
            deal.save_pct = max(0, int(round((1 - deal.daily / standard_all_in) * 100))) if standard_all_in else 0
        rows.sort(key=lambda x: (x.daily, x.total, x.model))
        city.deals[days] = rows[:MAX_OUTPUT_MODELS]


def build_payload(
    cities: Sequence[RentalCity],
    *,
    pickup_at: datetime,
    generated_at: datetime | None = None,
    errors: Iterable[dict[str, Any]] | None = None,
    requested_cities: int = 0,
) -> dict[str, Any]:
    """把城市报价整理成面板用的 JSON（榜单 + 城市明细）。"""
    now = generated_at or datetime.now(CN_TZ)
    deals: list[dict[str, Any]] = []
    city_rows: list[dict[str, Any]] = []
    for city in cities:
        city_rows.append(city.summary())
        for days, rows in city.deals.items():
            deals.extend(row.as_dict() for row in rows)

    by_days: dict[str, dict[str, Any]] = {}
    for days in sorted({d for c in cities for d in c.deals}):
        rows = [d for d in deals if d["days"] == days]
        if not rows:
            continue
        cheapest = min(rows, key=lambda r: (r["daily"], r["total"]))
        by_days[str(days)] = {
            "days": days,
            "pickupAt": pickup_at.strftime("%Y-%m-%d %H:%M"),
            "returnAt": (pickup_at + timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
            "minDaily": min(r["daily"] for r in rows),
            "minRentDaily": cheapest["rentDaily"],
            "minServiceDaily": cheapest["serviceDaily"],
            "minTotal": min(r["total"] for r in rows),
            "avgDaily": round(sum(r["daily"] for r in rows) / len(rows), 1),
            "great": sum(1 for r in rows if r["tier"] == "great"),
            "good": sum(1 for r in rows if r["tier"] in ("great", "good")),
            "count": len(rows),
        }

    def leader(days: int, key: str, reverse: bool = False, limit: int = 12) -> list[dict[str, Any]]:
        rows = [d for d in deals if d["days"] == days]
        if key == "save":
            rows.sort(key=lambda r: (-r["save"], r["daily"], r["total"]))
        else:
            rows.sort(key=lambda r: (r["daily"], r["total"]))
        return rows[:limit]

    leaders = {
        str(days): {
            "daily": leader(days, "daily"),
            "save": leader(days, "save"),
        }
        for days in (7, 15, 30)
        if any(d["days"] == days for d in deals)
    }

    available = [c for c in cities if c.available_days()]
    unique_models = {(d["cityId"], d["modelId"]) for d in deals}
    all_min = min((d["daily"] for d in deals), default=0)
    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "pickup_at": pickup_at.strftime("%Y-%m-%d %H:%M"),
        "days": sorted(by_days and [int(k) for k in by_days] or list(DEFAULT_PERIODS)),
        "periods": by_days,
        "note": "总价 = 车辆租金 + 出行服务费（基础服务/整备等必缴项）。服务费按同城同级别官方月租起价校准，仅供比价，最终以下单页为准。",
        "fee_note": "出行服务费为估算值，已含基础服务与车辆整备；不含异地还车、上门取送等条件费用。",
        "summary": {
            "requested": requested_cities or len(cities),
            "cities": len(available),
            "models": len(unique_models),
            "minDaily": all_min,
            "periods": by_days,
        },
        "cities": city_rows,
        "deals": deals,
        "leaders": leaders,
        "errors": list(errors or []),
    }


def load_rental_cities() -> list[str]:
    try:
        data = json.loads(RENTAL_CITY_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("cities") if isinstance(data, dict) else data
    return [str(x).strip() for x in (rows or []) if str(x).strip()]


def next_pickup(now: datetime | None = None, *, offset_days: int = 1) -> datetime:
    """统一取「明天 10:00」，让不同城市的价格在同一时间口径下可比。"""
    now = now or datetime.now(CN_TZ)
    day = (now + timedelta(days=max(1, int(offset_days)))).date()
    return datetime(day.year, day.month, day.day, 10, 0, tzinfo=CN_TZ)


def scan_rentals(
    cfg: dict[str, Any],
    *,
    client: GatewayClient | None = None,
    cities: Sequence[str] | None = None,
    days: Sequence[int] | None = None,
    log=print,
) -> dict[str, Any]:
    """逐城 × 租期抓取报价。单个城市失败只记录，不中断整轮。"""
    rental_cfg = cfg.get("rental") or {}
    limit = int(rental_cfg.get("city_limit", 0) or 0)
    names = list(cities or rental_cfg.get("cities") or load_rental_cities())
    if limit > 0:
        names = names[:limit]
    periods = tuple(int(x) for x in (days or rental_cfg.get("days") or DEFAULT_PERIODS))

    index = load_cities()
    centers = load_city_centers()
    catalog = load_brands()
    client = client or GatewayClient(
        timeout=float(rental_cfg.get("timeout", 20)),
        retries=int(rental_cfg.get("retries", 2)),
        min_interval=float(rental_cfg.get("min_request_interval", 0.55)),
    )
    pickup = next_pickup(offset_days=int(rental_cfg.get("pickup_offset_days", 1)))
    fallback = {
        int(k): int(v)
        for k, v in (rental_cfg.get("service_fee_fallback") or DEFAULT_SERVICE_FEE).items()
    }
    errors: list[dict[str, Any]] = []
    out: list[RentalCity] = []
    started = time.monotonic()

    for idx, name in enumerate(names, 1):
        city_id = index.city_id(name)
        center = centers.get(name)
        if not city_id or not center:
            errors.append({"city": name, "error": "缺少 cityId 或坐标"})
            continue
        lat, lon = center
        rc = RentalCity(name=name, city_id=city_id)
        try:
            groups = client.long_rent_groups(city_id=city_id)
            for group in (groups or {}).get("modelGroupList") or []:
                gid = parse_int(group.get("modelGroupId")) or 0
                price = parse_int(group.get("lowPrice")) or 0
                if gid and price > 0:
                    rc.group_prices[gid] = price
        except (GatewayError, OSError, ValueError) as exc:
            errors.append({"city": name, "error": f"月租校准失败: {str(exc)[:120]}"})

        for period in periods:
            return_at = pickup + timedelta(days=period)
            try:
                content = client.choose_cars(
                    city_id=city_id,
                    pickup_time=pickup.strftime("%Y-%m-%d %H:%M"),
                    return_time=return_at.strftime("%Y-%m-%d %H:%M"),
                    lat=lat,
                    lon=lon,
                )
            except (GatewayError, OSError, ValueError) as exc:
                errors.append({"city": name, "days": period, "error": str(exc)[:160]})
                continue
            rows, dept_n, model_n = parse_city_deals(
                content, city=name, city_id=city_id, days=period, catalog=catalog
            )
            rc.depts = max(rc.depts, dept_n)
            rc.model_count = max(rc.model_count, model_n)
            if rows:
                rc.deals[period] = rows
        apply_service_fees(rc, fallback=fallback)
        out.append(rc)
        if idx % 10 == 0 or idx == len(names):
            log(f"[长租] {idx}/{len(names)} 城，已取到 {sum(len(c.available_days()) for c in out)} 组报价")

    payload = build_payload(
        out,
        pickup_at=pickup,
        errors=errors,
        requested_cities=len(names),
    )
    elapsed = time.monotonic() - started
    payload["summary"]["elapsed"] = round(elapsed, 1)
    payload["summary"]["requests"] = getattr(client, "request_count", 0)
    log(
        f"[长租完成] {payload['summary']['cities']}/{len(names)} 城有报价 / "
        f"{payload['summary']['models']} 款 / 最低 {payload['summary']['minDaily']} 元/天 / "
        f"失败 {len(errors)} / 用时 {elapsed:.1f}s"
    )
    return payload


def save_payload(payload: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
        newline="\n",
    )
    return target


def load_payload(path: str | Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
