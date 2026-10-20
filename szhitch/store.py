"""状态存储：记录每条顺风车的生命周期、以及新车到达的历史。

状态机（依据实测：列表接口只返回「可预订」的车，没有状态字段）：

    available ──从列表消失、工单仍存在──▶ locked（被锁定，别人已下单）
        │                                   │
        │                                   └─重新出现在列表─▶ available（订单取消，回到订单库）
        └──从列表消失、详情报「工单不存在」──▶ closed（已关闭，剔除）

    available/locked ──还车日期已过──▶ expired（过期，剔除）

三种后端：
  * sqlite://data/state.db  —— 本机 / VPS 常驻
  * file://data/state.json  —— GitHub Actions、容器
  * oss://bucket/key.json   —— 阿里云函数计算
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ISO = "%Y-%m-%dT%H:%M:%SZ"

STATUS_AVAILABLE = "available"
STATUS_LOCKED = "locked"
STATUS_CLOSED = "closed"
STATUS_EXPIRED = "expired"

ACTIVE_STATUSES = (STATUS_AVAILABLE, STATUS_LOCKED)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime(ISO)


@dataclass
class ListingState:
    hitch_id: str
    price: int
    signature: str = ""
    model_name: str = ""
    pickup_city: str = ""
    return_city: str = ""
    begin_date: str = ""
    end_date: str = ""
    first_seen: str = ""
    last_seen: str = ""
    active: bool = True
    status: str = STATUS_AVAILABLE
    gone_since: str = ""
    orig_price: int = 0  # 缓存的日租原价（estTotalPrice），用于面板折扣对比
    absent_passes: int = 0  # 连续缺席轮次，用于避免单次漏抓误判锁定

    @classmethod
    def from_record(cls, hitch_id: str, rec: dict[str, Any]) -> "ListingState":
        return cls(
            hitch_id=str(hitch_id),
            price=int(rec.get("price") or 0),
            signature=str(rec.get("signature") or ""),
            model_name=str(rec.get("model_name") or ""),
            pickup_city=str(rec.get("pickup_city") or ""),
            return_city=str(rec.get("return_city") or ""),
            begin_date=str(rec.get("begin_date") or ""),
            end_date=str(rec.get("end_date") or ""),
            first_seen=str(rec.get("first_seen") or ""),
            last_seen=str(rec.get("last_seen") or ""),
            active=bool(rec.get("active", True)),
            status=str(rec.get("status") or STATUS_AVAILABLE),
            gone_since=str(rec.get("gone_since") or ""),
            orig_price=int(rec.get("orig_price") or 0),
            absent_passes=int(rec.get("absent_passes") or 0),
        )

    def as_record(self) -> dict[str, Any]:
        return asdict(self)

    def is_expired(self, today: date | None = None) -> bool:
        """还车日期早于今天即为过期（顺风车按自然日计）。"""
        if not self.end_date:
            return False
        today = today or date.today()
        try:
            return date.fromisoformat(self.end_date) < today
        except ValueError:
            return False


class BaseStore:
    def load(self) -> dict[str, ListingState]:
        raise NotImplementedError

    def save(self, records: dict[str, ListingState]) -> None:
        raise NotImplementedError

    def record_arrivals(self, pairs: Iterable[tuple[str, str]]) -> None:
        """记录「某城市在某时刻上新了一条」，供热力图与预报使用。"""

    def load_arrivals(self, days: int = 90) -> list[tuple[str, str]]:
        return []

    def log_event(self, kind: str, item: Any, extra: dict | None = None) -> None:
        """可选的事件流水，默认不做事。"""

    def prune(self, days: int = 30) -> None:
        """清理已过期/已关闭且超过保留期的车，控制库体积。"""


# --------------------------------------------------------------------- SQLite


class SqliteStore(BaseStore):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # 每次操作现开现关：避免长连接在 Windows 下锁住 -wal/-shm 文件
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS listings (
                    hitch_id    TEXT PRIMARY KEY,
                    price       INTEGER,
                    signature   TEXT,
                    model_name  TEXT,
                    pickup_city TEXT,
                    return_city TEXT,
                    begin_date  TEXT,
                    end_date    TEXT,
                    first_seen  TEXT,
                    last_seen   TEXT,
                    active      INTEGER DEFAULT 1,
                    status      TEXT DEFAULT 'available',
                    gone_since  TEXT DEFAULT '',
                    orig_price  INTEGER DEFAULT 0,
                    absent_passes INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS arrivals (
                    ts   TEXT,
                    city TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_arrivals_ts ON arrivals(ts);
                CREATE TABLE IF NOT EXISTS events (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       TEXT,
                    kind     TEXT,
                    hitch_id TEXT,
                    price    INTEGER,
                    detail   TEXT
                );
                """
            )
            # 兼容旧库：补列
            cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
            if "status" not in cols:
                conn.execute("ALTER TABLE listings ADD COLUMN status TEXT DEFAULT 'available'")
            if "gone_since" not in cols:
                conn.execute("ALTER TABLE listings ADD COLUMN gone_since TEXT DEFAULT ''")
            if "orig_price" not in cols:
                conn.execute("ALTER TABLE listings ADD COLUMN orig_price INTEGER DEFAULT 0")
            if "absent_passes" not in cols:
                conn.execute("ALTER TABLE listings ADD COLUMN absent_passes INTEGER DEFAULT 0")

    def load(self) -> dict[str, ListingState]:
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT hitch_id, price, signature, model_name, pickup_city, return_city,"
                " begin_date, end_date, first_seen, last_seen, active, status, gone_since,"
                " orig_price, absent_passes FROM listings"
            ).fetchall()
        out: dict[str, ListingState] = {}
        for r in rows:
            st = ListingState.from_record(
                r[0],
                {
                    "price": r[1], "signature": r[2], "model_name": r[3],
                    "pickup_city": r[4], "return_city": r[5], "begin_date": r[6],
                    "end_date": r[7], "first_seen": r[8], "last_seen": r[9],
                    "active": bool(r[10]), "status": r[11], "gone_since": r[12],
                    "orig_price": r[13], "absent_passes": r[14],
                },
            )
            out[st.hitch_id] = st
        return out

    def save(self, records: dict[str, ListingState]) -> None:
        payload = [
            (s.hitch_id, s.price, s.signature, s.model_name, s.pickup_city, s.return_city,
             s.begin_date, s.end_date, s.first_seen, s.last_seen, 1 if s.active else 0,
             s.status, s.gone_since, s.orig_price, s.absent_passes)
            for s in records.values()
        ]
        with self._lock, closing(self._connect()) as conn, conn:
            conn.executemany(
                """INSERT INTO listings (hitch_id, price, signature, model_name, pickup_city,
                        return_city, begin_date, end_date, first_seen, last_seen, active,
                        status, gone_since, orig_price, absent_passes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(hitch_id) DO UPDATE SET
                        price=excluded.price, signature=excluded.signature,
                        model_name=excluded.model_name, pickup_city=excluded.pickup_city,
                        return_city=excluded.return_city, begin_date=excluded.begin_date,
                        end_date=excluded.end_date, last_seen=excluded.last_seen,
                        active=excluded.active, status=excluded.status,
                        gone_since=excluded.gone_since, orig_price=excluded.orig_price,
                        absent_passes=excluded.absent_passes""",
                payload,
            )

    def record_arrivals(self, pairs: Iterable[tuple[str, str]]) -> None:
        rows = [(ts, city) for ts, city in pairs if city]
        if not rows:
            return
        with self._lock, closing(self._connect()) as conn, conn:
            conn.executemany("INSERT INTO arrivals (ts, city) VALUES (?,?)", rows)

    def load_arrivals(self, days: int = 90) -> list[tuple[str, str]]:
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT ts, city FROM arrivals WHERE ts >= datetime('now', ?)",
                (f"-{int(days)} days",),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def log_event(self, kind: str, item: Any, extra: dict | None = None) -> None:
        signature = ""
        sig = getattr(item, "signature", None)
        if callable(sig):
            try:
                signature = sig()
            except Exception:
                signature = ""
        detail = json.dumps({"signature": signature, "extra": extra or {}}, ensure_ascii=False)
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO events (ts, kind, hitch_id, price, detail) VALUES (?,?,?,?,?)",
                (utcnow(), kind, getattr(item, "hitch_id", ""), getattr(item, "price", 0), detail),
            )

    def prune(self, days: int = 30) -> None:
        """硬删除：已过期/已关闭且超过保留期的记录；顺带清理旧到达日志。"""
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "DELETE FROM listings WHERE status IN ('expired','closed')"
                " AND last_seen < datetime('now', ?)",
                (f"-{int(days)} days",),
            )
            conn.execute("DELETE FROM arrivals WHERE ts < datetime('now', '-180 days')")


# ----------------------------------------------------------------- JSON 文件


class JsonStore(BaseStore):
    """原子写入的 JSON 状态文件，适合 GitHub Actions / 容器。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def load(self) -> dict[str, ListingState]:
        data = self._read()
        return {
            k: ListingState.from_record(k, v)
            for k, v in (data.get("listings") or {}).items()
        }

    def save(self, records: dict[str, ListingState]) -> None:
        data = self._read()  # 保留 arrivals 等其它字段
        data.update(
            {
                "version": 2,
                "updated_at": utcnow(),
                "listings": {k: v.as_record() for k, v in records.items()},
            }
        )
        self._atomic_write(json.dumps(data, ensure_ascii=False))

    def record_arrivals(self, pairs: Iterable[tuple[str, str]]) -> None:
        new_rows = [[ts, city] for ts, city in pairs if city]
        if not new_rows:
            return
        data = self._read()
        rows: list = data.get("arrivals") or []
        rows.extend(new_rows)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        kept: list = []
        for row in rows:
            ts = _parse_iso_local(row[0])
            if ts and (now - ts).days <= 180:
                kept.append(row)
        kept = (kept or rows)[-20000:]
        data["arrivals"] = kept
        self._atomic_write(json.dumps(data, ensure_ascii=False))

    def load_arrivals(self, days: int = 90) -> list[tuple[str, str]]:
        data = self._read()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        out: list[tuple[str, str]] = []
        for row in data.get("arrivals") or []:
            ts = _parse_iso_local(row[0])
            if ts and (now - ts).days <= days:
                out.append((row[0], row[1]))
        return out

    def _atomic_write(self, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def _parse_iso_local(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, ISO)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------- 对象存储


class OssJsonStore(JsonStore):
    """阿里云 OSS 上的状态文件。需要 pip install oss2。"""

    def __init__(self, uri: str, *, cache_path: str | Path) -> None:
        super().__init__(cache_path)
        try:
            import oss2  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "oss:// 存储需要 oss2，请先执行 pip install oss2 并配置 OSS_* 环境变量"
            ) from exc
        _, _, rest = uri.partition("oss://")
        bucket_name, _, key = rest.partition("/")
        self.bucket = oss2.Bucket(
            oss2.EnvAuth(),
            os.environ.get("OSS_ENDPOINT", ""),
            bucket_name,
        )
        self.key = key
        self.fetch()

    def fetch(self) -> None:
        try:
            self.bucket.get_object_to_file(self.key, str(self.path))
        except Exception:  # 首次运行对象不存在，忽略
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, records: dict[str, ListingState]) -> None:
        super().save(records)
        self.bucket.put_object_from_file(self.key, str(self.path))

    def record_arrivals(self, pairs: Iterable[tuple[str, str]]) -> None:
        super().record_arrivals(pairs)
        self.bucket.put_object_from_file(self.key, str(self.path))


# --------------------------------------------------------------------- 工厂


def create_store(spec: str | dict[str, Any]) -> BaseStore:
    """spec 可以是 ``sqlite://path`` / ``file://path`` / ``oss://bucket/key`` 或配置字典。"""
    if isinstance(spec, dict):
        kind = str(spec.get("type") or "sqlite")
        if kind == "oss":
            return OssJsonStore(
                str(spec["uri"]),
                cache_path=spec.get("cache_path", "/tmp/szhitch-state.json"),
            )
        target = spec.get("path") or ("data/state.db" if kind == "sqlite" else "data/state.json")
        return SqliteStore(target) if kind == "sqlite" else JsonStore(target)

    text = str(spec or "sqlite://data/state.db")
    if text.startswith("oss://"):
        return OssJsonStore(
            text, cache_path=os.environ.get("SZHITCH_OSS_CACHE", "/tmp/szhitch-state.json")
        )
    if text.startswith("file://"):
        return JsonStore(text[len("file://"):])
    if text.startswith("sqlite://"):
        return SqliteStore(text[len("sqlite://"):])
    return SqliteStore(text)
