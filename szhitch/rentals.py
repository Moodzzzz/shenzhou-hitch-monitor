"""普通租车长租行情：周租 / 半月租 / 月租的低价与折扣。

数据来自移动端「选车」接口（免登录）：
``POST /api/gw.do?uri=/resource/carrctapi/order/chooseCar/v3``

同一城市、同一取车时间，分别按 7 / 15 / 30 天查一次，接口返回的
``packagePrice`` 是当前租期的套餐日价（日价，不是总价），``dailyPrice``
是门市日价。两者都只包含车辆租金，不含出行服务费，所以这里再用同城同
车型组的官方月租起价（``longRent/modelGroupsList``）反推服务费日价，
把「租金 + 出行服务费」拆成两行展示。

抓取口径：全城市（data/cities.json 全部城市）+ 全车型（不截断）。
471 城 × 3 档 ≈ 1900 次请求，用 4 路并发压到 10 分钟左右，每天中午
12:00 跑一轮。产物是紧凑格式的 ``dashboard/rental.json``：车型、门店、
城市单独建字典，报价行只存索引，文件体积约为展开写法的三分之一。
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .catalog import load_brands, load_cities, load_city_centers, parse_model
from .gateway import GatewayClient, GatewayError
from .models import parse_int
from .proxy import pool_from_config

ROOT = Path(__file__).resolve().parents[1]
RENTAL_CITY_FILE = ROOT / "data" / "rental_cities.json"
DEFAULT_PERIODS = (7, 15, 30)
CN_TZ = timezone(timedelta(hours=8))
PAYLOAD_VERSION = 2
#: 没有官方月租起价可校准时的兜底服务费（元/天，按车型组）。
#: 2 经济 / 9 舒适 / 16 SUV / 23 商务 / 30 豪华 / 44 新能源。
DEFAULT_SERVICE_FEE = {2: 50, 9: 60, 16: 80, 23: 80, 30: 100, 44: 50}
DEFAULT_SERVICE_FEE_OTHER = 50
#: 反推出来的服务费日价超出这个区间，说明租期口径不可比，退回兜底档位。
SERVICE_FEE_MIN = 20.0
SERVICE_FEE_MAX = 130.0
TIER_NAMES = ("normal", "fair", "good", "great")
#: 紧凑报价行的列序：城市 / 车型 / 天数 / 租金日价 / 服务费日价 / 门市日价 / 总价 / 门店序号。
#: 折扣与档位由前端按「总价 vs 门市+服务费」现算，省掉两个字段。
C_CITY, C_MODEL, C_DAYS, C_RENT, C_SERVICE, C_STD, C_TOTAL, C_DEPT = range(8)


@dataclass
class RentalDeal:
    """某城市、某租期下的一款车（租金与服务费分开记）。"""

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
    def all_in_total(self) -> int:
        return self.rent_total + self.service_total

    @property
    def all_in_daily(self) -> float:
        return round(self.all_in_total / self.days, 1) if self.days else 0.0

    @property
    def tier(self) -> str:
        if self.save_pct >= 50:
            return "great"
        if self.save_pct >= 30:
            return "good"
        if self.save_pct >= 15:
            return "fair"
        return "normal"

    @property
    def tier_code(self) -> int:
        return TIER_NAMES.index(self.tier)

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
            "allInDaily": self.all_in_daily,
            "allInTotal": self.all_in_total,
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
    dept_names: list[str] = field(default_factory=list)
    stale: bool = False

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
                "minAllInDaily": cheapest.all_in_daily,
                "minAllInTotal": cheapest.all_in_total,
                "minModel": cheapest.model,
                "bestSave": best_save.save_pct,
                "bestModel": best_save.model,
                "count": len(rows),
            }
        all_deals = [d for rows in self.deals.values() for d in rows]
        best = min(all_deals, key=lambda x: (x.daily, x.total), default=None)
        out = {
            "name": self.name,
            "cityId": self.city_id,
            "depts": self.depts,
            "models": self.model_count,
            "minDaily": round(best.daily, 1) if best else 0,
            "minRentDaily": best.rent_daily if best else 0,
            "minServiceDaily": round(best.service_daily, 1) if best else 0,
            "minAllInDaily": best.all_in_daily if best else 0,
            "periods": periods,
            "deptNames": self.dept_names,
        }
        if self.stale:
            out["stale"] = True
        return out


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
    """解析一次 chooseCar 响应，返回 (全部车型, 门店数, 车型出现次数)。

    不截断车型：面板要能按城市看全量车型。
    """
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
            # 同款车在多个门店出现时，保留租金最低的那个门店
            if old is None or (deal.rent_daily, deal.dept) < (old.rent_daily, old.dept):
                merged[model_id] = deal

    rows = sorted(merged.values(), key=lambda x: (x.rent_daily, x.model))
    return rows, len(depts), max(model_total, len(merged))


def apply_service_fees(
    city: RentalCity,
    *,
    fallback: dict[int, int] | None = None,
    calibrate: bool = False,
) -> None:
    """把「租金 + 出行服务费」补成总价。

    校准口径：同城同车型组的官方月租起价 ≈ 30 天租金 + 30 天服务费，
    差额除以 30 得到服务费日价；超出合理区间说明该组月租起价与选车
    口径不可比，退回公开必缴结构的兜底档位。
    """
    fallback = fallback or DEFAULT_SERVICE_FEE
    rates: dict[int, float] = {}
    rows30 = city.deals.get(30) or [] if calibrate else []
    for deal in rows30:
        low = city.group_prices.get(deal.group_id)
        if not low or deal.group_id in rates:
            continue
        peers = [d for d in rows30 if d.group_id == deal.group_id]
        if not peers:
            continue
        diff = low - min(d.rent_total for d in peers)
        if diff <= 0:
            continue
        rate = diff / 30.0
        if SERVICE_FEE_MIN <= rate <= SERVICE_FEE_MAX:
            rates[deal.group_id] = rate

    dept_index: dict[str, int] = {}

    def dept_idx(name: str) -> int:
        if name not in dept_index:
            dept_index[name] = len(city.dept_names)
            city.dept_names.append(name)
        return dept_index[name]

    for days, rows in city.deals.items():
        for deal in rows:
            rate = rates.get(deal.group_id)
            if rate is None:
                rate = float(fallback.get(deal.group_id, DEFAULT_SERVICE_FEE_OTHER))
            deal.service_daily = round(rate, 1)
            deal.service_total = int(round(rate * days))
            deal.rent_total = deal.rent_daily * days
            # 与 App 的「套餐价／日均」对齐：只算租金，服务费单独列
            deal.total = deal.rent_total
            deal.daily = float(deal.rent_daily)
            standard_total = deal.standard_daily * days
            deal.save_pct = (
                max(0, int(round((1 - deal.total / standard_total) * 100)))
                if standard_total
                else 0
            )
            dept_idx(deal.dept)
        rows.sort(key=lambda x: (x.daily, x.total, x.model))
        city.deals[days] = rows


def _compact_deal(deal: RentalDeal, dept_index: dict[str, int]) -> list[Any]:
    fee = round(deal.service_daily, 1)
    return [
        deal.city_id,
        deal.model_id,
        deal.days,
        deal.rent_daily,
        int(fee) if float(fee).is_integer() else fee,
        deal.standard_daily,
        deal.total,
        dept_index.get(deal.dept, 0),
    ]


def deal_daily(row: Sequence[Any]) -> float:
    """套餐日均（与 App 车型卡上的「¥xx/日均」同口径）。"""
    days = int(row[C_DAYS]) or 1
    return round(int(row[C_TOTAL]) / days, 1)


def deal_all_in_daily(row: Sequence[Any]) -> float:
    """租金 + 出行服务费估算后的日均，仅作参考。"""
    days = int(row[C_DAYS]) or 1
    return round((int(row[C_TOTAL]) + float(row[C_SERVICE]) * days) / days, 1)


def deal_all_in_total(row: Sequence[Any]) -> int:
    days = int(row[C_DAYS]) or 1
    return int(int(row[C_TOTAL]) + float(row[C_SERVICE]) * days)


def deal_save(row: Sequence[Any]) -> int:
    """相对门市日租的折扣（App 价格日历里的「日租价 vs 套餐价」）。"""
    days = int(row[C_DAYS]) or 1
    base = float(row[C_STD]) * days
    if base <= 0:
        return 0
    return max(0, int(round((1 - int(row[C_TOTAL]) / base) * 100)))


def deal_tier(row: Sequence[Any]) -> int:
    save = deal_save(row)
    if save >= 50:
        return 3
    if save >= 30:
        return 2
    if save >= 15:
        return 1
    return 0


def build_payload(
    cities: Sequence[RentalCity],
    *,
    pickup_at: datetime,
    generated_at: datetime | None = None,
    errors: Iterable[dict[str, Any]] | None = None,
    requested_cities: int = 0,
    carried: int = 0,
) -> dict[str, Any]:
    """把城市报价整理成面板用的紧凑 JSON（车型/门店字典 + 索引行）。"""
    now = generated_at or datetime.now(CN_TZ)
    models: dict[str, list[Any]] = {}
    city_rows: list[dict[str, Any]] = []
    deals: list[dict[str, Any]] = []

    for city in cities:
        summary = city.summary()
        dept_index = {name: i for i, name in enumerate(city.dept_names)}
        city_rows.append(summary)
        for days, rows in city.deals.items():
            deals.extend(_compact_deal(row, dept_index) for row in rows)
            for row in rows:
                models.setdefault(
                    str(row.model_id),
                    [row.model, row.brand, row.series, row.desc, row.pic, str(row.group_id)],
                )

    by_days: dict[str, dict[str, Any]] = {}
    for days in sorted({int(d[C_DAYS]) for d in deals}):
        rows = [d for d in deals if int(d[C_DAYS]) == days]
        if not rows:
            continue
        cheapest = min(rows, key=lambda r: (int(r[C_TOTAL]), int(r[C_RENT])))
        by_days[str(days)] = {
            "days": days,
            "pickupAt": pickup_at.strftime("%Y-%m-%d %H:%M"),
            "returnAt": (pickup_at + timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
            "minDaily": deal_daily(cheapest),
            "minRentDaily": int(cheapest[C_RENT]),
            "minServiceDaily": cheapest[C_SERVICE],
            "minTotal": min(int(r[C_TOTAL]) for r in rows),
            "minRentTotal": int(cheapest[C_TOTAL]),
            "minServiceTotal": int(deal_all_in_total(cheapest) - int(cheapest[C_TOTAL])),
            "minAllInTotal": deal_all_in_total(cheapest),
            "minAllInDaily": deal_all_in_daily(cheapest),
            "avgDaily": round(sum(deal_daily(r) for r in rows) / len(rows), 1),
            "great": sum(1 for r in rows if deal_tier(r) == 3),
            "good": sum(1 for r in rows if deal_tier(r) >= 2),
            "count": len(rows),
        }

    def leader(days: int, key: str, limit: int = 12) -> list[list[Any]]:
        rows = [d for d in deals if int(d[C_DAYS]) == days]
        if key == "save":
            rows.sort(key=lambda r: (-deal_save(r), int(r[C_TOTAL])))
        else:
            rows.sort(key=lambda r: (deal_daily(r), int(r[C_TOTAL])))
        return rows[:limit]

    leaders = {
        str(days): {"daily": leader(days, "daily"), "save": leader(days, "save")}
        for days in (7, 15, 30)
        if any(int(d[C_DAYS]) == days for d in deals)
    }

    available = [c for c in cities if c.available_days()]
    unique_models = {int(d[C_MODEL]) for d in deals}
    all_min = min((deal_daily(d) for d in deals), default=0)
    error_rows = list(errors or [])
    return {
        "v": PAYLOAD_VERSION,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "pickup_at": pickup_at.strftime("%Y-%m-%d %H:%M"),
        "days": sorted(by_days and [int(k) for k in by_days] or list(DEFAULT_PERIODS)),
        "periods": by_days,
        "note": "套餐价 = 当前租期的车辆租金合计（与 App 选车页「日均」同口径）；周租/半月租/月租各自有套餐优惠，租期越长日均越低。",
        "fee_note": "出行服务费（基础服务 + 车辆整备）为估算值，App 在订单页另收，不计入套餐价；不含异地还车、上门取送等条件费用。",
        "scope": "全城市 · 全车型",
        "summary": {
            "requested": requested_cities or len(cities),
            "cities": len(available),
            "models": len(unique_models),
            "deals": len(deals),
            "minDaily": all_min,
            "carried": carried,
            "periods": by_days,
        },
        "models": models,
        "cities": city_rows,
        "deals": deals,
        "leaders": leaders,
        "errors": error_rows[:80],
        "errorCount": len(error_rows),
    }


def _deal_from_compact(row: Sequence[Any], city: RentalCity, meta: list[Any]) -> RentalDeal:
    days = int(row[C_DAYS]) or 0
    rent_daily = int(row[C_RENT])
    total = int(row[C_TOTAL])  # 套餐价（租金合计）
    dept_idx = int(row[C_DEPT]) if len(row) > C_DEPT else 0
    return RentalDeal(
        city=city.name,
        city_id=city.city_id,
        days=days,
        model_id=int(row[C_MODEL]),
        group_id=parse_int(meta[5] if len(meta) > 5 else 0) or 0,
        model=str(meta[0] if meta else ""),
        brand=str(meta[1] if len(meta) > 1 else ""),
        series=str(meta[2] if len(meta) > 2 else ""),
        desc=str(meta[3] if len(meta) > 3 else ""),
        pic=str(meta[4] if len(meta) > 4 else ""),
        dept=(city.dept_names[dept_idx] if len(city.dept_names) > dept_idx else ""),
        distance="",
        rent_daily=rent_daily,
        service_daily=float(row[C_SERVICE]),
        daily=round(total / days, 1) if days else 0.0,
        standard_daily=int(row[C_STD]) or rent_daily,
        rent_total=rent_daily * days,
        service_total=max(0, int(round(float(row[C_SERVICE]) * days))),
        total=total,
        save_pct=deal_save(row),
    )


def city_from_payload(payload: dict[str, Any] | None, name: str) -> RentalCity | None:
    """从上一轮紧凑产物里恢复一个城市，用于本轮抓取失败时兜底。"""
    if not payload or payload.get("v") != PAYLOAD_VERSION:
        return None
    meta = next((c for c in payload.get("cities") or [] if c.get("name") == name), None)
    if not meta or not meta.get("periods"):
        return None
    models = payload.get("models") or {}
    city = RentalCity(
        name=name,
        city_id=str(meta.get("cityId") or ""),
        depts=int(meta.get("depts") or 0),
        model_count=int(meta.get("models") or 0),
        dept_names=list(meta.get("deptNames") or []),
        stale=True,
    )
    for row in payload.get("deals") or []:
        if not isinstance(row, list) or str(row[C_CITY]) != city.city_id:
            continue
        day = int(row[C_DAYS]) or 0
        deal = _deal_from_compact(row, city, models.get(str(row[C_MODEL])) or [])
        city.deals.setdefault(day, []).append(deal)
    return city if city.deals else None


def load_rental_cities() -> list[str]:
    """优先抓取的种子城市（人工挑选的热门/旅游城市）。"""
    try:
        data = json.loads(RENTAL_CITY_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("cities") if isinstance(data, dict) else data
    return [str(x).strip() for x in (rows or []) if str(x).strip()]


def all_rental_cities(index: Any | None = None) -> list[str]:
    """全量抓取顺序：种子城市在前，其余城市按拼音排在后面。"""
    index = index or load_cities()
    priority = load_rental_cities()
    known = {c["cityName"] for c in index.cities}
    ordered = [n for n in priority if n in known]
    seen = set(ordered)
    rest = [c["cityName"] for c in index.cities if c["cityName"] not in seen]
    return ordered + rest


def next_pickup(now: datetime | None = None, *, offset_days: int = 1) -> datetime:
    """统一取「明天 10:00」，让不同城市的价格在同一时间口径下可比。"""
    now = now or datetime.now(CN_TZ)
    day = (now + timedelta(days=max(1, int(offset_days)))).date()
    return datetime(day.year, day.month, day.day, 10, 0, tzinfo=CN_TZ)


def _scan_one_city(
    name: str,
    *,
    index: Any,
    centers: dict[str, list[float]],
    catalog: Any,
    periods: Sequence[int],
    pickup: datetime,
    client: GatewayClient,
    fallback: dict[int, int],
    calibrate_fee: bool = False,
) -> tuple[RentalCity | None, list[dict[str, Any]]]:
    """抓一个城市的三档报价。返回 (城市结果, 错误列表)。"""
    errors: list[dict[str, Any]] = []
    city_id = index.city_id(name)
    center = centers.get(name)
    if not city_id or not center:
        return None, [{"city": name, "error": "缺少 cityId 或坐标"}]
    lat, lon = center
    city = RentalCity(name=name, city_id=city_id)
    try:
        groups = client.long_rent_groups(city_id=city_id)
        for group in (groups or {}).get("modelGroupList") or []:
            gid = parse_int(group.get("modelGroupId")) or 0
            price = parse_int(group.get("lowPrice")) or 0
            if gid and price > 0:
                city.group_prices[gid] = price
    except (GatewayError, OSError, ValueError) as exc:
        if "无库存" not in str(exc):
            errors.append({"city": name, "error": f"月租校准失败: {str(exc)[:100]}"})

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
            # 「无库存」= 这个城市当前没有可租车辆，属于正常结果，不算失败
            if "无库存" not in str(exc):
                errors.append({"city": name, "days": period, "error": str(exc)[:140]})
            continue
        rows, dept_n, model_n = parse_city_deals(
            content, city=name, city_id=city_id, days=period, catalog=catalog
        )
        city.depts = max(city.depts, dept_n)
        city.model_count = max(city.model_count, model_n)
        if rows:
            city.deals[period] = rows

    apply_service_fees(city, fallback=fallback, calibrate=calibrate_fee)
    return city, errors


def scan_rentals(
    cfg: dict[str, Any],
    *,
    client: GatewayClient | None = None,
    cities: Sequence[str] | None = None,
    days: Sequence[int] | None = None,
    log=print,
    export_path: str | Path | None = None,
    save_every: int = 0,
    previous: dict[str, Any] | None = None,
    proxy_pool: Any = None,
) -> dict[str, Any]:
    """逐城 × 租期抓取报价（默认全城市、全车型、多线程）。

    单个城市失败只记录，不中断整轮；若上一轮有该城市数据则沿用，
    并在 ``summary.carried`` 里计数，避免面板整块变空。
    """
    rental_cfg = cfg.get("rental") or {}
    limit = int(rental_cfg.get("city_limit", 0) or 0)
    index = load_cities()
    if cities:
        names = [str(x).strip() for x in cities if str(x).strip()]
    elif rental_cfg.get("cities"):
        names = [str(x).strip() for x in rental_cfg["cities"] if str(x).strip()]
    elif rental_cfg.get("full_scan", True):
        names = all_rental_cities(index)
    else:
        names = load_rental_cities()
    if limit > 0:
        names = names[:limit]
    periods = tuple(int(x) for x in (days or rental_cfg.get("periods") or DEFAULT_PERIODS))

    centers = load_city_centers()
    catalog = load_brands()
    timeout = float(rental_cfg.get("timeout", 20))
    retries = int(rental_cfg.get("retries", 2))
    base_interval = float(rental_cfg.get("min_request_interval", 0.3))
    workers = max(1, int(rental_cfg.get("workers", 4)))
    if client is not None:
        workers = 1
    pickup = next_pickup(offset_days=int(rental_cfg.get("pickup_offset_days", 1)))
    fallback = {
        int(k): int(v)
        for k, v in (rental_cfg.get("service_fee_fallback") or DEFAULT_SERVICE_FEE).items()
    }
    calibrate_fee = bool(rental_cfg.get("calibrate_service_fee", False))
    errors: list[dict[str, Any]] = []
    done: dict[str, RentalCity] = {}
    carried = 0
    started = time.monotonic()
    lock = threading.Lock()
    total = len(names)

    if proxy_pool is None:
        try:
            proxy_pool = pool_from_config(cfg, log=log)
        except Exception as exc:  # noqa: BLE001 - 代理池失败不能拖垮抓取
            log(f"[代理] 初始化失败，改直连：{str(exc)[:100]}")
            proxy_pool = None
    if proxy_pool is not None and len(proxy_pool):
        log(f"[代理] 启用，当前可用 {len(proxy_pool)} 个出口")

    def make_client() -> GatewayClient:
        # 每个线程独立限速，全局速率 ≈ workers / (base_interval * workers) = 1/base_interval
        return GatewayClient(
            timeout=timeout,
            retries=retries,
            min_interval=base_interval * workers,
            proxy_pool=proxy_pool,
        )

    def record(city: RentalCity | None, errs: list[dict[str, Any]], name: str, *, failed: bool = False) -> None:
        nonlocal carried
        if city is not None and city.available_days():
            done[name] = city
            errors.extend(errs)
            return
        errors.extend(errs)
        fallback_city = city_from_payload(previous, name)
        if fallback_city is not None:
            done[name] = fallback_city
            with lock:
                carried += 1
        elif failed:
            errors.append({"city": name, "error": "本轮无报价，且无可沿用的历史数据"})

    def snapshot() -> dict[str, Any]:
        ordered = [done[n] for n in names if n in done]
        return build_payload(
            ordered,
            pickup_at=pickup,
            errors=errors,
            requested_cities=total,
            carried=carried,
        )

    def flush() -> None:
        if not export_path:
            return
        save_payload(snapshot(), export_path)

    chunk = save_every or int(rental_cfg.get("save_every", 25))
    processed = 0

    if workers == 1:
        shared = client or make_client()
        for name in names:
            try:
                city, errs = _scan_one_city(
                    name, index=index, centers=centers, catalog=catalog,
                    periods=periods, pickup=pickup, client=shared, fallback=fallback,
                    calibrate_fee=calibrate_fee,
                )
            except Exception as exc:  # noqa: BLE001 - 单城异常不能中断整轮
                record(None, [{"city": name, "error": str(exc)[:140]}], name, failed=True)
            else:
                record(city, errs, name, failed=bool(errs) and city is None)
            processed += 1
            if processed % 10 == 0 or processed == total:
                log(f"[长租] {processed}/{total} 城，已取到 {len(done)} 城报价")
            if chunk and processed % chunk == 0:
                flush()
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rental") as pool:
            futures = {
                pool.submit(
                    _scan_one_city,
                    name,
                    index=index,
                    centers=centers,
                    catalog=catalog,
                    periods=periods,
                    pickup=pickup,
                    client=make_client(),
                    fallback=fallback,
                    calibrate_fee=calibrate_fee,
                ): name
                for name in names
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    city, errs = future.result()
                except Exception as exc:  # noqa: BLE001
                    record(None, [{"city": name, "error": str(exc)[:140]}], name, failed=True)
                else:
                    record(city, errs, name, failed=bool(errs) and city is None)
                processed += 1
                if processed % 20 == 0 or processed == total:
                    log(
                        f"[长租] {processed}/{total} 城，已取到 {len(done)} 城报价，"
                        f"沿用 {carried} 城"
                    )
                if chunk and processed % chunk == 0:
                    flush()

    payload = snapshot()
    elapsed = time.monotonic() - started
    payload["summary"]["elapsed"] = round(elapsed, 1)
    payload["summary"]["workers"] = workers
    payload["summary"]["requests"] = getattr(client, "request_count", 0) or int(
        total * (len(periods) + 1)
    )
    if proxy_pool is not None:
        payload["summary"]["proxy"] = proxy_pool.stats()
        try:
            proxy_pool.save()
        except OSError:
            pass
    log(
        f"[长租完成] {payload['summary']['cities']}/{total} 城 / "
        f"{payload['summary']['models']} 款 / {payload['summary']['deals']} 条报价 / "
        f"最低 {payload['summary']['minDaily']} 元/天 / 沿用 {carried} 城 / "
        f"失败 {len(errors)} / 用时 {elapsed:.1f}s"
    )
    return payload


def save_payload(payload: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(target)
    return target


def load_payload(path: str | Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
