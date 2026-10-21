#!/usr/bin/env python
"""生成 ``data/cities.json``：城市全量字典 + 拼音 + 首字母 + 省份。

数据源是神州租车网关的 ``/action/carrctapi/order/cityList/v1``，
返回 ``hotCities``（24 个热门）与 ``allCities``（471 个全量），按 cityId 合并。

补的字段：
* ``province``：城市字段里没有省份，用 ``code``（6 位行政区划码）前两位推导；
* ``pinyin``：整名全拼（小写、无声调），用于拼音搜索；
* ``initials``：逐字首字母（呼和浩特 → hhht），用于首字母筛选；
* ``letter``：分组字母（A-Z / #），面板左侧索引条用它。

pypinyin 只在**生成字典**时用，运行期不依赖：
    pip install pypinyin && python tools/build_cities.py

已有拼音会被保留，只更新城市列表时分组不会漂移。

用法：
    python tools/build_cities.py                 # 联网抓最新
    python tools/build_cities.py --from-file x.json
    python tools/build_cities.py --dry-run       # 只看统计不写文件
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from szhitch.catalog import (  # noqa: E402
    CITY_FILE,
    collect_city_payload,
    enrich_cities,
    save_cities,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成城市全量字典")
    ap.add_argument("--from-file", help="离线：从保存的 cityList 响应 JSON 读取")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.from_file:
        raw = json.loads(Path(args.from_file).read_text("utf-8"))
        raw = raw.get("content", raw)
    else:
        from szhitch.gateway import GatewayClient

        raw = GatewayClient(timeout=25, min_interval=0.6).cities()

    previous: dict[str, dict] = {}
    if CITY_FILE.exists():
        try:
            old = json.loads(CITY_FILE.read_text("utf-8"))
            previous = {str(c.get("cityId")): c for c in (old.get("cities") or [])}
        except (OSError, json.JSONDecodeError):
            previous = {}

    cities = enrich_cities(collect_city_payload(raw), previous)
    letters: dict[str, int] = {}
    provinces: set[str] = set()
    no_pinyin = 0
    for c in cities:
        letters[c["letter"]] = letters.get(c["letter"], 0) + 1
        provinces.add(c["province"])
        if not c["pinyin"]:
            no_pinyin += 1

    print(f"城市 {len(cities)} 个 · 省份 {len(provinces)} 个 · 字母组 {len(letters)} 个")
    print("  " + " ".join(f"{k}:{v}" for k, v in sorted(letters.items())))
    if no_pinyin:
        print(f"⚠ {no_pinyin} 个城市没有拼音（未安装 pypinyin？）", file=sys.stderr)

    if args.dry_run:
        print(json.dumps(cities[:5], ensure_ascii=False, indent=1))
        return 0

    path = save_cities(cities)
    print(f"已写入 {path}（{path.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
