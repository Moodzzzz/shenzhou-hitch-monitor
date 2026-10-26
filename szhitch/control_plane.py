"""私有控制面客户端：只读已验证订阅规则，写入幂等通知任务。

令牌只读环境变量，不写配置或日志；HTTP 错误不比本地规则更危险：
发送失败只记录，不回退到公开 Issue 或 SMTP。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

TIMEOUT = 20


class ControlPlaneError(RuntimeError):
    pass


class ControlPlaneClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "szhitch-monitor/control-plane",
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ControlPlaneError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ControlPlaneError(f"网络错误: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ControlPlaneError("控制面返回非 JSON") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise ControlPlaneError("控制面返回失败")
        return payload

    def subscriptions(self) -> list[dict[str, Any]]:
        payload = self._request("/internal/v1/subscriptions")
        rows = payload.get("subscriptions")
        if not isinstance(rows, list):
            raise ControlPlaneError("订阅列表格式错误")
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("rules"), dict):
                continue
            rules = dict(row["rules"])
            rules["__subscription_id"] = int(row.get("id") or 0)
            rules["__source"] = "control-plane"
            out.append(rules)
        return out

    def enqueue(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        if not jobs:
            return {"accepted": 0, "skipped": 0}
        payload = self._request("/internal/v1/outbox", method="POST", body={"jobs": jobs})
        return {
            "accepted": int(payload.get("accepted") or 0),
            "skipped": int(payload.get("skipped") or 0),
        }


def from_config(cfg: dict[str, Any]) -> ControlPlaneClient | None:
    subscribe = cfg.get("subscribe") or {}
    base = str(
        os.environ.get("SZHITCH_CONTROL_PLANE_URL")
        or subscribe.get("backend_url")
        or ""
    ).strip().rstrip("/")
    token = str(os.environ.get("SZHITCH_INTERNAL_TOKEN") or "").strip()
    if not base or not token:
        return None
    if not base.startswith("https://"):
        raise ControlPlaneError("控制面必须使用 HTTPS")
    return ControlPlaneClient(
        base, token, timeout=float((cfg.get("poll") or {}).get("timeout", 20)),
    )
