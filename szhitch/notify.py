"""通知渠道：企业微信 / 飞书 / 钉钉 / Bark / Server酱 / 邮件 / 通用 Webhook / 控制台。

全部使用标准库 urllib 实现，云端部署无需安装任何第三方包。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import smtplib
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Any

TIMEOUT = 15


@dataclass
class Message:
    title: str
    markdown: str
    plain: str
    html: str = ""


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=UTF-8", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


class Notifier:
    name = "base"

    def send(self, msg: Message) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def __str__(self) -> str:
        return self.name


class ConsoleNotifier(Notifier):
    name = "console"

    def send(self, msg: Message) -> None:
        print(f"\n=== {msg.title} ===\n{msg.plain}\n")


class WecomNotifier(Notifier):
    """企业微信群机器人。"""

    name = "wecom"

    def __init__(self, webhook: str) -> None:
        self.webhook = webhook

    def send(self, msg: Message) -> None:
        # 企业微信 markdown 上限 4096 字节，超长截断避免整条丢失
        content = msg.markdown[:4000]
        out = _post_json(self.webhook, {"msgtype": "markdown", "markdown": {"content": content}})
        data = json.loads(out or "{}")
        if data.get("errcode") not in (0, None):
            raise RuntimeError(f"企业微信返回错误: {data}")


class FeishuNotifier(Notifier):
    """飞书自定义机器人（text 消息，无需签名时最简）。"""

    name = "feishu"

    def __init__(self, webhook: str, secret: str = "") -> None:
        self.webhook = webhook
        self.secret = secret

    def send(self, msg: Message) -> None:
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": msg.plain}}
        if self.secret:
            ts = str(int(time.time()))
            key = f"{ts}\n{self.secret}".encode("utf-8")
            sign = base64.b64encode(hmac.new(key, b"", digestmod=hashlib.sha256).digest()).decode()
            payload["timestamp"] = ts
            payload["sign"] = sign
        out = _post_json(self.webhook, payload)
        data = json.loads(out or "{}")
        if data.get("code") not in (0, None):
            raise RuntimeError(f"飞书返回错误: {data}")


class DingTalkNotifier(Notifier):
    """钉钉群自定义机器人，支持加签。"""

    name = "dingtalk"

    def __init__(self, webhook: str, secret: str = "") -> None:
        self.webhook = webhook
        self.secret = secret

    def _signed_url(self) -> str:
        if not self.secret:
            return self.webhook
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{self.secret}".encode("utf-8")
        sign = urllib.parse.quote_plus(
            base64.b64encode(
                hmac.new(self.secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
            )
        )
        sep = "&" if "?" in self.webhook else "?"
        return f"{self.webhook}{sep}timestamp={ts}&sign={sign}"

    def send(self, msg: Message) -> None:
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": msg.title, "text": msg.markdown[:4000]},
        }
        out = _post_json(self._signed_url(), payload)
        data = json.loads(out or "{}")
        if data.get("errcode") not in (0, None):
            raise RuntimeError(f"钉钉返回错误: {data}")


class BarkNotifier(Notifier):
    """Bark（iOS 推送）。key 可含完整地址，如 https://api.day.app/xxx。"""

    name = "bark"

    def __init__(self, key: str, server: str = "https://api.day.app") -> None:
        self.key = key
        self.server = server.rstrip("/")

    def send(self, msg: Message) -> None:
        url = f"{self.server}/push"
        _post_json(
            url,
            {
                "device_key": self.key,
                "title": msg.title,
                "body": msg.plain[:1800],
                "group": "神州顺风车",
                "icon": "https://fimg.zuchecdn.com/upload/app/other/free_ride@2x.png",
            },
        )


class ServerChanNotifier(Notifier):
    """Server酱 Turbo。"""

    name = "serverchan"

    def __init__(self, sendkey: str) -> None:
        self.sendkey = sendkey

    def send(self, msg: Message) -> None:
        url = f"https://sctapi.ftqq.com/{self.sendkey}.send"
        body = urllib.parse.urlencode(
            {"title": msg.title, "desp": msg.markdown[:30000]}
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        if data.get("code") not in (0, None):
            raise RuntimeError(f"Server酱返回错误: {data}")


class SmtpNotifier(Notifier):
    """邮件通知。优先发 HTML 版（利于手机阅读），同时带纯文本兜底。"""

    name = "smtp"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.host = cfg["host"]
        self.port = int(cfg.get("port", 465))
        self.user = cfg["user"]
        self.password = cfg["password"]
        self.sender = cfg.get("from") or self.user
        self.to = cfg["to"] if isinstance(cfg["to"], list) else [cfg["to"]]
        self.ssl = bool(cfg.get("ssl", True))
        self.starttls = bool(cfg.get("starttls", False))

    def _build(self, msg: Message) -> MIMEMultipart:
        mail = MIMEMultipart("alternative")
        mail["Subject"] = msg.title
        mail["From"] = formataddr(("神州顺风车监控", self.sender))
        mail["To"] = ", ".join(self.to)
        mail["Date"] = formatdate(localtime=True)
        mail.attach(MIMEText(msg.plain, "plain", "utf-8"))
        html = msg.html or (
            "<html><body style=\"font-family:-apple-system,Segoe UI,sans-serif\">"
            f"<h2>{msg.title}</h2><pre style=\"white-space:pre-wrap\">{msg.plain}</pre>"
            "</body></html>"
        )
        mail.attach(MIMEText(html, "html", "utf-8"))
        return mail

    def send(self, msg: Message) -> None:
        mail = self._build(msg)
        context = ssl.create_default_context()
        if self.ssl:
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=TIMEOUT) as s:
                s.login(self.user, self.password)
                s.sendmail(self.sender, self.to, mail.as_string())
        else:
            with smtplib.SMTP(self.host, self.port, timeout=TIMEOUT) as s:
                if self.starttls:
                    s.starttls(context=context)
                s.login(self.user, self.password)
                s.sendmail(self.sender, self.to, mail.as_string())


class WebhookNotifier(Notifier):
    """通用 Webhook：POST {"title","text"}，方便接自建服务 / n8n / 各种自动化。"""

    name = "webhook"

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers or {}

    def send(self, msg: Message) -> None:
        _post_json(self.url, {"title": msg.title, "text": msg.plain, "markdown": msg.markdown}, self.headers)


def build_notifiers(cfg: dict[str, Any]) -> list[Notifier]:
    """按配置实例化全部可用渠道。空配置自动跳过。"""
    out: list[Notifier] = []
    cfg = cfg or {}

    if cfg.get("console", True):
        out.append(ConsoleNotifier())
    if wh := cfg.get("wecom_webhook"):
        out.append(WecomNotifier(wh))
    if wh := cfg.get("feishu_webhook"):
        out.append(FeishuNotifier(wh, cfg.get("feishu_secret", "")))
    if cfg.get("dingtalk_webhook"):
        out.append(DingTalkNotifier(cfg["dingtalk_webhook"], cfg.get("dingtalk_secret", "")))
    if key := cfg.get("bark_key"):
        out.append(BarkNotifier(key, cfg.get("bark_server", "https://api.day.app")))
    if key := cfg.get("serverchan_key"):
        out.append(ServerChanNotifier(key))
    if cfg.get("smtp"):
        out.append(SmtpNotifier(cfg["smtp"]))
    if url := cfg.get("webhook_url"):
        out.append(WebhookNotifier(url, cfg.get("webhook_headers")))
    return out
