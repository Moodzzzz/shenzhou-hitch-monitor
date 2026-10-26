"""神州顺风车监控 —— 网关客户端。

逆向结论（详见 docs/REVERSE_ENGINEERING.md）：
  POST https://m.zuche.com/api/gw.do?uri=<接口URI>
  Content-Type: application/x-www-form-urlencoded
  body: data={"pageNo":1,"pageSize":200}

  * 无需登录、无需 Cookie、无需签名
  * 接口 URI 使用 /action/ 命名空间（/resource/ 是 H5 旧命名空间，顺风车不在其中）
  * 响应统一信封：{"code":1,"msg":"成功","busiCode":"BASE000","status":"SUCCESS","content":{...}}
"""
from __future__ import annotations

import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GATEWAY_URL = "https://m.zuche.com/api/gw.do"
ORIGIN = "https://m.zuche.com"
REFERER = "https://m.zuche.com/"

# 与 App/移动端 H5 保持一致的 UA，避免被当作异常流量
DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/122.0.0.0 Mobile Safari/537.36"
)

URI_HITCH_LIST = "/action/carrctapi/order/hitchList/v1"
URI_HITCH_DETAIL = "/action/carrctapi/order/hitchDetail/v1"
URI_CITY_LIST = "/action/carrctapi/order/cityList/v1"
#: 普通租车选车（含套餐日均参考价）。免登录，按城市 + 时间 + 坐标返回门店车型。
URI_CHOOSE_CAR = "/resource/carrctapi/order/chooseCar/v3"
#: 价格日历。参数必须是 city/dept/model/精确取还时间；免登录可读。
URI_PRICE_CALENDAR = "/resource/carrctapi/order/chooseCarPriceCalendar/v1"
#: 月租车型组起价。仅作历史兼容，不得再用于反推服务费或订单应付。
URI_LONG_RENT_GROUPS = "/action/carrctapi/longRent/modelGroupsList/v1"

# 服务端单页上限实测可到 200
MAX_PAGE_SIZE = 200


class GatewayError(RuntimeError):
    """网关返回非成功信封，或响应不可解析。"""

    def __init__(self, message: str, *, uri: str = "", code: Any = None,
                 busi_code: str = "", status: str = "", raw: str = "") -> None:
        super().__init__(message)
        self.uri = uri
        self.code = code
        self.busi_code = busi_code
        self.status = status
        self.raw = raw

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        head = f"[{self.status or self.busi_code or '?'}] code={self.code} {self.args[0]}"
        return f"{head} (uri={self.uri})" if self.uri else head


class RouteNotFound(GatewayError):
    """接口 URI 未被网关注册（服务端 CID_AID_ROUTELIST_EMPTY）。"""


class GatewayClient:
    """带限速与重试的网关客户端。

    min_interval: 两次请求之间的最小间隔（秒），用于自我限速，避免给对方造成压力。
    """

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        min_interval: float = 1.2,
        user_agent: str = DEFAULT_UA,
        gateway_url: str = GATEWAY_URL,
        proxy_pool: Any = None,
    ) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self.min_interval = max(0.0, min_interval)
        self.user_agent = user_agent
        self.gateway_url = gateway_url
        self.proxy_pool = proxy_pool
        self._last_call = 0.0
        self.request_count = 0
        self.proxy_hits = 0

    # ---------------------------------------------------------------- 传输层

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _post(self, uri: str, params: dict[str, Any]) -> str:
        payload = urllib.parse.urlencode(
            {"data": json.dumps(params, ensure_ascii=False, separators=(",", ":"))}
        ).encode("utf-8")
        url = f"{self.gateway_url}?uri={urllib.parse.quote(uri, safe='/')}"
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": ORIGIN,
                "Referer": REFERER,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        proxy = self.proxy_pool.get() if self.proxy_pool is not None else None
        if proxy:
            # 本请求单独走代理；失败由上层记账并换下一个出口
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            self.proxy_hits += 1
        else:
            opener = urllib.request.build_opener()
        self._throttle()
        self.request_count += 1
        started = time.monotonic()
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception:
            if self.proxy_pool is not None:
                self.proxy_pool.report(proxy, False)
            raise
        if self.proxy_pool is not None and proxy:
            self.proxy_pool.report(proxy, True, time.monotonic() - started)
        return text

    # ---------------------------------------------------------------- 业务层

    def call(self, uri: str, params: dict[str, Any] | None = None) -> Any:
        """调用网关并返回 content 字段。

        网络错误与 5xx 按指数退避重试；业务错误（code != 1）直接抛出。
        """
        params = params or {}
        last_err: Exception | None = None
        # 风控/限流类状态码换出口重试；这类响应在代理池场景下很常见
        retryable = {403, 407, 429, 500, 502, 503, 504}
        for attempt in range(self.retries + 1):
            try:
                text = self._post(uri, params)
            except urllib.error.HTTPError as exc:
                last_err = exc
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:400]
                except Exception:
                    pass
                if exc.code == 422 and ("carwrm" in body or "<html" in body.lower()):
                    # 风控 WAF 质询页（carwrm），不是参数契约问题
                    raise GatewayError(
                        "风控拦截(422 WAF 质询)，稍后或换出口再试",
                        uri=uri, code=exc.code, raw=body[:160],
                    ) from exc
                if exc.code not in retryable or attempt == self.retries:
                    raise GatewayError(
                        f"HTTP {exc.code}", uri=uri, code=exc.code
                    ) from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                last_err = exc
                if attempt == self.retries:
                    raise GatewayError(f"网络错误: {exc}", uri=uri) from exc
            else:
                return self._unwrap(uri, text)

            # 指数退避 + 抖动
            time.sleep(min(2 ** attempt * 0.8 + random.uniform(0, 0.4), 8.0))

        raise GatewayError(f"请求失败: {last_err}", uri=uri)  # pragma: no cover

    @staticmethod
    def _unwrap(uri: str, text: str) -> Any:
        try:
            body = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GatewayError(
                "响应不是合法 JSON（可能被风控或网关降级）", uri=uri, raw=text[:300]
            ) from exc

        code = body.get("code")
        if code == 1:
            return body.get("content")

        status = str(body.get("status") or "")
        kwargs = dict(
            uri=uri,
            code=code,
            busi_code=str(body.get("busiCode") or ""),
            status=status,
            raw=text[:300],
        )
        if status.startswith("CID_AID_ROUTELIST"):
            raise RouteNotFound(str(body.get("msg") or "接口不存在"), **kwargs)
        raise GatewayError(str(body.get("msg") or "接口返回失败"), **kwargs)

    # ------------------------------------------------------------ 具体接口

    def hitch_page(
        self,
        page_no: int = 1,
        page_size: int = MAX_PAGE_SIZE,
        *,
        pickup_city_id: str | None = None,
        return_city_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """拉取一页顺风车列表。返回 (原始 items, 总条数)。"""
        params: dict[str, Any] = {
            "pageNo": int(page_no),
            "pageSize": min(int(page_size), MAX_PAGE_SIZE),
        }
        if pickup_city_id:
            params["pickupCityId"] = str(pickup_city_id)
        if return_city_id:
            params["returnCityId"] = str(return_city_id)

        content = self.call(URI_HITCH_LIST, params) or {}
        items = content.get("hitchList") or []
        try:
            total = int(content.get("count") or 0)
        except (TypeError, ValueError):
            total = 0
        return list(items), total

    def hitch_all(
        self,
        *,
        page_size: int = MAX_PAGE_SIZE,
        max_pages: int = 30,
        pickup_city_id: str | None = None,
        return_city_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """翻页拉全量。

        列表是「偏移分页」，翻页期间有新车上架会导致某条被挤到下一页而漏抓，
        漏抓会让下游误判为「已下架」。所以这里做两件事：
          1. 记录页内重复（重复说明数据在翻页时发生了位移）；
          2. 一旦发现重复或抓取数少于总数，立刻补一遍全量再取并集。
        """
        items, total, duplicated = self._collect_pages(
            page_size, max_pages, pickup_city_id, return_city_id
        )
        unstable = duplicated > 0 or (total > 0 and len(items) < total)
        if unstable:
            items2, total2, _ = self._collect_pages(
                page_size, max_pages, pickup_city_id, return_city_id
            )
            merged: dict[str, dict[str, Any]] = {str(i.get("hitchId")): i for i in items}
            for it in items2:
                merged.setdefault(str(it.get("hitchId")), it)
            items = list(merged.values())
            total = max(total, total2)
        return items, total

    def _collect_pages(
        self,
        page_size: int,
        max_pages: int,
        pickup_city_id: str | None,
        return_city_id: str | None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        duplicated = 0
        total = 0
        for page in range(1, max_pages + 1):
            batch, page_total = self.hitch_page(
                page,
                page_size,
                pickup_city_id=pickup_city_id,
                return_city_id=return_city_id,
            )
            # count 通常只在首页返回，后续页为 0，不能直接覆盖
            if page_total:
                total = max(total, page_total)
            if not batch:
                break
            for item in batch:
                hid = str(item.get("hitchId"))
                if hid in seen:
                    duplicated += 1
                    continue
                seen.add(hid)
                collected.append(item)
            if total and len(collected) >= total:
                break
        return collected, total, duplicated

    def hitch_detail(self, hitch_id: str | int) -> dict[str, Any]:
        """顺风车详情：含取还车门店、原价、车型描述等。"""
        return self.call(URI_HITCH_DETAIL, {"hitchId": str(hitch_id)}) or {}

    def cities(self) -> dict[str, Any]:
        """城市字典：{"hotCities": [...], "allCities": [...]}"""
        return self.call(URI_CITY_LIST, {}) or {}

    def choose_cars(
        self,
        *,
        city_id: str | int,
        pickup_time: str,
        return_time: str,
        lat: float,
        lon: float,
    ) -> dict[str, Any]:
        """普通租车选车，返回 ``{"deptHangModels": [...]}``。

        ``packagePrice`` 是当前租期的套餐日价（周租/月租会自动折扣），
        ``dailyPrice`` 是门市日价，两者都只是租金口径，不含服务费/整备费。
        """
        return self.call(
            URI_CHOOSE_CAR,
            {
                "pickupCityId": str(city_id),
                "pickupTime": str(pickup_time),
                "returnCityId": str(city_id),
                "returnTime": str(return_time),
                "entrance": 1,
                "userChooseLat": f"{float(lat):.6f}",
                "userChooseLon": f"{float(lon):.6f}",
                "holidaysWaitingFlag": 0,
            },
        ) or {}

    def choose_car_price_calendar(
        self,
        *,
        pickup_city_id: str | int,
        pickup_dept_id: str | int,
        model_id: str | int,
        pickup_time: str,
        return_time: str,
    ) -> dict[str, Any]:
        """读取车型的价格日历。

        参数契约来自 2026-10-26 对移动站真实请求的捕获：缺任一字段会
        返回网关超时，不能用其它猜测参数代替。返回 content，不在此处
        解释金额语义；调用方应交给 ``rental_pricing.parse_price_calendar``。
        """
        return self.call(
            URI_PRICE_CALENDAR,
            {
                "allianceBusinessFlag": False,
                "modelId": int(model_id),
                "orderEntrance": 1,
                "pickupCityId": int(pickup_city_id),
                "pickupDate": str(pickup_time),
                "pickupDeptId": int(pickup_dept_id),
                "returnDate": str(return_time),
            },
        ) or {}

    def long_rent_groups(self, *, city_id: str | int) -> dict[str, Any]:
        """官方长租（月租）车型组起价；不得用于反推服务费或应付。"""
        return self.call(URI_LONG_RENT_GROUPS, {"cityId": str(city_id)}) or {}
