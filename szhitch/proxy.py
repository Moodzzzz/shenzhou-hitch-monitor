"""代理池：给长租抓取分散出口 IP，降低被限流的概率。

两级来源，按优先级合并：

1. **付费/自有代理**（推荐，稳定）：环境变量 ``SZHITCH_PROXIES``，
   逗号或换行分隔，例如 ``http://user:pass@1.2.3.4:8080``；
   也可以用 ``SZHITCH_PROXY_FILE`` 指向一个每行一条的文件。
2. **免费代理**（默认开启）：从公开列表拉候选，逐个拿神州网关做验活，
   只有真正能打通 ``m.zuche.com`` 的才进池。免费代理存活率通常只有
   5%–15%，适合兜底，不适合当唯一出口。

池子按「成功次数 / 最近延迟」挑，失败两次进入冷却，冷却结束自动复活。
池子空了就退回直连，不会让整轮抓取停摆。
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
CACHE_FILE = ROOT / ".cache" / "proxies.json"

FREE_SOURCES = (
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
)
#: 验活用的最小请求：顺风车列表只要 1 条，够轻。
CHECK_URL = "https://m.zuche.com/api/gw.do?uri=/action/carrctapi/order/hitchList/v1"
CHECK_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/122.0.0.0 Mobile Safari/537.36"
)
_IP_PORT = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})$")


def mask(url: str) -> str:
    """日志里隐藏代理账号密码。"""
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if parts.username:
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://***@{host}{port}"
    return url


@dataclass
class ProxyEntry:
    url: str
    ok: int = 0
    fail: int = 0
    latency: float = 0.0
    cooldown_until: float = 0.0

    @property
    def alive(self) -> bool:
        return time.monotonic() >= self.cooldown_until

    @property
    def score(self) -> float:
        """成功多、延迟低的排前面；没试过的排中间。"""
        base = self.ok - self.fail * 2
        return base - min(self.latency, 30.0) * 0.05

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "ok": self.ok,
            "fail": self.fail,
            "latency": round(self.latency, 2),
        }


class ProxyPool:
    """线程安全的代理轮换池。``get()`` 返回 None 表示走直连。"""

    def __init__(
        self,
        proxies: Iterable[str] = (),
        *,
        cooldown: float = 180.0,
        max_fail: int = 2,
        allow_direct: bool = True,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, ProxyEntry] = {}
        self.cooldown = cooldown
        self.max_fail = max_fail
        self.allow_direct = allow_direct
        self._rr = 0
        for url in proxies:
            self.add(url)

    # ------------------------------------------------------------ 基础操作

    def add(self, url: str) -> None:
        url = normalize_proxy(url)
        if not url:
            return
        with self._lock:
            self._entries.setdefault(url, ProxyEntry(url))

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for e in self._entries.values() if e.alive)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            alive = [e for e in self._entries.values() if e.alive]
            return {
                "total": len(self._entries),
                "alive": len(alive),
                "dead": len(self._entries) - len(alive),
                "best": mask(max(alive, key=lambda e: e.score).url) if alive else "",
            }

    def get(self) -> str | None:
        """挑一个当前可用的代理；全挂了返回 None（直连）。"""
        with self._lock:
            alive = [e for e in self._entries.values() if e.alive]
            if not alive:
                return None
            alive.sort(key=lambda e: e.score, reverse=True)
            # 前几名里随机，既优先用好的，又避免所有线程挤同一个出口
            top = alive[: max(1, min(4, len(alive)))]
            return random.choice(top).url

    def report(self, url: str | None, ok: bool, latency: float = 0.0) -> None:
        if not url:
            return
        with self._lock:
            entry = self._entries.get(url)
            if not entry:
                return
            if ok:
                entry.ok += 1
                entry.fail = 0
                entry.latency = latency if latency > 0 else entry.latency
                entry.cooldown_until = 0.0
            else:
                entry.fail += 1
                if entry.fail >= self.max_fail:
                    entry.cooldown_until = time.monotonic() + self.cooldown

    # ------------------------------------------------------------ 持久化

    def save(self, path: str | Path = CACHE_FILE) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            rows = [e.as_dict() for e in self._entries.values() if e.alive and e.ok > 0]
        target.write_text(
            json.dumps({"saved_at": int(time.time()), "proxies": rows}, ensure_ascii=False),
            encoding="utf-8",
            newline="\n",
        )
        return target

    @classmethod
    def load(cls, path: str | Path = CACHE_FILE, **kwargs: Any) -> "ProxyPool":
        pool = cls(**kwargs)
        try:
            data = json.loads(Path(path).read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return pool
        for row in data.get("proxies") or []:
            url = normalize_proxy(row.get("url") if isinstance(row, dict) else str(row))
            if url:
                pool.add(url)
        return pool


def normalize_proxy(raw: str) -> str:
    """把 ``1.2.3.4:8080`` 补成 ``http://1.2.3.4:8080``。"""
    text = (raw or "").strip()
    if not text or text.startswith("#"):
        return ""
    if "://" not in text:
        if _IP_PORT.match(text):
            return f"http://{text}"
        return ""
    scheme = text.split("://", 1)[0].lower()
    # urllib 只原生支持 http/https 代理，socks 需要额外库，直接跳过
    if scheme not in ("http", "https"):
        return ""
    return text


def parse_proxy_list(text: str) -> list[str]:
    out: list[str] = []
    for chunk in re.split(r"[\s,;]+", text or ""):
        url = normalize_proxy(chunk)
        if url:
            out.append(url)
    return out


def paid_proxies_from_env() -> list[str]:
    """付费/自有代理：环境变量或本地文件，优先级高于免费池。"""
    rows: list[str] = []
    rows.extend(parse_proxy_list(os.environ.get("SZHITCH_PROXIES", "")))
    path = os.environ.get("SZHITCH_PROXY_FILE", "").strip()
    if path:
        try:
            rows.extend(parse_proxy_list(Path(path).read_text("utf-8")))
        except OSError:
            pass
    return rows


def _fetch_text(url: str, timeout: float) -> str:
    """抓文本；urllib 被 TLS 拦时退回 curl（GitHub 源常见）。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": CHECK_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        import subprocess

        out = subprocess.run(
            ["curl", "-sS", "-L", "--max-time", str(int(timeout)), url],
            capture_output=True,
        )
        if out.returncode != 0:
            raise OSError(out.stderr.decode("utf-8", "replace")[:120] or "curl 失败")
        return out.stdout.decode("utf-8", "replace")


def fetch_free_candidates(
    sources: Iterable[str] = FREE_SOURCES,
    *,
    limit: int = 240,
    timeout: float = 20.0,
    log=print,
) -> list[str]:
    """从公开列表拉候选（未验活）。"""
    seen: dict[str, None] = {}
    for url in sources:
        try:
            text = _fetch_text(url, timeout)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"[代理] 拉取失败 {url}: {str(exc)[:80]}")
            continue
        before = len(seen)
        for item in parse_proxy_list(text):
            seen.setdefault(item, None)
            if len(seen) >= limit:
                break
        log(f"[代理] {url.rsplit('/', 1)[-1]} 新增 {len(seen) - before} 条")
        if len(seen) >= limit:
            break
    return list(seen)


def check_proxy(url: str, *, timeout: float = 8.0) -> tuple[bool, float]:
    """用一次真实的网关请求验活，顺带确认能返回业务 JSON。"""
    body = urllib.parse.urlencode(
        {"data": json.dumps({"pageNo": 1, "pageSize": 1}, separators=(",", ":"))}
    ).encode()
    req = urllib.request.Request(
        CHECK_URL,
        data=body,
        method="POST",
        headers={
            "User-Agent": CHECK_UA,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url})
    )
    started = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as resp:
            text = resp.read(4000).decode("utf-8", "replace")
        cost = time.monotonic() - started
        if '"code":1' in text.replace(" ", ""):
            return True, cost
        return False, cost
    except Exception:  # noqa: BLE001 - 代理失败形式五花八门，统一当不可用
        return False, time.monotonic() - started


def build_pool(
    *,
    use_free: bool = True,
    limit: int = 120,
    workers: int = 40,
    timeout: float = 8.0,
    min_pool: int = 6,
    log=print,
) -> ProxyPool:
    """组池：付费代理必收，免费代理验活后收。"""
    paid = paid_proxies_from_env()
    pool = ProxyPool(paid)
    if paid:
        log(f"[代理] 付费/自有代理 {len(paid)} 条（优先使用）")
    if not use_free:
        return pool

    candidates = fetch_free_candidates(limit=limit, log=log)
    if not candidates:
        log("[代理] 没有拿到免费候选")
        return pool

    log(f"[代理] 验活 {len(candidates)} 条免费代理…")
    good = 0
    with ThreadPoolExecutor(max_workers=max(4, workers)) as pool_exec:
        futures = {pool_exec.submit(check_proxy, url, timeout=timeout): url for url in candidates}
        for future in as_completed(futures):
            url = futures[future]
            try:
                ok, cost = future.result()
            except Exception:  # noqa: BLE001
                continue
            if ok:
                pool.add(url)
                pool.report(url, True, cost)
                good += 1
    log(f"[代理] 免费代理可用 {good}/{len(candidates)}")
    if good < min_pool:
        log(f"[代理] 可用数量少于 {min_pool}，本轮以直连为主")
    return pool


def pool_from_config(cfg: dict[str, Any] | None, *, log=print) -> ProxyPool | None:
    """按配置准备代理池；未启用或可用太少时返回 None（纯直连）。"""
    proxy_cfg = (cfg or {}).get("proxy") or {}
    if not proxy_cfg.get("enabled"):
        return None
    min_pool = int(proxy_cfg.get("min_pool", 6))
    paid = paid_proxies_from_env()
    if proxy_cfg.get("cache", True):
        cached = ProxyPool.load()
        if len(cached) >= min_pool:
            log(f"[代理] 复用缓存代理 {len(cached)} 条")
            return cached
    pool = build_pool(
        use_free=bool(proxy_cfg.get("free", True)),
        limit=int(proxy_cfg.get("free_limit", 120)),
        workers=int(proxy_cfg.get("check_workers", 40)),
        timeout=float(proxy_cfg.get("check_timeout", 8)),
        min_pool=min_pool,
        log=log,
    )
    if len(pool):
        pool.save()
    # 可用出口太少反而更容易失败：没有付费代理时，达不到下限就直接直连
    if len(pool) >= min_pool or (paid and len(pool)) or proxy_cfg.get("force"):
        return pool
    log(f"[代理] 可用 {len(pool)} 个，少于下限 {min_pool}，本轮直连")
    return None
