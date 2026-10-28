"""普通租车长租行情：周租 / 半月租 / 月租的低价与折扣。

数据来自移动端「选车」接口（免登录）：
``POST /api/gw.do?uri=/resource/carrctapi/order/chooseCar/v3``

同一城市、同一取车时间，分别按 7 / 15 / 30 天各查一次，接口返回的
``packagePrice`` 是**该租期**的套餐日价（日价，不是总价，周租/月租由
服务端自动打折），``dailyPrice`` 是门市日价。两者都只含车辆租金，
**不含出行服务费（基础服务费 + 车辆整备）**。

因此这里只保留「列表参考价」这一层证据，不再用月租车型组起价反推服务费，
也不乘天数伪造套餐总价；需要逐日价时走 ``chooseCarPriceCalendar/v1``
单独取证（``calendar_rent``），订单应付在拿到订单页证据前一律「待核价」。

抓取口径：全城市（data/cities.json 全部城市）+ 全车型（不截断）。
471 城 × 3 档，4 路并发，单轮约 20–27 分钟，每天 12:13 跑一轮。
产物是紧凑格式的 ``dashboard/rental.json``：车型、门店、城市单独建字典，
报价行只存索引，文件体积约为展开写法的三分之一。
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

from .catalog import load_brands, load_cities, load_city_centers, parse_model
from .gateway import GatewayClient, GatewayError
from .models import parse_int
from .proxy import pool_from_config
from .rental_pricing import (
    EVIDENCE_CALENDAR_RENT,
    EVIDENCE_CODES,
    EVIDENCE_LIST_REFERENCE,
    LIST_REFERENCE_NOTE,
    PriceContractError,
    evidence_name,
    parse_price_calendar,
)

ROOT = Path(__file__).resolve().parents[1]
RENTAL_CITY_FILE = ROOT / "data" / "rental_cities.json"
DEFAULT_PERIODS = (7, 15, 30)
CN_TZ = timezone(timedelta(hours=8))
PAYLOAD_VERSION = 3
TIER_NAMES = ("normal", "fair", "good", "great")
#: 紧凑报价行的列序：
#: 城市 / 车型 / 天数 / 列表套餐日均(分) / 门市日价(分) / 日历租金合计(分,可空)
#: / 门店序号 / 门店ID / 证据码 / 是否陈旧
C_CITY, C_MODEL, C_DAYS, C_LIST, C_STD, C_CAL, C_DEPT, C_DEPT_ID, C_EVIDENCE, C_STALE = range(10)


def _yuan_int(value: Any, *, field: str = "金额") -> int | None:
    """严格解析整数元；出现小数或非法值时返回 None 并保留待核状态。"""
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        return None
    return int(amount)


@dataclass
class RentalDeal:
    """某城市、某租期、某门店下的一款车。

    ``rent_daily`` 是列表套餐日均参考；``calendar_total_fen`` 是价格日历核过的
    **该租期套餐总价**（周租/半月租/月租折扣已计入），核对失败时为空。
    两者都只代表租金，不含出行服务费，也不等于订单应付总额。
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
    standard_daily: int
    save_pct: int
    dept_id: str = ""
    calendar_total_fen: int | None = None
    evidence: str = EVIDENCE_LIST_REFERENCE
    stale: bool = False
    pickup_at: str = ""
    return_at: str = ""
    price_generated_at: str = ""

    @property
    def daily(self) -> float:
        return float(self.rent_daily)

    @property
    def list_total_fen(self) -> int:
        """只用于展示“按列表日均折算”的参考合计，不是套餐价。"""
        return self.rent_daily * 100 * self.days

    @property
    def calendar_daily(self) -> float | None:
        if self.calendar_total_fen is None or not self.days:
            return None
        return round(self.calendar_total_fen / 100 / self.days, 2)

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
            "deptId": self.dept_id,
            "distance": self.distance,
            "listDaily": self.rent_daily,
            "standardDaily": self.standard_daily,
            "listTotal": round(self.list_total_fen / 100, 2),
            "calendarTotal": (
                None if self.calendar_total_fen is None
                else round(self.calendar_total_fen / 100, 2)
            ),
            "calendarDaily": self.calendar_daily,
            "evidence": self.evidence,
            "stale": self.stale,
            "pickupAt": self.pickup_at,
            "returnAt": self.return_at,
            "priceGeneratedAt": self.price_generated_at,
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
    period_meta: dict[int, dict[str, Any]] = field(default_factory=dict)
    dept_names: list[str] = field(default_factory=list)
    stale: bool = False

    def available_days(self) -> list[int]:
        return sorted(d for d, rows in self.deals.items() if rows)

    def summary(self) -> dict[str, Any]:
        periods: dict[str, Any] = {}
        for days, rows in sorted(self.deals.items()):
            if not rows:
                continue
            cheapest = min(rows, key=lambda x: (x.rent_daily, x.model))
            best_save = max(rows, key=lambda x: (x.save_pct, -x.rent_daily))
            meta = dict(self.period_meta.get(days) or {})
            meta.update({
                "days": days,
                "pickupAt": meta.get("pickupAt", ""),
                "returnAt": meta.get("returnAt", ""),
                "priceGeneratedAt": meta.get("priceGeneratedAt", ""),
                "observedAt": meta.get("observedAt", ""),
                "stale": bool(meta.get("stale", self.stale)),
                "source": meta.get("source", "chooseCar/v3"),
                "evidence": meta.get("evidence", EVIDENCE_LIST_REFERENCE),
                "minListDaily": float(cheapest.rent_daily),
                "minDaily": float(cheapest.rent_daily),
                "minModel": cheapest.model,
                "bestSave": best_save.save_pct,
                "bestModel": best_save.model,
                "calendarVerified": sum(1 for row in rows if row.calendar_total_fen is not None),
                "count": len(rows),
            })
            periods[str(days)] = meta
        all_deals = [d for rows in self.deals.values() for d in rows]
        best = min(all_deals, key=lambda x: (x.rent_daily, x.model), default=None)
        out = {
            "name": self.name,
            "cityId": self.city_id,
            "depts": self.depts,
            "models": self.model_count,
            "minListDaily": float(best.rent_daily) if best else 0,
            "minDaily": float(best.rent_daily) if best else 0,
            "periods": periods,
            "deptNames": self.dept_names,
            "stale": bool(self.stale),
        }
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
        dept_id = str(dept.get("deptId") or "").strip()
        distance = str(dept.get("deptDistance") or "").strip()
        for raw in dept.get("models") or []:
            if not isinstance(raw, dict) or not _tier_ok(raw.get("bookFlag", True)):
                continue
            model_id = parse_int(raw.get("modelId")) or 0
            daily = _yuan_int(raw.get("packagePrice"))
            if daily is None:
                daily = _yuan_int(raw.get("lowPrice"))
            if not model_id or daily is None or daily <= 0:
                continue
            model_total += 1
            standard = _yuan_int(raw.get("dailyPrice")) or daily
            model_name = str(raw.get("modelName") or "未知车型").strip()
            info = parse_model(model_name, catalog)
            save = 0
            if standard > daily:
                save = int(round((1 - daily / standard) * 100))
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
                dept_id=dept_id,
                distance=distance,
                rent_daily=daily,
                standard_daily=standard,
                save_pct=save,
                evidence=EVIDENCE_LIST_REFERENCE,
            )
            old = merged.get(model_id)
            # 同款车在多个门店出现时，保留列表日均最低的那个门店；保留 deptId。
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
    """历史兼容占位：服务费不再反推，也不再伪造套餐总价。

    保留函数名只为避免旧调用方导入失败；参数被显式忽略。
    """
    del fallback, calibrate
    for days, rows in city.deals.items():
        for deal in rows:
            deal.calendar_total_fen = None
            deal.evidence = EVIDENCE_LIST_REFERENCE
        rows.sort(key=lambda x: (x.rent_daily, x.model))
        city.deals[days] = rows


def _compact_deal(deal: RentalDeal, dept_index: dict[str, int]) -> list[Any]:
    return [
        deal.city_id,
        deal.model_id,
        deal.days,
        deal.rent_daily * 100,
        deal.standard_daily * 100,
        deal.calendar_total_fen,
        dept_index.get(deal.dept, 0),
        deal.dept_id,
        EVIDENCE_CODES.get(deal.evidence, EVIDENCE_CODES[EVIDENCE_LIST_REFERENCE]),
        1 if deal.stale else 0,
    ]


def deal_daily(row: Sequence[Any]) -> float:
    """列表套餐日均参考价（不是套餐总价，不是应付）。"""
    return round(int(row[C_LIST]) / 100, 2)


def deal_list_total_fen(row: Sequence[Any]) -> int:
    """按列表日均折算的参考合计，仅供排序说明，不得当作报价。"""
    return int(row[C_LIST]) * (int(row[C_DAYS]) or 1)


def deal_save(row: Sequence[Any]) -> int:
    """列表日均相对门市日租的参考折扣。"""
    base = int(row[C_STD])
    if base <= 0:
        return 0
    return max(0, int(round((1 - int(row[C_LIST]) / base) * 100)))


def deal_tier(row: Sequence[Any]) -> int:
    save = deal_save(row)
    if save >= 50:
        return 3
    if save >= 30:
        return 2
    if save >= 15:
        return 1
    return 0


def _period_meta(
    city: RentalCity,
    days: int,
    *,
    fallback_pickup: datetime,
    observed_at: str,
) -> dict[str, Any]:
    meta = dict(city.period_meta.get(days) or {})
    pickup = meta.get("pickupAt") or fallback_pickup.strftime("%Y-%m-%d %H:%M")
    default_return = (fallback_pickup + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    meta.setdefault("pickupAt", pickup)
    meta.setdefault("returnAt", default_return)
    meta.setdefault("observedAt", observed_at)
    meta.setdefault("priceGeneratedAt", meta["observedAt"])
    meta.setdefault("stale", bool(city.stale))
    meta.setdefault("source", "chooseCar/v3")
    meta.setdefault("evidence", EVIDENCE_LIST_REFERENCE)
    return meta


def build_payload(
    cities: Sequence[RentalCity],
    *,
    pickup_at: datetime,
    generated_at: datetime | None = None,
    errors: Iterable[dict[str, Any]] | None = None,
    requested_cities: int = 0,
    carried: int = 0,
) -> dict[str, Any]:
    """把城市报价整理成 v3 紧凑 JSON（只保存有证据的价格层级）。"""
    now = generated_at or datetime.now(CN_TZ)
    observed_at = now.strftime("%Y-%m-%d %H:%M")
    models: dict[str, list[Any]] = {}
    city_rows: list[dict[str, Any]] = []
    deals: list[list[Any]] = []

    for city in cities:
        for rows in city.deals.values():
            for row in rows:
                if row.dept and row.dept not in city.dept_names:
                    city.dept_names.append(row.dept)
        dept_index = {name: i for i, name in enumerate(city.dept_names)}
        for days in city.deals:
            city.period_meta[days] = _period_meta(
                city, days, fallback_pickup=pickup_at, observed_at=observed_at
            )
        summary = city.summary()
        city_rows.append(summary)
        for days, rows in city.deals.items():
            meta = city.period_meta[days]
            for row in rows:
                row.stale = bool(meta.get("stale", city.stale))
                row.pickup_at = str(meta.get("pickupAt") or "")
                row.return_at = str(meta.get("returnAt") or "")
                row.price_generated_at = str(meta.get("priceGeneratedAt") or "")
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
        cheapest = min(rows, key=lambda r: (int(r[C_LIST]), int(r[C_STD])))
        by_days[str(days)] = {
            "days": days,
            "pickupAt": pickup_at.strftime("%Y-%m-%d %H:%M"),
            "returnAt": (pickup_at + timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
            "minListDaily": deal_daily(cheapest),
            "minDaily": deal_daily(cheapest),
            "avgListDaily": round(sum(deal_daily(r) for r in rows) / len(rows), 2),
            "calendarVerified": sum(1 for r in rows if r[C_CAL] is not None),
            "calendarAttempts": sum(
                int((c.period_meta.get(days) or {}).get("calendarAttempts", 0))
                for c in cities
            ),
            "calendarFailures": sum(
                int((c.period_meta.get(days) or {}).get("calendarFailures", 0))
                for c in cities
            ),
            "stale": sum(1 for r in rows if int(r[C_STALE] or 0)),
            "great": sum(1 for r in rows if deal_tier(r) == 3),
            "good": sum(1 for r in rows if deal_tier(r) >= 2),
            "count": len(rows),
            "priceKind": "list_reference",
        }

    def leader(days: int, key: str, limit: int = 12) -> list[list[Any]]:
        rows = [d for d in deals if int(d[C_DAYS]) == days]
        if key == "save":
            rows.sort(key=lambda r: (-deal_save(r), int(r[C_LIST])))
        else:
            rows.sort(key=lambda r: (int(r[C_LIST]), int(r[C_STD]), str(r[C_DEPT_ID])))
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
    stale_cities = sum(1 for c in cities if c.stale)
    summary = {
        "requested": requested_cities or len(cities),
        "cities": len(available),
        "models": len(unique_models),
        "deals": len(deals),
        "minListDaily": all_min,
        "minDaily": all_min,
        "carried": stale_cities,
        "staleCities": stale_cities,
        "calendarVerified": sum(1 for d in deals if d[C_CAL] is not None),
        "calendarAttempts": sum(v.get("calendarAttempts", 0) for v in by_days.values()),
        "calendarFailures": sum(v.get("calendarFailures", 0) for v in by_days.values()),
        "periods": by_days,
        "errorCount": len(error_rows),
    }
    return {
        "v": PAYLOAD_VERSION,
        "generated_at": observed_at,
        "pickup_at": pickup_at.strftime("%Y-%m-%d %H:%M"),
        "days": sorted(by_days and [int(k) for k in by_days] or list(DEFAULT_PERIODS)),
        "periods": by_days,
        "note": LIST_REFERENCE_NOTE,
        "pricing": {
            "kind": "list_reference",
            "payableAvailable": False,
            "serviceFeeIncluded": False,
            "note": "列表日均只用于找候选；最终套餐和应付必须等专用价格/订单接口核价。",
        },
        "scope": "全城市 · 全车型",
        "summary": summary,
        "models": models,
        "cities": city_rows,
        "deals": deals,
        "leaders": leaders,
        "errors": error_rows[:80],
        "errorCount": len(error_rows),
    }


def sanitize_calendar_totals(cities: Sequence[RentalCity], *, min_ratio: float = 0.2) -> int:
    """剔除明显失真的套餐总价，返回剔除条数。

    实测（2026-10-28）发现两类异常，都是接口原样返回但语义不可信：

    * 同城同店同车，租期越长总价反而越低（例如三亚途铠 7 天 204、15 天 94、
      30 天 139），长租档显然不是真实报价；
    * 套餐日均低到门市日价的 20% 以下，基本是占位值/系统脏数据。

    处理方式是**不采信**：把该行的套餐总价清空，面板回落为「待核套餐总价」，
    绝不臆造一个「修正后」的价格。
    """
    dropped = 0
    for city in cities:
        for day, rows in city.deals.items():
            for deal in rows:
                if deal.calendar_total_fen is None:
                    continue
                std_fen = int(deal.standard_daily or 0) * 100
                if std_fen > 0 and deal.calendar_total_fen < std_fen * day * min_ratio:
                    deal.calendar_total_fen = None
                    deal.evidence = EVIDENCE_LIST_REFERENCE
                    dropped += 1

    groups: dict[tuple[str, int, str], list[RentalDeal]] = {}
    for city in cities:
        for rows in city.deals.values():
            for deal in rows:
                if deal.calendar_total_fen is None:
                    continue
                groups.setdefault((city.city_id, deal.model_id, deal.dept_id), []).append(deal)
    for deals in groups.values():
        if len(deals) < 2:
            continue
        deals.sort(key=lambda d: d.days)
        floor: int | None = None
        for deal in deals:
            if floor is not None and deal.calendar_total_fen < floor:
                deal.calendar_total_fen = None
                deal.evidence = EVIDENCE_LIST_REFERENCE
                dropped += 1
                continue
            floor = deal.calendar_total_fen
    return dropped


def merge_rental_parts(
    parts_dir: str | Path,
    *,
    requested_cities: int = 0,
) -> dict[str, Any]:
    """把按省份分片抓到的 parts/*.json 合成一份全国 payload。

    分片跑法：每个 job 只抓自己那几个省，产物落到 ``dashboard/parts/<分片>.json``；
    全部跑完后再由 merge 任务调用这里合成 ``dashboard/rental.json``。
    各片的 ``periods`` 元信息（取车时间、陈旧标记）原样保留，不重新推断。
    """
    parts = sorted(Path(parts_dir).glob("*.json"))
    if not parts:
        raise FileNotFoundError(f"{parts_dir} 里没有分片产物")
    cities: list[RentalCity] = []
    seen: set[str] = set()
    errors: list[dict[str, Any]] = []
    latest: datetime | None = None
    for path in parts:
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({"shard": path.name, "error": f"读取失败: {exc}"})
            continue
        if payload.get("v") != PAYLOAD_VERSION:
            errors.append({"shard": path.name, "error": f"版本不符: {payload.get('v')}"})
            continue
        stamp = str(payload.get("pickup_at") or "")
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                when = datetime.strptime(stamp, fmt).replace(tzinfo=CN_TZ)
                latest = when if latest is None or when > latest else latest
                break
            except ValueError:
                continue
        for meta in payload.get("cities") or []:
            name = str(meta.get("name") or "").strip()
            if not name or name in seen:
                continue
            city = city_from_payload(payload, name, preserve_stale=True)
            if city is None:
                continue
            cities.append(city)
            seen.add(name)
        for item in payload.get("errors") or []:
            errors.append(item)
    if not cities:
        raise ValueError("所有分片都没有可用的城市数据")

    # 防止拿昨天的分片冒充今天：观测日期不是今天的，按陈旧处理
    today = datetime.now(CN_TZ).date()
    for city in cities:
        for day, rows in city.deals.items():
            meta = city.period_meta.get(day) or {}
            obs = str(meta.get("observedAt") or "")[:10]
            try:
                obs_date = datetime.strptime(obs, "%Y-%m-%d").date()
            except ValueError:
                obs_date = None
            stale = obs_date != today
            meta["stale"] = stale
            for deal in rows:
                deal.stale = stale

    dropped = sanitize_calendar_totals(cities)
    payload = build_payload(
        cities,
        pickup_at=latest or next_pickup(),
        errors=errors,
        requested_cities=requested_cities or len(cities),
    )
    payload["summary"]["suspiciousTotals"] = dropped
    return payload


def _deal_from_compact(row: Sequence[Any], city: RentalCity, meta: list[Any]) -> RentalDeal:
    days = int(row[C_DAYS]) or 0
    dept_idx = int(row[C_DEPT]) if len(row) > C_DEPT else 0
    dept_id = str(row[C_DEPT_ID] or "")
    period = city.period_meta.get(days) or {}
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
        dept_id=dept_id,
        distance="",
        rent_daily=int(row[C_LIST]) // 100,
        standard_daily=int(row[C_STD]) // 100 or int(row[C_LIST]) // 100,
        calendar_total_fen=(int(row[C_CAL]) if row[C_CAL] is not None else None),
        evidence=evidence_name(row[C_EVIDENCE]),
        stale=bool(int(row[C_STALE] or 0)),
        pickup_at=str(period.get("pickupAt") or ""),
        return_at=str(period.get("returnAt") or ""),
        price_generated_at=str(period.get("priceGeneratedAt") or ""),
        save_pct=deal_save(row),
    )


def city_from_payload(
    payload: dict[str, Any] | None,
    name: str,
    *,
    preserve_stale: bool = False,
) -> RentalCity | None:
    """从上一轮 v3 产物恢复整城快照；旧租期和时间必须原样保留。

    ``preserve_stale=False``（默认）用于「本轮没抓到、沿用上一轮」，
    整城会被标成陈旧；分片合并时要传 ``True``，否则新鲜数据会被误判成历史沿用。
    """
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
        stale=not preserve_stale,
    )
    for day_text, period in (meta.get("periods") or {}).items():
        if isinstance(period, dict):
            restored = dict(period)
            if not preserve_stale:
                restored["stale"] = True
            city.period_meta[int(day_text)] = restored
    for row in payload.get("deals") or []:
        if not isinstance(row, list) or str(row[C_CITY]) != city.city_id:
            continue
        day = int(row[C_DAYS]) or 0
        deal = _deal_from_compact(row, city, models.get(str(row[C_MODEL])) or [])
        city.deals.setdefault(day, []).append(deal)
    for day, rows in city.deals.items():
        stale_flag = True if not preserve_stale else bool(
            (city.period_meta.get(day) or {}).get("stale")
        )
        for deal in rows:
            deal.stale = stale_flag
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


def _verify_calendar_rows(
    rows: Sequence[RentalDeal],
    *,
    city_name: str,
    days: int,
    pickup_text: str,
    return_text: str,
    client: GatewayClient,
    limit: int,
) -> list[dict[str, Any]]:
    """对列表中最便宜的 N 条做价格日历核对；失败只记证据缺口。"""
    errors: list[dict[str, Any]] = []
    if limit <= 0:
        return errors
    for deal in list(rows)[:limit]:
        if not deal.dept_id:
            errors.append({
                "city": city_name,
                "days": days,
                "modelId": deal.model_id,
                "error": "价格日历缺少 pickupDeptId",
            })
            continue
        try:
            content = client.choose_car_price_calendar(
                pickup_city_id=deal.city_id,
                pickup_dept_id=deal.dept_id,
                model_id=deal.model_id,
                pickup_time=pickup_text,
                return_time=return_text,
            )
            parsed = parse_price_calendar(content, expected_days=days)
        except (GatewayError, OSError, ValueError) as exc:
            errors.append({
                "city": city_name,
                "days": days,
                "modelId": deal.model_id,
                "error": f"价格日历: {str(exc)[:110]}",
            })
            continue
        # 优先用套餐总价（周租/半月租/月租的折扣在这一层），没有套餐价才退回门市合计
        deal.calendar_total_fen = (
            parsed.package_total_fen
            if parsed.package_total_fen is not None
            else parsed.total_fen
        )
        deal.evidence = EVIDENCE_CALENDAR_RENT
    return errors


def _scan_one_city(
    name: str,
    *,
    index: Any,
    centers: dict[str, list[float]],
    catalog: Any,
    periods: Sequence[int],
    pickup: datetime,
    client: GatewayClient,
    calendar_verify: int = 0,
) -> tuple[RentalCity | None, list[dict[str, Any]]]:
    """抓一个城市的三档列表参考价。返回 (城市结果, 错误列表)。

    只调用 ``chooseCar/v3`` 获取候选和列表日均；不再调用月租起价接口
    反推服务费。专用价格日历/订单核价由独立探测流程完成。
    """
    errors: list[dict[str, Any]] = []
    city_id = index.city_id(name)
    center = centers.get(name)
    if not city_id or not center:
        return None, [{"city": name, "error": "缺少 cityId 或坐标"}]
    lat, lon = center
    city = RentalCity(name=name, city_id=city_id)
    observed_at = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")

    for period in periods:
        return_at = pickup + timedelta(days=period)
        pickup_text = pickup.strftime("%Y-%m-%d %H:%M")
        return_text = return_at.strftime("%Y-%m-%d %H:%M")
        try:
            content = client.choose_cars(
                city_id=city_id,
                pickup_time=pickup_text,
                return_time=return_text,
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
            for row in rows:
                if row.dept and row.dept not in city.dept_names:
                    city.dept_names.append(row.dept)
            city.deals[period] = rows
            city.period_meta[period] = {
                "pickupAt": pickup_text,
                "returnAt": return_text,
                "observedAt": observed_at,
                "priceGeneratedAt": observed_at,
                "stale": False,
                "source": "chooseCar/v3",
                "evidence": EVIDENCE_LIST_REFERENCE,
            }
            calendar_errors = _verify_calendar_rows(
                rows,
                city_name=name,
                days=period,
                pickup_text=pickup_text,
                return_text=return_text,
                client=client,
                limit=calendar_verify,
            )
            # 价格日历是增强证据，失败不阻塞列表参考价；独立计数。
            city.period_meta[period]["calendarAttempts"] = min(calendar_verify, len(rows))
            city.period_meta[period]["calendarFailures"] = len(calendar_errors)
            if calendar_errors:
                meta = city.period_meta[period]
                meta["calendarErrorSample"] = calendar_errors[0].get("error", "")[:140]

    return city, errors


def scan_rentals(
    cfg: dict[str, Any],
    *,
    client: GatewayClient | None = None,
    cities: Sequence[str] | None = None,
    provinces: Sequence[str] | None = None,
    days: Sequence[int] | None = None,
    log=print,
    export_path: str | Path | None = None,
    save_every: int = 0,
    previous: dict[str, Any] | None = None,
    proxy_pool: Any = None,
) -> dict[str, Any]:
    """逐城 × 租期抓取报价（默认全城市、全车型、多线程）。

    ``provinces`` 用于按省份分片：多台/多个 job 各抓一省，产物再由
    :func:`merge_rental_parts` 合成全国 payload。
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
    if provinces:
        wanted = {str(p).strip() for p in provinces if str(p).strip()}

        def _province(name: str) -> str:
            # 极少数城市没带 province（数据缺失），统一归到「未分类」，避免分片漏城
            return str((index.get(name) or {}).get("province") or "").strip() or "未分类"

        names = [n for n in names if _province(n) in wanted]
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
    calendar_verify = max(0, int(rental_cfg.get("calendar_verify_per_city", 0) or 0))
    errors: list[dict[str, Any]] = []
    done: dict[str, RentalCity] = {}
    clients: list[GatewayClient] = []
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
        created = GatewayClient(
            timeout=timeout,
            retries=retries,
            min_interval=base_interval * workers,
            proxy_pool=proxy_pool,
        )
        clients.append(created)
        return created

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
        dropped = sanitize_calendar_totals(ordered)
        payload = build_payload(
            ordered,
            pickup_at=pickup,
            errors=errors,
            requested_cities=total,
            carried=carried,
        )
        payload["summary"]["suspiciousTotals"] = dropped
        return payload

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
                    periods=periods, pickup=pickup, client=shared,
                    calendar_verify=calendar_verify,
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
                    calendar_verify=calendar_verify,
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
    payload["summary"]["requests"] = sum(c.request_count for c in clients)
    if proxy_pool is not None:
        payload["summary"]["proxy"] = proxy_pool.stats()
        try:
            proxy_pool.save()
        except OSError:
            pass
    log(
        f"[长租完成] {payload['summary']['cities']}/{total} 城 / "
        f"{payload['summary']['models']} 款 / {payload['summary']['deals']} 条报价 / "
        f"最低列表参考 {payload['summary']['minListDaily']} 元/天 / "
        f"日历已核 {payload['summary'].get('calendarVerified', 0)} 条 / "
        f"沿用 {payload['summary'].get('carried', 0)} 城 / "
        f"失败 {len(errors)} / 用时 {elapsed:.1f}s"
    )
    return payload


def _minute_text(value: Any) -> bool:
    try:
        datetime.strptime(str(value), "%Y-%m-%d %H:%M")
        return True
    except (TypeError, ValueError):
        return False


def validate_rental_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """校验 v3 产物自洽；不通过则拒绝发布，绝不覆盖上一份可信数据。"""
    if not isinstance(payload, dict):
        raise PriceContractError("长租产物不是对象")
    if payload.get("v") != PAYLOAD_VERSION:
        raise PriceContractError(
            f"长租产物版本为 {payload.get('v')!r}，需要 v{PAYLOAD_VERSION}"
        )
    cities = payload.get("cities")
    deals = payload.get("deals")
    models = payload.get("models")
    summary = payload.get("summary")
    if not isinstance(cities, list) or not isinstance(deals, list):
        raise PriceContractError("cities/deals 必须是数组")
    if not isinstance(models, dict) or not isinstance(summary, dict):
        raise PriceContractError("models/summary 结构错误")
    pricing = payload.get("pricing")
    if not isinstance(pricing, dict) or pricing.get("kind") != EVIDENCE_LIST_REFERENCE:
        raise PriceContractError("缺少明确的列表参考价口径")
    if pricing.get("payableAvailable") is not False:
        raise PriceContractError("当前没有订单应付证据，payableAvailable 必须为 false")

    city_by_id: dict[str, dict[str, Any]] = {}
    city_by_name: dict[str, dict[str, Any]] = {}
    for city in cities:
        if not isinstance(city, dict):
            raise PriceContractError("cities 中存在非对象")
        cid = str(city.get("cityId") or "")
        name = str(city.get("name") or "")
        if not cid or not name:
            raise PriceContractError("城市缺少 cityId/name")
        if cid in city_by_id:
            raise PriceContractError(f"城市 cityId 重复: {cid}")
        if name in city_by_name:
            raise PriceContractError(f"城市名称重复: {name}")
        periods = city.get("periods")
        if not isinstance(periods, dict) or not periods:
            raise PriceContractError(f"{name} 缺少 periods")
        for day_text, period in periods.items():
            try:
                days = int(day_text)
            except (TypeError, ValueError) as exc:
                raise PriceContractError(f"{name} 非法租期: {day_text!r}") from exc
            if days <= 0 or not isinstance(period, dict):
                raise PriceContractError(f"{name} 的 {day_text} 天元数据错误")
            if not _minute_text(period.get("pickupAt")) or not _minute_text(period.get("returnAt")):
                raise PriceContractError(f"{name} {days} 天缺少精确取还时间")
            if not _minute_text(period.get("priceGeneratedAt")):
                raise PriceContractError(f"{name} {days} 天缺少价格生成时间")
            if not isinstance(period.get("stale"), bool):
                raise PriceContractError(f"{name} {days} 天缺少布尔 stale")
        city_by_id[cid] = city
        city_by_name[name] = city

    seen_keys: set[tuple[str, int, int]] = set()
    calendar_verified = 0
    stale_deals = 0
    for row in deals:
        if not isinstance(row, list) or len(row) < 10:
            raise PriceContractError("报价行必须是至少 10 列的数组")
        cid = str(row[C_CITY])
        city = city_by_id.get(cid)
        if city is None:
            raise PriceContractError(f"报价行引用未知城市: {cid}")
        try:
            model_id = int(row[C_MODEL])
            days = int(row[C_DAYS])
            list_fen = int(row[C_LIST])
            std_fen = int(row[C_STD])
            dept_idx = int(row[C_DEPT])
            evidence = evidence_name(row[C_EVIDENCE])
            stale = bool(int(row[C_STALE] or 0))
        except (TypeError, ValueError) as exc:
            raise PriceContractError(f"报价行字段类型错误: {row!r}") from exc
        if model_id <= 0 or days <= 0 or list_fen <= 0 or std_fen <= 0:
            raise PriceContractError(f"报价行价格/ID 非法: {row!r}")
        if str(model_id) not in models:
            raise PriceContractError(f"报价行引用未知车型: {model_id}")
        if str(days) not in (city.get("periods") or {}):
            raise PriceContractError(f"{city['name']} {days} 天缺少租期元数据")
        dept_names = city.get("deptNames") or []
        if dept_idx < 0 or dept_idx >= len(dept_names):
            raise PriceContractError(f"{city['name']} 报价行门店索引越界")
        key = (cid, model_id, days)
        if key in seen_keys:
            raise PriceContractError(f"报价行重复: {key}")
        seen_keys.add(key)
        cal = row[C_CAL]
        if cal not in (None, ""):
            try:
                cal_value = int(cal)
            except (TypeError, ValueError) as exc:
                raise PriceContractError(f"日历合计不是整数分: {cal!r}") from exc
            if cal_value <= 0:
                raise PriceContractError(f"日历合计必须为正: {cal_value}")
            if evidence != EVIDENCE_CALENDAR_RENT:
                raise PriceContractError("有日历合计但证据级别不是 calendar_rent")
            calendar_verified += 1
        elif evidence == EVIDENCE_CALENDAR_RENT:
            raise PriceContractError("证据级别为 calendar_rent 但缺少日历合计")
        if stale:
            stale_deals += 1
            if not bool((city.get("periods") or {}).get(str(days), {}).get("stale")):
                raise PriceContractError("报价行 stale 与租期元数据冲突")

    stale_cities = sum(1 for city in cities if city.get("stale"))
    expected = {
        "cities": len(cities),
        "models": len({int(row[C_MODEL]) for row in deals}),
        "deals": len(deals),
        "carried": stale_cities,
        "staleCities": stale_cities,
        "calendarVerified": calendar_verified,
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            raise PriceContractError(
                f"summary.{key}={summary.get(key)!r}，应与明细一致为 {value}"
            )
    if summary.get("errorCount") != payload.get("errorCount"):
        raise PriceContractError("summary.errorCount 与顶层 errorCount 不一致")
    if cities and stale_cities == len(cities) and len(deals):
        raise PriceContractError(
            "全部城市均为历史沿用：这是可疑快照，拒绝覆盖上一份可信产物"
        )
    if payload.get("errorCount") and not payload.get("errors"):
        raise PriceContractError("errorCount 非零但 errors 为空")
    return payload


def save_payload(payload: dict[str, Any], path: str | Path) -> Path:
    """校验后原子替换。校验失败时原文件保持不动。"""
    validate_rental_payload(payload)
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
