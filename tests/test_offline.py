"""离线测试：不联网，用录制的真实响应验证解析、规则、去重与文案。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from szhitch.models import HitchItem, parse_date, parse_int  # noqa: E402
from szhitch.monitor import Event, Monitor, ScanStats  # noqa: E402
from szhitch.proxy import ProxyPool, mask, normalize_proxy, parse_proxy_list  # noqa: E402
from szhitch.rental_pricing import (  # noqa: E402
    PriceContractError,
    money_to_fen,
    parse_price_calendar,
)
from szhitch.rentals import (  # noqa: E402
    RentalCity,
    build_payload as build_rental_payload,
    city_from_payload,
    parse_city_deals,
    validate_rental_payload,
)
from szhitch.rules import RuleSet  # noqa: E402
from szhitch.store import JsonStore, ListingState, SqliteStore  # noqa: E402
from szhitch.subscriptions import parse_issue_body  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "hitch_list.json"


def load_fixture_items() -> list[dict]:
    data = json.loads(FIXTURE.read_text("utf-8"))
    return data["content"]["hitchList"]


class FakeClient:
    """替代 GatewayClient，便于构造价格变化、漏抓、线路复核等场景。"""

    def __init__(self, items: list[dict], detail: dict | None = None,
                 route_items: list[dict] | None = None) -> None:
        self.items = items
        self.detail = detail or {}
        self.route_items = route_items
        self.request_count = 0

    def hitch_all(self, **_kwargs):
        self.request_count += 1
        return list(self.items), len(self.items)

    def hitch_page(self, page_no=1, page_size=200, pickup_city_id=None, return_city_id=None):
        """定向复核用。route_items=None 表示「线路查不到」，即无法确认。"""
        self.request_count += 1
        if self.route_items is None:
            return [], 0
        return list(self.route_items), len(self.route_items)

    def hitch_detail(self, hitch_id):
        self.request_count += 1
        return self.detail


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, ListingState] = {}
        self.arrivals: list[tuple[str, str]] = []
        self.saved = 0

    def load(self):
        return dict(self.data)

    def save(self, records):
        self.saved += 1
        self.data = dict(records)

    def record_arrivals(self, pairs):
        self.arrivals.extend(pairs)

    def load_arrivals(self, days=90):
        return list(self.arrivals)

    def prune(self, days=30):
        return None


BASE_CFG = {
    "poll": {"page_size": 200, "max_pages": 5, "min_request_interval": 0, "timeout": 5, "retries": 0},
    "source": {},
    "watch": {},
    "behavior": {
        "first_run": "notify",
        "notify_new": True,
        "notify_price_drop": True,
        "enrich_detail": False,
        "max_items_per_message": 8,
    },
    "notify": {"console": False},
    "storage": {"type": "sqlite", "path": "data/test.db"},
}


class TestParsing(unittest.TestCase):
    def test_list_item_fields(self):
        item = HitchItem.from_api(load_fixture_items()[0])
        self.assertEqual(item.hitch_id, "9515098")
        self.assertEqual(item.model_name, "理想L7")
        self.assertEqual(item.price, 100)
        self.assertEqual(item.pickup_city, "张掖")
        self.assertEqual(item.return_city, "揭阳")
        self.assertEqual(item.begin_date, date(2026, 10, 20))
        self.assertEqual(item.end_date, date(2026, 10, 22))
        self.assertEqual(item.days, 2)
        self.assertEqual(item.price_per_day, 50.0)

    def test_date_variants(self):
        self.assertEqual(parse_date("2026.10.20"), date(2026, 10, 20))
        self.assertEqual(parse_date("2026-10-22 00:00"), date(2026, 10, 22))
        self.assertEqual(parse_date("10月2日", fallback_year=2026), date(2026, 10, 2))
        self.assertIsNone(parse_date("", fallback_year=2026))

    def test_price_coercion(self):
        self.assertEqual(parse_int("100"), 100)
        self.assertEqual(parse_int("1.0"), 1)
        self.assertEqual(parse_int(None), None)
        self.assertEqual(parse_int("abc"), None)

    def test_merge_detail_fills_original_price(self):
        item = HitchItem.from_api(load_fixture_items()[0])
        merged = item.merge_detail(
            {
                "estTotalPrice": "3048",
                "realTotalPrice": "100",
                "modelDesc": "42kWh增程式 | SUV 5座",
                "pickupDept": {"deptName": "张掖机场自助点"},
                "returnDept": {"deptName": "揭阳国际机场服务点"},
            }
        )
        self.assertEqual(merged.original_price, 3048)
        self.assertEqual(merged.pickup_dept, "张掖机场自助点")
        self.assertIn("原价 3048", merged.discount)


class TestRules(unittest.TestCase):
    def setUp(self):
        self.items = [HitchItem.from_api(p) for p in load_fixture_items()]

    def test_empty_rules_pass_everything(self):
        rules = RuleSet()
        for item in self.items:
            ok, _ = rules.evaluate(item)
            self.assertTrue(ok)

    def test_city_filter(self):
        rules = RuleSet(pickup_cities=["张掖"])
        hits = [i for i in self.items if rules.evaluate(i)[0]]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].pickup_city, "张掖")

    def test_city_filter_normalizes_suffix(self):
        rules = RuleSet(pickup_cities=["张掖市"])
        ok, _ = rules.evaluate(self.items[0])
        self.assertTrue(ok)

    def test_price_and_special(self):
        rules = RuleSet(max_price=50)
        self.assertFalse(rules.evaluate(self.items[0])[0])  # 100 元被挡
        special = RuleSet(only_special=True, special_price=1)
        self.assertFalse(special.evaluate(self.items[0])[0])

    def test_route_filter(self):
        rules = RuleSet(routes=["张掖→揭阳"])
        ok = [i for i in self.items if rules.evaluate(i)[0]]
        self.assertEqual([i.hitch_id for i in ok], ["9515098"])
        rules_miss = RuleSet(routes=["成都→丽江"])
        self.assertFalse(any(rules_miss.evaluate(i)[0] for i in self.items))

    def test_model_whitelist_and_exclude(self):
        rules = RuleSet(models=["理想"])
        self.assertTrue(rules.evaluate(self.items[0])[0])
        self.assertFalse(rules.evaluate(self.items[1])[0])
        rules_ex = RuleSet(exclude_models=["理想"])
        self.assertFalse(rules_ex.evaluate(self.items[0])[0])


class TestMonitorFlow(unittest.TestCase):
    @staticmethod
    def _make_live(items):
        """录制数据是 2026-10-20；把同一批车平移到未来，避免测试随时间过期。"""
        raw_begin = next((x for x in items if x.get("beginTime") == "2026.10.20"), None)
        if raw_begin:
            target = date.today() + timedelta(days=3)
            delta = (target - date(2026, 10, 20)).days
            for item in items:
                begin = parse_date(item.get("beginTime"))
                end = parse_date(item.get("endTime"))
                if begin:
                    item["beginTime"] = (begin + timedelta(days=delta)).strftime("%Y.%m.%d")
                if end:
                    item["endTime"] = (end + timedelta(days=delta)).strftime("%Y.%m.%d")
        return items

    def _monitor(self, items, store, cfg=None, client=None):
        items = self._make_live(items)
        conf = json.loads(json.dumps(BASE_CFG))
        conf.update(cfg or {})
        mon = Monitor(conf, client=client or FakeClient(items), store=store,
                      notifiers=[], log=lambda *_: None)
        # 固定城市字典，避免依赖 data/cities.json
        mon._city_ids = {"张掖": "335", "揭阳": "236", "合肥": "300",
                         "芜湖": "301", "呼和浩特": "302", "信阳": "303"}
        return mon

    def test_first_run_emits_new(self):
        store = FakeStore()
        mon = self._monitor(load_fixture_items(), store)
        stats = mon.run_once()
        self.assertEqual(stats.new, 3)
        self.assertEqual(stats.matched, 3)
        self.assertEqual(store.saved, 1)

    def test_second_run_no_duplicate(self):
        store = FakeStore()
        items = load_fixture_items()
        mon = self._monitor(items, store)
        mon.run_once()
        stats = mon.run_once()
        self.assertEqual(stats.new, 0)
        self.assertEqual(stats.matched, 0)

    def test_price_drop_detected(self):
        store = FakeStore()
        items = json.loads(json.dumps(load_fixture_items()))
        mon = self._monitor(items, store)
        mon.run_once()

        items[0]["realTotalPrice"] = "1"
        mon2 = self._monitor(items, store)
        stats = mon2.run_once()
        self.assertEqual(stats.price_drop, 1)
        self.assertEqual(stats.events[0].old_price, 100)
        self.assertEqual(stats.events[0].item.price, 1)

    def test_baseline_silent_on_first_run(self):
        store = FakeStore()
        cfg = {"behavior": {**BASE_CFG["behavior"], "first_run": "silent"}}
        mon = self._monitor(load_fixture_items(), store, cfg)
        stats = mon.run_once()
        self.assertTrue(stats.baseline)
        self.assertEqual(stats.matched, 0)
        self.assertEqual(store.saved, 1)  # 仍然落基线

    def test_dry_run_does_not_persist(self):
        store = FakeStore()
        mon = self._monitor(load_fixture_items(), store)
        mon.run_once(persist=False)
        self.assertEqual(store.saved, 0)

    def test_missing_but_still_listed_is_not_locked(self):
        """全量翻页漏抓时，定向复核能查到就撤销判定（线上真实误报场景）。"""
        store = FakeStore()
        items = load_fixture_items()
        self._monitor(items, store).run_once()

        partial = items[:2]  # 第三轮“消失”
        client = FakeClient(partial, route_items=items)  # 但线路里还在
        stats = self._monitor(partial, store, client=client).run_once()

        self.assertEqual(stats.locked, 0)
        self.assertEqual(stats.corrected, 1)
        self.assertEqual(store.data[items[2]["hitchId"]].status, "available")

    def test_unknown_route_does_not_lock(self):
        store = FakeStore()
        items = load_fixture_items()
        self._monitor(items, store).run_once()

        partial = items[:2]
        client = FakeClient(partial, route_items=None)  # 线路查不到，无法确认
        stats = self._monitor(partial, store, client=client).run_once()

        self.assertEqual(stats.locked, 0)
        self.assertEqual(store.data[items[2]["hitchId"]].status, "available")

    def test_confirmed_absence_needs_two_passes(self):
        store = FakeStore()
        items = load_fixture_items()
        self._monitor(items, store).run_once()

        partial = items[:2]
        client = FakeClient(partial, route_items=partial)  # 线路里确实没了

        stats1 = self._monitor(partial, store, client=client).run_once()
        self.assertEqual(stats1.locked, 0)  # 第一次只记疑似
        self.assertEqual(store.data[items[2]["hitchId"]].absent_passes, 1)

        stats2 = self._monitor(partial, store, client=client).run_once()
        self.assertEqual(stats2.locked, 1)
        self.assertEqual(store.data[items[2]["hitchId"]].status, "locked")

        # 第三轮：已经锁定的不应重复推送
        stats3 = self._monitor(partial, store, client=client).run_once()
        self.assertEqual(stats3.locked, 0)

    def test_relisted_after_lock(self):
        store = FakeStore()
        items = load_fixture_items()
        self._monitor(items, store).run_once()

        partial = items[:2]
        client = FakeClient(partial, route_items=partial)
        self._monitor(partial, store, client=client).run_once()
        self._monitor(partial, store, client=client).run_once()
        self.assertEqual(store.data[items[2]["hitchId"]].status, "locked")

        # 订单取消，重新出现在列表
        client2 = FakeClient(items, route_items=items)
        stats = self._monitor(items, store, client=client2).run_once()
        self.assertEqual(stats.relisted, 1)
        self.assertEqual(store.data[items[2]["hitchId"]].status, "available")

    def test_expired_is_excluded(self):
        store = FakeStore()
        items = json.loads(json.dumps(load_fixture_items()))
        items[0]["beginTime"] = "2020-01-01"
        items[0]["endTime"] = "2020-01-02"
        self._monitor(items, store).run_once()

        rest = items[1:]
        client = FakeClient(rest, route_items=rest)
        stats = self._monitor(rest, store, client=client).run_once()
        self.assertEqual(stats.expired, 1)
        # 超过保留期（默认 7 天）直接剔除
        self.assertNotIn(items[0]["hitchId"], store.data)


class TestMessage(unittest.TestCase):
    def test_message_contains_original_price_and_route(self):
        item = HitchItem.from_api(load_fixture_items()[0]).merge_detail(
            {"estTotalPrice": "3048", "pickupDept": {"deptName": "张掖机场自助点"}}
        )
        mon = Monitor(dict(BASE_CFG), client=FakeClient([]), store=FakeStore(), notifiers=[])
        msg = mon.build_message([Event("new", item)], ScanStats(total=959))
        self.assertIn("张掖 → 揭阳", msg.markdown)
        self.assertIn("3048", msg.markdown)
        self.assertIn("原价3048", msg.plain)
        self.assertIn("9515098", msg.plain)

    def test_message_truncates_long_list(self):
        items = [HitchItem.from_api(p) for p in load_fixture_items()]
        events = [Event("new", i) for i in items] * 4
        mon = Monitor(dict(BASE_CFG), client=FakeClient([]), store=FakeStore(), notifiers=[])
        msg = mon.build_message(events, ScanStats(total=12))
        self.assertIn("另有", msg.plain)
        self.assertIn("另有", msg.markdown)


class TestStores(unittest.TestCase):
    def test_json_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = JsonStore(path)
            rec = ListingState(hitch_id="1", price=100, pickup_city="张掖", first_seen="t0", last_seen="t0")
            store.save({"1": rec})
            loaded = store.load()
            self.assertEqual(loaded["1"].price, 100)
            self.assertEqual(loaded["1"].pickup_city, "张掖")

    def test_sqlite_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteStore(Path(tmp) / "state.db")
            store.save({"1": ListingState(hitch_id="1", price=50)})
            store.save({"1": ListingState(hitch_id="1", price=1), "2": ListingState(hitch_id="2", price=100)})
            loaded = store.load()
            self.assertEqual(loaded["1"].price, 1)
            self.assertEqual(len(loaded), 2)

    def test_sqlite_marks_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteStore(Path(tmp) / "state.db")
            store.save({"1": ListingState(hitch_id="1", price=50, active=False)})
            self.assertFalse(store.load()["1"].active)


class TestRentals(unittest.TestCase):
    @staticmethod
    def _content(price="24", daily="120", model_id=2011, name="日产轩逸"):
        return {
            "deptHangModels": [
                {
                    "deptId": 498,
                    "deptName": "高新区自助点",
                    "deptDistance": "1.2km",
                    "models": [
                        {
                            "modelId": model_id,
                            "modelGroupId": 2,
                            "modelName": name,
                            "modelDesc": "1.6 自动 | 三厢5座",
                            "modelImgUrl": "https://example.com/car.png",
                            "packagePrice": price,
                            "lowPrice": int(price),
                            "dailyPrice": daily,
                            "bookFlag": True,
                        }
                    ],
                }
            ]
        }

    def test_parse_city_deals_keeps_list_price_as_reference(self):
        rows, depts, models = parse_city_deals(
            self._content(), city="北京", city_id="1", days=30
        )
        self.assertEqual(depts, 1)
        self.assertEqual(models, 1)
        self.assertEqual(rows[0].rent_daily, 24)
        self.assertIsNone(rows[0].calendar_total_fen)
        self.assertEqual(rows[0].list_total_fen, 72000)
        self.assertEqual(rows[0].evidence, "list_reference")
        self.assertEqual(rows[0].dept_id, "498")
        self.assertEqual(rows[0].save_pct, 80)
        self.assertEqual(rows[0].brand, "日产")

    def test_parse_city_deals_filters_unbookable(self):
        content = self._content()
        content["deptHangModels"][0]["models"][0]["bookFlag"] = False
        rows, _, _ = parse_city_deals(content, city="北京", city_id="1", days=7)
        self.assertEqual(rows, [])

    def test_rental_payload_summary_and_leaders(self):
        beijing = RentalCity("北京", "1")
        shanghai = RentalCity("上海", "6")
        beijing.deals[7] = parse_city_deals(
            self._content("86", "127", 1, "科鲁泽"), city="北京", city_id="1", days=7
        )[0]
        shanghai.deals[7] = parse_city_deals(
            self._content("21", "41", 2, "轩逸"), city="上海", city_id="6", days=7
        )[0]
        pickup = datetime(2026, 10, 25, 10, tzinfo=timezone(timedelta(hours=8)))
        payload = build_rental_payload(
            [beijing, shanghai], pickup_at=pickup, requested_cities=2
        )
        self.assertEqual(payload["v"], 3)
        self.assertEqual(payload["summary"]["cities"], 2)
        self.assertEqual(payload["pricing"]["payableAvailable"], False)
        self.assertNotIn("allInTotal", payload)

        def city_of(row):
            meta = next(c for c in payload["cities"] if c["cityId"] == row[0])
            return meta["name"], payload["models"][str(row[1])][0]

        self.assertEqual(payload["periods"]["7"]["minListDaily"], 21)
        self.assertEqual(city_of(payload["leaders"]["7"]["daily"][0])[0], "上海")
        self.assertEqual(city_of(payload["leaders"]["7"]["save"][0])[0], "上海")
        self.assertEqual({city_of(r)[1] for r in payload["deals"]}, {"科鲁泽", "轩逸"})
        self.assertEqual(payload["periods"]["7"]["pickupAt"], "2026-10-25 10:00")
        self.assertEqual(payload["periods"]["7"]["returnAt"], "2026-11-01 10:00")
        validate_rental_payload(payload)

    def test_contract_rejects_all_in_fee_formula(self):
        city = RentalCity("北京", "1")
        city.deals = {30: parse_city_deals(
            self._content("24", "120", 1, "轩逸"), city="北京", city_id="1", days=30
        )[0]}
        pickup = datetime(2026, 10, 25, 10, tzinfo=timezone(timedelta(hours=8)))
        payload = build_rental_payload([city], pickup_at=pickup)
        deal = payload["deals"][0]
        self.assertIsNone(deal[5])
        self.assertNotIn("serviceTotal", payload)
        self.assertNotIn("payableTotal", payload)

    def test_validate_rejects_stale_summary_contradiction(self):
        city = RentalCity("北京", "1", stale=True)
        city.deals = {7: parse_city_deals(
            self._content(), city="北京", city_id="1", days=7
        )[0]}
        pickup = datetime(2026, 10, 25, 10, tzinfo=timezone(timedelta(hours=8)))
        payload = build_rental_payload([city], pickup_at=pickup)
        payload["summary"]["carried"] = 0
        with self.assertRaises(PriceContractError):
            validate_rental_payload(payload)


class TestSubscriptionMail(unittest.TestCase):
    def test_parse_events_and_sanitize_email(self):
        parsed = parse_issue_body(
            '```json\n{"name":"测试","email":"a@example.com","events":["new","bad"],'
            '"pickup_cities":["北京"]}\n```'
        )
        self.assertEqual(parsed["email"], "a@example.com")
        self.assertEqual(parsed["events"], ["new"])

        bad = parse_issue_body(
            '```json\n{"name":"测试","email":"not-an-email","pickup_cities":["北京"]}\n```'
        )
        self.assertNotIn("email", bad)

    def test_backend_mode_enqueues_without_smtp_or_public_issue(self):
        from szhitch.models import HitchItem

        class FakeBackend:
            def __init__(self):
                self.jobs = []

            def enqueue(self, jobs):
                self.jobs.extend(jobs)
                return {"accepted": len(jobs), "skipped": 0}

        mon = Monitor(
            {**BASE_CFG, "notify": {"console": False}},
            client=FakeClient([]), store=FakeStore(), notifiers=[], log=lambda *_: None,
        )
        mon.backend_mode = True
        mon.control_plane = FakeBackend()
        mon.scan_stamp = "20261026213000"
        mon.subscriptions = [{
            "__subscription_id": 7,
            "name": "北京",
            "pickup_cities": ["北京"],
            "events": ["new"],
        }]
        item = HitchItem(hitch_id="9", model_name="轩逸", price=100,
                         pickup_city="北京", return_city="上海")
        mon._email_subscriptions([Event("new", item)], ScanStats(total=1))
        self.assertEqual(len(mon.control_plane.jobs), 1)
        job = mon.control_plane.jobs[0]
        self.assertEqual(job["subscriptionId"], 7)
        self.assertTrue(job["eventKey"].startswith("20261026213000:"))
        self.assertEqual(job["payload"]["events"][0]["price"], 100)

    def test_email_only_goes_to_matching_subscription(self):
        from szhitch.models import HitchItem
        from szhitch.monitor import Event

        mon = Monitor(
            {**BASE_CFG, "notify": {"console": False, "smtp": {
                "host": "smtp.example.com", "user": "u", "password": "p", "to": []
            }}},
            client=FakeClient([]), store=FakeStore(), notifiers=[], log=lambda *_: None,
        )
        beijing = HitchItem(hitch_id="1", model_name="轩逸", price=100,
                            pickup_city="北京", return_city="上海")
        shanghai = HitchItem(hitch_id="2", model_name="朗逸", price=100,
                             pickup_city="上海", return_city="北京")
        mon.subscriptions = [
            {"name": "北京", "email": "bj@example.com", "pickup_cities": ["北京"],
             "__email": "bj@example.com"},
            {"name": "上海", "email": "sh@example.com", "pickup_cities": ["上海"],
             "__email": "sh@example.com"},
        ]
        with patch("szhitch.notify.SmtpNotifier") as fake:
            mon._email_subscriptions([Event("new", beijing), Event("new", shanghai)],
                                     ScanStats(total=2))
        self.assertEqual(fake.call_count, 2)
        recipients = {call.args[0]["to"][0] for call in fake.call_args_list}
        self.assertEqual(recipients, {"bj@example.com", "sh@example.com"})


class TestProxyPool(unittest.TestCase):
    def test_normalize_and_parse(self):
        self.assertEqual(normalize_proxy("1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertEqual(normalize_proxy("http://u:p@1.2.3.4:8080"), "http://u:p@1.2.3.4:8080")
        self.assertEqual(normalize_proxy("socks5://1.2.3.4:1080"), "")
        self.assertEqual(normalize_proxy("not-a-proxy"), "")
        self.assertEqual(
            parse_proxy_list("1.1.1.1:80, 2.2.2.2:81\nhttp://3.3.3.3:82"),
            ["http://1.1.1.1:80", "http://2.2.2.2:81", "http://3.3.3.3:82"],
        )

    def test_rotation_and_cooldown(self):
        pool = ProxyPool(["1.1.1.1:8080"], max_fail=1, cooldown=999)
        self.assertEqual(pool.get(), "http://1.1.1.1:8080")
        pool.report("http://1.1.1.1:8080", False)
        self.assertIsNone(pool.get())
        self.assertEqual(pool.stats()["alive"], 0)

    def test_paid_env_takes_priority(self):
        with patch.dict(os.environ, {"SZHITCH_PROXIES": "http://u:p@2.2.2.2:3128"}, clear=False):
            pool = ProxyPool(["http://1.1.1.1:8080", "http://2.2.2.2:3128"], max_fail=1)
            self.assertEqual(len(pool), 2)
            self.assertEqual(pool.stats()["total"], 2)
            self.assertIn("***", mask("http://u:p@2.2.2.2:3128"))
            self.assertNotIn("u:p", mask("http://u:p@2.2.2.2:3128"))

    def test_city_from_payload_preserves_original_rental_window(self):
        city = RentalCity("北京", "1")
        city.deals[7] = parse_city_deals(
            TestRentals._content("24", "120", 2011, "日产轩逸"),
            city="北京", city_id="1", days=7,
        )[0]
        original = datetime(2026, 10, 20, 10, tzinfo=timezone(timedelta(hours=8)))
        payload = build_rental_payload([city], pickup_at=original, requested_cities=1)
        restored = city_from_payload(payload, "北京")
        self.assertIsNotNone(restored)
        self.assertTrue(restored.stale)
        self.assertEqual(restored.deals[7][0].model, "日产轩逸")
        self.assertEqual(restored.period_meta[7]["pickupAt"], "2026-10-20 10:00")
        self.assertEqual(restored.period_meta[7]["returnAt"], "2026-10-27 10:00")
        later = datetime(2026, 11, 1, 10, tzinfo=timezone(timedelta(hours=8)))
        copied = build_rental_payload([restored], pickup_at=later, requested_cities=1)
        meta = copied["cities"][0]["periods"]["7"]
        self.assertEqual(meta["pickupAt"], "2026-10-20 10:00")
        self.assertEqual(meta["returnAt"], "2026-10-27 10:00")
        self.assertTrue(meta["stale"])


class TestRentalPriceEvidence(unittest.TestCase):
    def test_price_calendar_fixture_matches_declared_total(self):
        data = json.loads(
            (Path(__file__).parent / "fixtures" / "rental" / "price_calendar_terms.json")
            .read_text("utf-8")
        )
        for days in (7, 15, 30):
            parsed = parse_price_calendar(data["terms"][str(days)]["content"], expected_days=days)
            self.assertEqual(parsed.rent_term, days)
            self.assertEqual(parsed.covered_days(), days)
            self.assertEqual(sum(fen for _, fen in parsed.day_prices), parsed.total_fen)

    def test_price_calendar_rejects_inconsistent_sum(self):
        content = {
            "rentStartDay": "2026-10-27",
            "rentEndDay": "2026-10-28",
            "rentTerm": "2",
            "totalDailyPrice": "999",
            "dayPrices": [
                {"priceDate": "2026-10-27", "dayPrice": "148"},
                {"priceDate": "2026-10-28", "dayPrice": "138"},
            ],
        }
        with self.assertRaises(PriceContractError):
            parse_price_calendar(content, expected_days=2)

    def test_money_parser_uses_fen_without_truncation(self):
        self.assertEqual(money_to_fen("24"), 2400)
        self.assertEqual(money_to_fen("24.5"), 2450)
        with self.assertRaises(PriceContractError):
            money_to_fen("24.999")
        with self.assertRaises(PriceContractError):
            money_to_fen("-1")

    def test_list_daily_never_becomes_calendar_total(self):
        data = json.loads(
            (Path(__file__).parent / "fixtures" / "rental" / "price_calendar_terms.json")
            .read_text("utf-8")
        )
        reference = data.get("reference_choose_car") or {}
        matched = reference.get("matched")
        if matched:
            daily = money_to_fen(matched["packagePrice"])
            calendar = parse_price_calendar(data["terms"]["7"]["content"], expected_days=7)
            self.assertNotEqual(daily * 7, calendar.total_fen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
