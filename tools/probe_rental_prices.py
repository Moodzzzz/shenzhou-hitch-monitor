"""只读核验一款车在 7/15/30 天价格日历中的租金口径。

示例：

    python tools/probe_rental_prices.py \
      --city-id 1 --dept-id 498 --model-id 1906 --pickup "2026-10-27 10:00"

注意：这是价格日历的“日租价”口径，不是订单应付，也不包含服务费、
优惠或押金。需要登录的费用接口不在本工具中猜测或绕过。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from szhitch.gateway import GatewayClient, GatewayError  # noqa: E402
from szhitch.rental_pricing import (  # noqa: E402
    CALENDAR_NOTE,
    LIST_REFERENCE_NOTE,
    parse_price_calendar,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="核验长租价格日历（只读）")
    parser.add_argument("--city-id", required=True, help="pickupCityId，如 1")
    parser.add_argument("--dept-id", required=True, help="pickupDeptId")
    parser.add_argument("--model-id", required=True, help="modelId")
    parser.add_argument("--pickup", required=True, help="取车时间，如 '2026-10-27 10:00'")
    parser.add_argument("--days", default="7,15,30", help="租期天数，逗号分隔")
    parser.add_argument("--save", default="", help="可选：保存脱敏证据 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        pickup = datetime.strptime(args.pickup, "%Y-%m-%d %H:%M")
        days = sorted({int(x.strip()) for x in args.days.split(",") if x.strip()})
        if not days or any(day <= 0 for day in days):
            raise ValueError("租期必须为正整数")
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2

    client = GatewayClient(timeout=25, retries=2, min_interval=1.2)
    evidence = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "condition": {
            "pickupCityId": int(args.city_id),
            "pickupDeptId": int(args.dept_id),
            "modelId": int(args.model_id),
            "pickupDate": pickup.strftime("%Y-%m-%d %H:%M"),
        },
        "notes": {
            "list_reference": LIST_REFERENCE_NOTE,
            "price_calendar": CALENDAR_NOTE,
            "payable": "未取得登录后的订单费用明细，禁止计算应付总额。",
        },
        "terms": {},
    }
    failed = 0
    for day in days:
        return_at = pickup + timedelta(days=day)
        request = {
            "pickupCityId": int(args.city_id),
            "pickupDeptId": int(args.dept_id),
            "modelId": int(args.model_id),
            "pickupDate": pickup.strftime("%Y-%m-%d %H:%M"),
            "returnDate": return_at.strftime("%Y-%m-%d %H:%M"),
            "orderEntrance": 1,
            "allianceBusinessFlag": False,
        }
        try:
            content = client.choose_car_price_calendar(
                pickup_city_id=args.city_id,
                pickup_dept_id=args.dept_id,
                model_id=args.model_id,
                pickup_time=request["pickupDate"],
                return_time=request["returnDate"],
            )
            parsed = parse_price_calendar(content, expected_days=day)
            evidence["terms"][str(day)] = {
                "request": request,
                "content": content,
                "calendarTotal": round(parsed.total_fen / 100, 2),
                "calendarDaily": round(parsed.total_fen / 100 / day, 2),
                "evidence": parsed.evidence,
            }
            print(
                f"{day:>2} 天  日历合计 ¥{parsed.total_fen / 100:.2f}  "
                f"折算 ¥{parsed.total_fen / 100 / day:.2f}/天  "
                f"（日租口径，非应付）"
            )
        except (GatewayError, ValueError) as exc:
            failed += 1
            evidence["terms"][str(day)] = {"request": request, "error": str(exc)}
            print(f"{day:>2} 天  失败: {exc}", file=sys.stderr)

    if args.save:
        target = Path(args.save)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"证据已保存：{target}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
