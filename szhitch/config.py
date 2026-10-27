"""配置加载：默认值 + JSON/YAML + 环境变量覆盖。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "poll": {
        "interval_seconds": 14400,
        "page_size": 200,
        "max_pages": 30,
        "min_request_interval": 1.2,
        "timeout": 20,
        "retries": 3,
    },
    "source": {
        "pickup_city_id": None,
        "return_city_id": None,
    },
    "watch": {
        "pickup_cities": [],
        "return_cities": [],
        "any_cities": [],
        "routes": [],
        "brands": [],
        "exclude_brands": [],
        "models": [],
        "exclude_models": [],
        "max_price": 200,
        "min_price": None,
        "max_price_per_day": None,
        "only_special": False,
        "special_price": 100,
        "min_days": None,
        "max_days": None,
        "date_from": None,
        "date_to": None,
    },
    # 订阅分组：非空时优先于 watch。任一命中即推送，消息里标注订阅名。
    "watch_profiles": [],
    # 订阅中心：从 GitHub Issues 读取订阅（面板「一键创建订阅」生成）。
    # repo 留空时，Actions 里自动取 GITHUB_REPOSITORY；关闭 Issue 即退订。
    "subscribe": {
        "enabled": True,
        # 配置后优先使用私有控制面；内部令牌只从 SZHITCH_INTERNAL_TOKEN 读取。
        "backend_url": "",
        # legacy 公开 Issue 仅在 backend_url 为空时启用；迁移完成后应清空。
        "repo": "",
        "comment_alerts": True,
    },
    # 普通租车长租行情（周租 / 半月 / 月租），与顺风车分开刷新。
    "rental": {
        "enabled": True,
        "periods": [7, 15, 30],
        "cities": [],
        "city_limit": 0,
        "pickup_offset_days": 1,
        "min_request_interval": 0.55,
        "timeout": 20,
        "retries": 2,
        # v3 只保存有证据的价格层级；没有专用接口/订单证据时不生成应付价。
        "pricing_mode": "list_reference",
        # 每城每租期只对列表最便宜的前 N 款做价格日历核验；0 关闭。
        "calendar_verify_per_city": 5,
    },
    "behavior": {
        "first_run": "silent",
        "notify_new": True,
        "notify_relisted": True,
        "notify_price_drop": True,
        "notify_locked": True,
        "notify_closed": False,
        "notify_expired": False,
        "probe_missing_limit": 15,
        "verify_missing": True,
        "verify_missing_limit": 40,
        "confirm_absence_passes": 2,
        "prune_expired_days": 7,
        "enrich_detail": True,
        "detail_limit": 10,
        "dashboard_enrich_limit": 40,
        "max_items_per_message": 8,
        "prune_days": 30,
    },
    "notify": {
        "console": True,
        "wecom_webhook": "",
        "feishu_webhook": "",
        "feishu_secret": "",
        "dingtalk_webhook": "",
        "dingtalk_secret": "",
        "bark_key": "",
        "serverchan_key": "",
        "webhook_url": "",
        "smtp": None,
    },
    "storage": {"type": "sqlite", "path": "data/state.db"},
}

_ENV_MAP = {
    "SZHITCH_WECOM": ("notify", "wecom_webhook"),
    "SZHITCH_FEISHU": ("notify", "feishu_webhook"),
    "SZHITCH_DINGTALK": ("notify", "dingtalk_webhook"),
    "SZHITCH_BARK": ("notify", "bark_key"),
    "SZHITCH_SERVERCHAN": ("notify", "serverchan_key"),
    "SZHITCH_WEBHOOK": ("notify", "webhook_url"),
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取配置。支持 .json / .yaml / .yml；缺省或不存在时返回默认配置。"""
    cfg: dict[str, Any] = {}
    if path:
        p = Path(path)
        if p.exists():
            text = p.read_text("utf-8")
            if p.suffix.lower() in (".yaml", ".yml"):
                try:
                    import yaml  # type: ignore
                except ImportError as exc:  # pragma: no cover
                    raise RuntimeError("读取 YAML 配置需要 PyYAML：pip install pyyaml") from exc
                cfg = yaml.safe_load(text) or {}
            else:
                cfg = json.loads(text)

    merged = _deep_merge(DEFAULT_CONFIG, cfg)

    # 环境变量覆盖（云端部署时把密钥放在环境变量里，不落盘）
    for env, (section, key) in _ENV_MAP.items():
        value = os.environ.get(env)
        if value:
            merged[section][key] = value

    # 邮件：主机存在即视为启用，其余用环境变量补齐（适合 GitHub Actions Secrets）
    smtp_host = os.environ.get("SZHITCH_SMTP_HOST")
    if smtp_host:
        merged["notify"]["smtp"] = {
            "host": smtp_host,
            "port": int(os.environ.get("SZHITCH_SMTP_PORT", "465")),
            "user": os.environ.get("SZHITCH_SMTP_USER", ""),
            "password": os.environ.get("SZHITCH_SMTP_PASS", ""),
            "to": [x.strip() for x in os.environ.get("SZHITCH_SMTP_TO", "").split(",") if x.strip()],
            "from": os.environ.get("SZHITCH_SMTP_FROM") or os.environ.get("SZHITCH_SMTP_USER", ""),
            "ssl": os.environ.get("SZHITCH_SMTP_SSL", "1").lower() not in ("0", "false", "no"),
        }

    state_env = os.environ.get("SZHITCH_STATE")
    if state_env:
        if state_env.startswith(("oss://", "file://", "sqlite://")):
            merged["storage"] = {"type": "auto", "path": state_env, "raw": state_env}
        else:
            merged["storage"]["path"] = state_env

    merged["poll"]["interval_seconds"] = max(120, int(merged["poll"]["interval_seconds"]))
    merged["poll"]["page_size"] = max(1, min(int(merged["poll"]["page_size"]), 200))
    return merged


def dump_default(path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    return p
