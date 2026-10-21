"""城市与车型目录。

两件事：
1. **城市**：把官方 ``cityList`` 的 471 个城市整理成可搜索的结构 ——
   中文名 / 全拼 / 首字母 / 省份 / 字母分组都能命中，面板用它渲染选择器。
2. **车型**：把 ``modelName`` 拆成「品牌 + 车系」，让筛选可以按品牌（大众、理想）
   而不是靠字符串包含，也顺手剥掉 ``2023`` 这种年份前缀和「尊享版」这类配置后缀。

全部为标准库实现，云端不需要任何第三方依赖。
拼音与首字母在 ``tools/build_cities.py`` 里用 pypinyin 离线生成后写进
``data/cities.json``，运行期只读字典。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CITY_FILE = DATA_DIR / "cities.json"
BRAND_FILE = DATA_DIR / "brands.json"

#: 行政区划码前两位 → 省级行政区。城市字典里没有省份字段，用 code 推导。
PROVINCE_BY_CODE = {
    "11": "北京", "12": "天津", "13": "河北", "14": "山西", "15": "内蒙古",
    "21": "辽宁", "22": "吉林", "23": "黑龙江",
    "31": "上海", "32": "江苏", "33": "浙江", "34": "安徽", "35": "福建",
    "36": "江西", "37": "山东",
    "41": "河南", "42": "湖北", "43": "湖南", "44": "广东", "45": "广西", "46": "海南",
    "50": "重庆", "51": "四川", "52": "贵州", "53": "云南", "54": "西藏",
    "61": "陕西", "62": "甘肃", "63": "青海", "64": "宁夏", "65": "新疆",
    "71": "台湾", "81": "香港", "82": "澳门",
}

#: 直辖市：省份名与城市名相同，分组时不必重复显示
MUNICIPALITIES = {"北京", "天津", "上海", "重庆"}

LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + ["#"]

_CITY_SUFFIXES = ("市", "地区", "自治州", "盟", "县")


def normalize_city(name: str) -> str:
    """去掉「市 / 地区 / 自治州 / 盟 / 县」后缀，便于「成都」与「成都市」互认。"""
    text = (name or "").strip()
    for suffix in _CITY_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            text = text[: -len(suffix)]
    return text


def province_of(code: str) -> str:
    return PROVINCE_BY_CODE.get(str(code or "")[:2], "")


def letter_of(pinyin: str, name: str = "") -> str:
    """分组字母：取拼音首字母，非 A-Z 归到 ``#``。"""
    py = (pinyin or "").strip()
    if py:
        head = py[0].upper()
        if head in LETTERS:
            return head
    head = (name or "").strip()[:1].upper()
    return head if head in LETTERS else "#"


# --------------------------------------------------------------------- 城市


class CityIndex:
    """城市字典的检索索引。

    ``cities`` 的每一项至少含 ``cityId`` 与 ``cityName``；带 ``pinyin`` /
    ``initials`` / ``province`` / ``letter`` 时搜索能力更完整。
    """

    def __init__(self, cities: Iterable[dict[str, Any]]) -> None:
        self.cities: list[dict[str, Any]] = [dict(c) for c in cities if c.get("cityName")]
        self._by_id = {str(c.get("cityId")): c for c in self.cities}
        self._by_name: dict[str, dict[str, Any]] = {}
        self._by_norm: dict[str, dict[str, Any]] = {}
        self._blobs: dict[str, str] = {}

        for city in self.cities:
            name = str(city["cityName"]).strip()
            city.setdefault("province", province_of(str(city.get("code", ""))))
            city.setdefault("letter", letter_of(str(city.get("pinyin", "")), name))
            # 同名城市（极少）保留第一个，避免覆盖热门城市
            self._by_name.setdefault(name, city)
            self._by_norm.setdefault(normalize_city(name), city)
            self._blobs[str(city.get("cityId"))] = " ".join(
                str(x).lower()
                for x in (
                    name,
                    city.get("pinyin", ""),
                    city.get("initials", ""),
                    city.get("enName", ""),
                    city.get("province", ""),
                )
                if x
            )

    # -- 查询 ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self.cities)

    def get(self, name: str) -> dict[str, Any] | None:
        text = (name or "").strip()
        if not text:
            return None
        return self._by_name.get(text) or self._by_norm.get(normalize_city(text))

    def city_id(self, name: str) -> str | None:
        city = self.get(name)
        return str(city.get("cityId")) if city else None

    def search(self, query: str, *, limit: int = 60) -> list[dict[str, Any]]:
        """按中文名 / 全拼 / 首字母 / 省份搜索。

        排序优先级：完全相等 > 名称前缀 > 拼音/首字母前缀 > 包含。
        """
        q = (query or "").strip().lower()
        if not q:
            return self.cities[:limit]

        scored: list[tuple[int, str, dict[str, Any]]] = []
        for city in self.cities:
            name = str(city["cityName"])
            pinyin = str(city.get("pinyin", "")).lower()
            initials = str(city.get("initials", "")).lower()
            province = str(city.get("province", ""))

            if q == name.lower() or q == pinyin:
                rank = 0
            elif name.lower().startswith(q) or initials.startswith(q):
                rank = 1
            elif pinyin.startswith(q):
                rank = 2
            elif q in name.lower() or q in province.lower():
                rank = 3
            elif q in pinyin or q in initials:
                rank = 4
            else:
                continue
            scored.append((rank, name, city))

        scored.sort(key=lambda x: (x[0], len(x[1]), x[1]))
        return [c for _, _, c in scored[:limit]]

    def by_letter(self) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {k: [] for k in LETTERS}
        for city in self.cities:
            groups.setdefault(str(city.get("letter") or "#"), []).append(city)
        for bucket in groups.values():
            bucket.sort(key=lambda c: str(c.get("pinyin") or c["cityName"]))
        return {k: v for k, v in groups.items() if v}

    def to_payload(self, *, counts: dict[str, int] | None = None) -> list[list[Any]]:
        """面板用的紧凑数组：``[id, 名称, 拼音, 首字母, 省份, 字母, 条数]``。"""
        counts = counts or {}
        return [
            [
                str(c.get("cityId", "")),
                c["cityName"],
                c.get("pinyin", ""),
                c.get("initials", ""),
                c.get("province", ""),
                c.get("letter", "#"),
                int(counts.get(c["cityName"], 0)),
            ]
            for c in self.cities
        ]


def load_cities(path: str | Path | None = None) -> CityIndex:
    p = Path(path) if path else CITY_FILE
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return CityIndex([])
    cities = data.get("cities") if isinstance(data, dict) else data
    return CityIndex(cities or [])


# ------------------------------------------------------- 字典构建（离线/刷新共用）

CITY_FIELDS = ("cityId", "cityName", "enName", "code")


def collect_city_payload(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """合并 ``hotCities`` 与 ``allCities``，按 cityId 去重（热门优先）。"""
    merged: dict[str, dict[str, Any]] = {}
    for key in ("hotCities", "allCities"):
        for city in raw.get(key) or []:
            cid = str(city.get("cityId") or "").strip()
            name = str(city.get("cityName") or "").strip()
            if not cid or not name:
                continue
            item = {k: city.get(k, "") for k in CITY_FIELDS}
            item["cityId"] = cid
            merged.setdefault(cid, item)
    return list(merged.values())


def _pinyin_of(name: str) -> tuple[str, str]:
    """(全拼, 逐字首字母)。没装 pypinyin 时返回空串。"""
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return "", ""
    full = "".join(lazy_pinyin(name)).lower()
    initials = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER)).lower()
    return full, initials


def enrich_cities(
    cities: Iterable[dict[str, Any]], previous: dict[str, dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """补 province / pinyin / initials / letter；已有值优先复用，避免刷新后分组漂移。"""
    previous = previous or {}
    out: list[dict[str, Any]] = []
    for city in cities:
        name = str(city.get("cityName") or "").strip()
        old = previous.get(str(city.get("cityId"))) or {}
        pinyin = str(old.get("pinyin") or "").strip()
        initials = str(old.get("initials") or "").strip()
        if not pinyin or not initials:
            pinyin, initials = _pinyin_of(name)
        if not pinyin or not initials:
            en = str(city.get("enName") or "").lower()
            pinyin = pinyin or en
            initials = initials or (en[:1] or name[:1].lower())
        out.append(
            {
                **city,
                "province": str(old.get("province") or province_of(str(city.get("code") or ""))),
                "pinyin": pinyin,
                "initials": initials,
                "letter": letter_of(pinyin, name),
            }
        )
    out.sort(key=lambda c: (str(c["letter"]), str(c["pinyin"] or c["cityName"]), str(c["cityId"])))
    return out


def save_cities(cities: list[dict[str, Any]], path: str | Path | None = None) -> Path:
    p = Path(path) if path else CITY_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "/action/carrctapi/order/cityList/v1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(cities),
        "cities": cities,
    }
    p.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
    )
    return p


# --------------------------------------------------------------------- 车型

_YEAR_PREFIX = re.compile(r"^\s*(?:19|20)\d{2}\s*")
#: 只剥「配置/版本」类后缀，不动 PLUS / Pro / L 这类车系名的一部分
_CONFIG_SUFFIXES = (
    "尊享版", "豪华版", "舒适版", "精英版", "领先版", "旗舰版", "大师版", "大师",
    "六座版", "七座版", "五座版", "四座版", "两驱", "四驱", "荣耀", "乾崑", "增程",
)


@dataclass(frozen=True)
class ModelInfo:
    raw: str
    brand: str
    series: str

    @property
    def display(self) -> str:
        return f"{self.brand}{self.series}" if self.brand and self.series else (self.raw or "未知车型")


class BrandCatalog:
    """品牌别名表：长别名优先，避免「奇瑞捷途」被「奇瑞」抢走。"""

    def __init__(self, brands: Iterable[dict[str, Any]] | None = None) -> None:
        aliases: list[tuple[str, str]] = []
        for item in brands or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            for alias in [name, *(item.get("aliases") or [])]:
                alias = str(alias).strip()
                if alias:
                    aliases.append((alias, name))
        # 长的先匹配，同长按名称稳定排序
        aliases.sort(key=lambda x: (-len(x[0]), x[0]))
        self._aliases = aliases
        self.names = sorted({name for _, name in aliases}, key=len, reverse=True)

    def parse(self, model_name: str) -> ModelInfo:
        raw = (model_name or "").strip()
        text = _YEAR_PREFIX.sub("", raw).strip() or raw
        lowered = text.lower()

        for alias, brand in self._aliases:
            if lowered.startswith(alias.lower()):
                rest = text[len(alias):].strip()
                return ModelInfo(raw=raw, brand=brand, series=self._clean_series(rest))
        return ModelInfo(raw=raw, brand="", series=self._clean_series(text))

    @staticmethod
    def _clean_series(series: str) -> str:
        out = (series or "").strip()
        changed = True
        while changed and out:
            changed = False
            for suffix in _CONFIG_SUFFIXES:
                if out.endswith(suffix) and len(out) > len(suffix):
                    out = out[: -len(suffix)].strip()
                    changed = True
        return out


_DEFAULT_BRANDS: BrandCatalog | None = None


def load_brands(path: str | Path | None = None) -> BrandCatalog:
    global _DEFAULT_BRANDS
    p = Path(path) if path else BRAND_FILE
    if path is None and _DEFAULT_BRANDS is not None:
        return _DEFAULT_BRANDS
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        catalog = BrandCatalog([])
    else:
        brands = data.get("brands") if isinstance(data, dict) else data
        catalog = BrandCatalog(brands or [])
    if path is None:
        _DEFAULT_BRANDS = catalog
    return catalog


def parse_model(model_name: str, catalog: BrandCatalog | None = None) -> ModelInfo:
    return (catalog or load_brands()).parse(model_name)
