"""腾讯云函数（SCF）入口。

部署方式：项目根目录打包上传，执行方法填 ``cloud.tencent_scf.main_handler.main_handler``。
定时触发器建议 4 小时一次（cron: ``0 0 */4 * * * *``）。

环境变量同阿里云版本：SZHITCH_CONFIG / SZHITCH_STATE / SZHITCH_WECOM …
注意：SCF 实例的 /tmp 不保证跨实例保留，状态务必放在 COS/OSS 或自建存储上，
否则每一轮都会把全部在售车当成「新上线」。
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


def main_handler(event, context):  # noqa: ANN001, ARG001
    cfg = load_config(os.environ.get("SZHITCH_CONFIG") or (_ROOT / "config.json"))
    os.chdir(_ROOT)
    stats = Monitor(cfg, log=lambda m: print(m, flush=True)).run_once()
    result = stats.as_dict()
    print(json.dumps(result, ensure_ascii=False))
    return result
