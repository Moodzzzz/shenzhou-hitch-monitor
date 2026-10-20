"""阿里云函数计算（FC）入口。

部署方式：把项目根目录整体打包上传，请求处理程序填 ``cloud.aliyun_fc.handler.handler``。
定时触发器建议 4 小时一次（cron 表达式如 ``0 0 */4 * * *``）。

环境变量：
  SZHITCH_CONFIG   配置文件路径（默认 config.json）
  SZHITCH_STATE    状态位置，函数实例无状态，必须用外部存储，如 oss://bucket/szhitch/state.json
  SZHITCH_WECOM    企业微信机器人 webhook（也可写在配置里）
  OSS_ENDPOINT / OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET  （用 oss:// 时需要）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from szhitch.config import load_config  # noqa: E402
from szhitch.monitor import Monitor  # noqa: E402


def handler(event, context):  # noqa: ANN001, ARG001
    cfg = load_config(os.environ.get("SZHITCH_CONFIG") or (_ROOT / "config.json"))
    os.chdir(_ROOT)  # 让 storage 里的相对路径稳定
    stats = Monitor(cfg, log=lambda m: print(m, flush=True)).run_once()
    result = stats.as_dict()
    print(json.dumps(result, ensure_ascii=False))
    return json.dumps(result, ensure_ascii=False)
