"""城市字典 / 车型品牌 / 路线方向 / 订阅分组 的离线测试。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from szhitch.catalog import (  # noqa: E402
    BrandCatalog,
    CityIndex,
    load_brands,
    load_cities,
    normalize_city,
    province_of,
)
from szhitch.insights import build_payload  # noqa: E402
from szhitch.models import HitchItem  # noqa: E402
from szhitch.monitor import Event, Monitor, ScanStats  # noqa: E402
from szhitch.rules import ProfileSet, RuleSet, build_profiles, parse_route, route_match  # noqa: E402


def item(frm="成都", to="丽江", model="大众朗逸", price=100, begin=None):
    return HitchItem(
        hitch_id="1", model_name=model, price=price,
        pickup_city=frm, return_city=to,
        begin_date=begin, end_date=None,
    )


class TestCityIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = load_cities()

    def test_dictionary_is_complete(self):
        # 官方 cityList 全量 471 个城市，一个都不能少
        self.assertGreaterEqual(len(self.index), 471)

    def test_suffix_normalization(self):
        self.assertEqual(normalize_city("成都市"), "成都")
        self.assertEqual(normalize_city("阿坝藏族羌族自治州"), "阿坝藏族羌族")
        self.assertEqual(self.index.city_id("成都市"), self.index.city_id("成都"))

    def test_province_from_admin_code(self):
        self.assertEqual(province_of("110100"), "北京")
        self.assertEqual(province_of("510100"), "四川")
        self.assertEqual(self.index.get("成都")["province"], "四川")
        self.assertEqual(self.index.get("呼和浩特")["province"], "内蒙古")

    def test_pinyin_and_initials(self):
        self.assertEqual(self.index.get("呼和浩特")["initials"], "hhht")
        self.assertEqual(self.index.get("重庆")["initials"], "cq")
        self.assertEqual(self.index.get("长沙")["pinyin"], "changsha")

    def test_search_by_chinese_pinyin_and_initials(self):
        self.assertIn("成都", [c["cityName"] for c in self.index.search("成都")])
        self.assertIn("成都", [c["cityName"] for c in self.index.search("chengdu")])
        self.assertIn("成都", [c["cityName"] for c in self.index.search("cd")])
        self.assertEqual(self.index.search("hhht")[0]["cityName"], "呼和浩特")

    def test_search_by_province(self):
        names = [c["cityName"] for c in self.index.search("云南")]
        self.assertIn("丽江", names)

    def test_letter_groups(self):
        groups = self.index.by_letter()
        self.assertIn("A", groups)
        self.assertTrue(all(c["letter"] == "A" for c in groups["A"]))
        total = sum(len(v) for v in groups.values())
        self.assertEqual(total, len(self.index))

    def test_empty_dictionary_is_safe(self):
        empty = CityIndex([])
        self.assertIsNone(empty.city_id("成都"))
        self.assertEqual(empty.search("成都"), [])


class TestBrandCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = load_brands()

    def test_simple_brand(self):
        info = self.cat.parse("理想L7")
        self.assertEqual((info.brand, info.series), ("理想", "L7"))

    def test_year_prefix_stripped(self):
        info = self.cat.parse("2023沃尔沃S90")
        self.assertEqual((info.brand, info.series), ("沃尔沃", "S90"))

    def test_config_suffix_stripped(self):
        info = self.cat.parse("广汽传祺M8尊享版")
        self.assertEqual((info.brand, info.series), ("传祺", "M8"))
        info2 = self.cat.parse("问界M9六座版")
        self.assertEqual((info2.brand, info2.series), ("问界", "M9"))

    def test_longest_alias_wins(self):
        self.assertEqual(self.cat.parse("吉利银河星耀8").brand, "吉利银河")
        self.assertEqual(self.cat.parse("长安深蓝S07").brand, "深蓝")
        self.assertEqual(self.cat.parse("奇瑞捷途旅行者").brand, "捷途")

    def test_unknown_brand(self):
        info = self.cat.parse("某不存在的车X1")
        self.assertEqual(info.brand, "")
        self.assertEqual(info.series, "某不存在的车X1")

    def test_empty_catalog_does_not_crash(self):
        self.assertEqual(BrandCatalog([]).parse("理想L7").series, "理想L7")


class TestRouteRules(unittest.TestCase):
    def test_parse_route_variants(self):
        self.assertEqual(parse_route("成都→丽江"), ("成都", "丽江"))
        self.assertEqual(parse_route("成都->丽江"), ("成都", "丽江"))
        self.assertEqual(parse_route("成都-丽江"), ("成都", "丽江"))
        self.assertEqual(parse_route("成都"), ("成都", "*"))
        self.assertIsNone(parse_route(""))

    def test_direction_is_respected(self):
        # 旧实现把两个城市当子串在整串里找，反向路线会误命中；这里必须方向敏感
        self.assertTrue(route_match(item("成都", "丽江"), "成都→丽江"))
        self.assertFalse(route_match(item("丽江", "成都"), "成都→丽江"))

    def test_wildcards(self):
        self.assertTrue(route_match(item("成都", "丽江"), "成都→*"))
        self.assertFalse(route_match(item("重庆", "丽江"), "成都→*"))
        self.assertTrue(route_match(item("重庆", "丽江"), "*→丽江"))
        self.assertFalse(route_match(item("重庆", "成都"), "*→丽江"))

    def test_single_city_means_pickup(self):
        self.assertTrue(route_match(item("成都", "丽江"), "成都"))
        self.assertFalse(route_match(item("丽江", "成都"), "成都"))

    def test_routes_in_ruleset(self):
        rules = RuleSet(routes=["成都→丽江"])
        self.assertTrue(rules.evaluate(item("成都", "丽江"))[0])
        self.assertFalse(rules.evaluate(item("丽江", "成都"))[0])

    def test_city_suffix_tolerated_in_route(self):
        rules = RuleSet(routes=["成都市→丽江市"])
        self.assertTrue(rules.evaluate(item("成都", "丽江"))[0])


class TestBrandRules(unittest.TestCase):
    def test_brand_whitelist(self):
        rules = RuleSet(brands=["理想"])
        self.assertTrue(rules.evaluate(item(model="理想L7"))[0])
        self.assertFalse(rules.evaluate(item(model="大众朗逸"))[0])

    def test_brand_whitelist_beats_substring(self):
        # 「大众」不应被当成「大众朗逸」以外的任何东西；同时「北京」不该吃掉「北京BJ30」以外车型
        rules = RuleSet(brands=["北京"])
        self.assertTrue(rules.evaluate(item(model="北京BJ30"))[0])
        self.assertFalse(rules.evaluate(item(model="大众途观L"))[0])

    def test_exclude_brand(self):
        rules = RuleSet(exclude_brands=["大众"])
        self.assertFalse(rules.evaluate(item(model="大众朗逸"))[0])
        self.assertTrue(rules.evaluate(item(model="理想L7"))[0])

    def test_any_cities(self):
        rules = RuleSet(any_cities=["丽江"])
        self.assertTrue(rules.evaluate(item("成都", "丽江"))[0])
        self.assertFalse(rules.evaluate(item("成都", "重庆"))[0])

    def test_describe_mentions_new_filters(self):
        text = RuleSet(name="去丽江", routes=["*→丽江"], brands=["理想"]).describe()
        self.assertIn("去丽江", text)
        self.assertIn("*→丽江", text)
        self.assertIn("理想", text)


class TestProfiles(unittest.TestCase):
    def test_no_profiles_falls_back_to_watch(self):
        profiles = build_profiles({"watch": {"pickup_cities": ["成都"]}})
        self.assertEqual(len(profiles), 0)
        ok, name, _ = profiles.evaluate(item("成都", "丽江"))
        self.assertTrue(ok)
        self.assertEqual(name, "")

    def test_named_profile_reported(self):
        profiles = build_profiles({"watch_profiles": [
            {"name": "成都出发", "pickup_cities": ["成都"]},
            {"name": "回丽江", "routes": ["*→丽江"]},
        ]})
        self.assertEqual(len(profiles), 2)
        ok, name, _ = profiles.evaluate(item("成都", "丽江"))
        self.assertTrue(ok)
        self.assertEqual(name, "成都出发")

        ok2, name2, _ = profiles.evaluate(item("重庆", "丽江"))
        self.assertTrue(ok2)
        self.assertEqual(name2, "回丽江")

        ok3, _, reason = profiles.evaluate(item("重庆", "成都"))
        self.assertFalse(ok3)
        self.assertTrue(reason)

    def test_disabled_profile_skipped(self):
        profiles = build_profiles({"watch_profiles": [
            {"name": "停用", "enabled": False, "pickup_cities": ["成都"]},
            {"name": "启用", "routes": ["*→丽江"]},
        ]})
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles.profiles[0].name, "启用")

    def test_profile_without_name_gets_index(self):
        profiles = build_profiles({"watch_profiles": [{"pickup_cities": ["成都"]}]})
        self.assertEqual(profiles.profiles[0].name, "订阅1")


class TestMessageProfile(unittest.TestCase):
    def test_message_marks_hit_profile(self):
        cfg = {"poll": {}, "watch": {}, "notify": {}, "behavior": {}}
        mon = Monitor(cfg, client=object(), store=object(), notifiers=[], log=lambda *_: None)
        ev = Event("new", item("成都", "丽江"))
        ev.profile = "成都出发"
        msg = mon.build_message([ev], ScanStats(total=10))
        self.assertIn("命中订阅", msg.markdown)
        self.assertIn("成都出发", msg.markdown)
        self.assertIn("订阅:成都出发", msg.plain)
        self.assertIn("成都出发", msg.html)


class TestPayload(unittest.TestCase):
    def test_payload_has_full_city_dictionary_and_facets(self):
        items = [item("成都", "丽江", "理想L7"), item("重庆", "三亚", "大众朗逸", 1)]
        payload = build_payload(items, {})
        self.assertEqual(len(payload["city_dict"]), len(load_cities()))
        self.assertEqual(payload["totals"]["city_dict"], len(load_cities()))
        names = {row[1] for row in payload["city_dict"]}
        self.assertIn("成都", names)
        self.assertIn("阿坝", names)  # 当前没车也必须出现在字典里
        self.assertIn("理想", [b["name"] for b in payload["brands"]])
        self.assertTrue(any(r["from"] == "成都" and r["to"] == "丽江" for r in payload["routes"]))

    def test_listing_carries_brand_and_series(self):
        payload = build_payload([item("成都", "丽江", "2023沃尔沃S90")], {})
        row = payload["listings"][0]
        self.assertEqual(row["brand"], "沃尔沃")
        self.assertEqual(row["series"], "S90")

    def test_hit_flag_follows_profiles(self):
        profiles = build_profiles({"watch_profiles": [{"name": "成都", "pickup_cities": ["成都"]}]})
        payload = build_payload([item("成都", "丽江"), item("重庆", "三亚")], {}, profiles=profiles)
        hits = {row["from"]: row.get("hit") for row in payload["listings"]}
        self.assertTrue(hits["成都"])
        self.assertFalse(bool(hits["重庆"]))
        self.assertEqual(payload["watch"]["profiles"][0]["name"], "成都")

    def test_empty_payload_is_stable(self):
        payload = build_payload([], {})
        self.assertEqual(payload["totals"]["live"], 0)
        self.assertEqual(len(payload["city_dict"]), len(load_cities()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
