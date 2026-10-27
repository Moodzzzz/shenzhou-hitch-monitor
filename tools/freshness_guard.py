#!/usr/bin/env python3
"""触发去重：定时器撞车时，数据还新就直接跳过本轮。

面板数据有三路触发源（GitHub schedule、本机计划任务、cron-job.org），
彼此只差几分钟就会重复抓一遍。这里用 dashboard.json 的 generated_at 判断：
非手动触发、且数据不到 MIN_AGE_MIN 分钟，就不重复跑。

输出写进 GITHUB_OUTPUT（skip / reason），工作流后续步骤据此跳过。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

MIN_AGE_MIN = float(os.environ.get("SZHITCH_MIN_AGE_MIN", "25"))
DASHBOARD = Path(os.environ.get("SZHITCH_DASHBOARD", "dashboard/dashboard.json"))
STAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
)


def payload_age_minutes(path: Path = DASHBOARD) -> tuple[float | None, str]:
    """返回（距今分钟数, 原始时间戳或错误说明）；无法判断时年龄为 None。"""
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception as exc:  # 文件缺失、JSON 损坏都算“判断不了”
        return None, f"读不到 {path}：{exc}"
    stamp = str(data.get("generated_at") or "").strip()
    if not stamp:
        return None, "generated_at 为空"
    when = None
    for fmt in STAMP_FORMATS:
        try:
            when = dt.datetime.strptime(stamp, fmt)
            break
        except ValueError:
            continue
    if when is None:
        return None, f"generated_at 格式不认识：{stamp}"
    if when.tzinfo is None:  # 面板时间戳是北京时间
        when = when.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    delta = dt.datetime.now(dt.timezone.utc) - when.astimezone(dt.timezone.utc)
    return delta.total_seconds() / 60, stamp


def main() -> int:
    event = (os.environ.get("GITHUB_EVENT_NAME") or "").strip()
    source = (os.environ.get("SZHITCH_TRIGGER_SOURCE") or "").strip().lower()
    if not source:  # 定时事件没传 inputs，按调度器算
        source = "scheduler" if event == "schedule" else "manual"
    age, info = payload_age_minutes()

    skip = False
    if event == "push" or source == "manual":
        reason = f"手动 / 推送触发（event={event or '本地'}, source={source}），不拦"
    elif age is None:
        reason = f"判断不了新鲜度，照常运行（{info}）"
    elif age < MIN_AGE_MIN:
        skip = True
        reason = f"数据 {age:.1f} 分钟前刚更新（阈值 {MIN_AGE_MIN:g} 分钟），跳过本轮"
    else:
        reason = f"数据已 {age:.1f} 分钟未更新，照常运行"

    print(f"[guard] {reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"skip={'true' if skip else 'false'}\n")
            fh.write(f"reason={reason}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
