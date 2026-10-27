# 神州车价 · 顺风车监控 + 长租行情

盯「神州租车」App 里的两类车：

1. **顺风车**（异地调车特价车）—— 车需要从一个城市调回另一个城市，你以 1 元 / 50 元 / 100 元的固定价把车开过去，租期 1～13 天。放出来基本靠抢，所以需要程序盯着。
2. **周租 / 半月租 / 月租** —— 按 7 / 15 / 30 天分别取数；列表日均只作候选参考，价格日历单独核对逐日合计，订单应付未取得费用明细前不伪造。

程序做的事：**定时拉全量 → 比对生命周期（新上线 / 被锁定 / 回到订单库 / 过期剔除）→ 按条件过滤 → 发邮件等通知**，并把结果同步到一个可搜索的**可视面板**。

> 逆向过程、接口证据链见 [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md)。
> 线上看板：<https://moodzzzz.github.io/shenzhou-hitch-monitor/>

---

## 它能告诉你什么

| 事件 | 含义 | 默认是否推送 |
|---|---|---|
| 🆕 **新上线** | 新放出来的顺风车 | 是 |
| ♻️ **回到订单库** | 之前被锁定/关闭的车，订单取消后又可预订了 | 是 |
| 🔒 **被锁定** | 车被别人下单锁定了，从可订池里消失 | 是 |
| ⬇️ **降价** | 同一条 `hitchId` 价格下调 | 是 |
| ⌛ **已过期剔除** | 还车日期已过，从库里清掉 | 否（只计数） |
| 🧹 **已关闭剔除** | 工单已不存在 | 否（只计数） |

> 「被锁定」不是猜的：列表接口只返回可预订的车，某条消失后程序会**按这条线路定向复查**，仍查不到才判定不可订；而且要**连续两轮**都查不到才推送，避免翻页抖动造成的误报。

---

## 特点

| | |
|---|---|
| 零第三方依赖 | 纯 Python 标准库，云端不用 `pip install` |
| 私有订阅 | 面板填条件与邮箱 → 后台发送验证链接 → 确认后生效；每封提醒自带管理/退订入口 |
| 免登录 | 列表/详情/城市字典都不需要账号、Cookie 或签名 |
| 全国覆盖 | 5 个请求拿全 950+ 条（`pageSize=200`） |
| 城市全量 | 471 个城市的字典，含省份、全拼、首字母；中文 / `chengdu` / `cd` 都能搜到 |
| 车型归一 | 把 `modelName` 拆成「品牌 + 车系」，剥掉 `2023` 年份前缀和「尊享版」这类后缀 |
| 只推增量 | 按 `hitchId` 落库，新上线性推送；同一条降价也提示 |
| 抗漏抓 | 全量翻页遇到数据位移会自动补一遍；消失的车再按线路复核 |
| 首轮不刷屏 | 首次运行只建基线，不把 900+ 条历史当「新上线」 |
| 邮件优先 | HTML 邮件，卡片式排版，手机上也好看 |
| 订阅隔离 | 每条订阅只收到自己规则命中的车；私有后台保存邮箱，公开 JSON 不出现地址 |
| 长租行情 v3 | 列表日均 / 价格日历合计 / 订单应付三档证据分开；无证据显示「待核价」 |
| 全量长租 | 全国 471 城 × 全车型 × 三档租期；每城每租期抽样核验价格日历 |
| 代理池 | 免费代理自动验活 + 付费代理优先，分散出口、降低限流 |
| 实时同步 | 顺风车每 30 分钟刷新（错峰 :07/:37）；长租每天 12 点档全量刷新；面板每 60 秒自动读最新 JSON |
| App 式卡片流 | 顺风车首屏精选 6 辆 + 6 条路线，图片卡片、路线渐进展开，不再铺满全表 |
| 私有控制面 | Cloudflare Workers + D1：管理员、double opt-in、邮件 outbox、审计、支付禁用态预留 |

---

## 5 分钟上手

**不想碰命令行？** 打开看板 → 选好城市 / 路线 / 品牌 → 「邮件提醒」→ 填邮箱 → 发送验证邮件。
邮箱只进私有后台；必须点邮件里的确认链接，订阅才会启用。未部署后台时页面保持只读，不会把邮箱写进公开仓库。
后台按订阅规则匹配车辆，每封提醒只发给对应订阅人，并带独立退订链接。

```bash
python main.py init                      # 生成配置
python main.py once --dry-run            # 演练：看当前能命中什么（不推送、不写状态）
python main.py test-notify               # 测通知渠道

python main.py once                      # 建立基线（首轮只记录，不推送）
python main.py watch                     # 常驻，默认 4 小时一轮
python main.py rentals --export dashboard/rental.json   # 抓周租/半月租/月租行情
```

看板：

```bash
python main.py serve --refresh           # 抓一轮 + 起本地看板 → http://127.0.0.1:8765/
```

查城市 ID、导出 CSV：

```bash
python main.py cities 成都          # 中文 / 全拼 / 首字母都能查
python main.py cities cd            # 首字母：常德 / 成都 / 承德 / 昌都
python main.py cities --letter L    # 按字母分组浏览
python main.py snapshot             # 导出全国 CSV，标注命中的订阅
```

城市字典补拼音（只在生成时用 pypinyin，运行期无依赖）：

```bash
pip install pypinyin && python tools/build_cities.py
```

---

## 可视面板

![顺风车看板](docs/panel.png)

长租票版：列表日均、价格日历合计、订单应付三层证据分开显示。

![长租看板](docs/panel-rental.png)

城市选择器：471 城全量，中文 / 全拼 / 首字母都能搜，右侧是字母索引条。

![城市选择器](docs/panel-city.png)

面板是一个**静态页 + 一份 JSON**，所以两种用法都行：

- 本地：`python main.py serve --refresh`（或 `once --export dashboard/dashboard.json` 后起任意静态服务）
- 线上：`szhitch.yml` 每 30 分钟刷新 `dashboard/dashboard.json` 并发布 Pages；`rental.yml` 每天 12:13（北京时间）刷新 `dashboard/rental.json`（由下一轮顺风车一起发布）

功能：

- **App 式顺风车卡片流**：首屏只展示 6 辆精选车，其余按路线分组，每条路线先给 3 辆；点「继续看路线」或「＋ N 辆」渐进展开。卡片带真实车图、路线、车型、取还日期、门店、总价与日均。
- **城市选择器（全量 471 城）**：中文 / 全拼 / 首字母三种搜法，右侧 A–Z 索引条；每个城市标当前取车 / 还车条数。
- **路线筛选**：取车 + 还车两个选择器就是一条定向路线，或点「热门路线」直接跳进去。
- **更多筛选**：品牌 chip、车系关键词、可预订/锁定状态收进折叠区，不再挤满首屏。
- **只看关注**：服务端订阅命中的车带 ★。
- **链接可分享**：筛选条件写进 URL hash（`#from=成都&brands=大众`）。
- **预警监控**：命中车、订阅状态、最近 30 天预警流水。
- **长租票版**：7 / 15 / 30 天三档，每行显示列表日均、日历逐日合计和「待核价」的订单应付。
  - **列表日均**来自 `chooseCar/v3` 的 `packagePrice`，只负责找候选，绝不乘天数冒充总价。
  - **日历已核**来自 `chooseCarPriceCalendar/v1`，逐日价完整覆盖租期且合计与 `totalDailyPrice` 一致才盖章。
  - **订单应付**没有登录后的费用明细证据，固定在 UI 显示「待核价」；可退押金、可选保障和优惠不混进租金。
  - 范围是全城市（471 城）× 全车型；旧数据沿用会保留原租期，不会被贴上新日期。

> 界面是「奶油纸票据 × 调度验讫章」风格，价格用等宽数字，图片卡片按路线分组。
> 移动端单列卡片；筛选和路线渐进展开，长租 JSON 只在进入票版时加载。

> 「预警记录」来自监控每轮的命中流水（与是否配置通知渠道无关）；刚跑第一天记录较少属正常。

---

## 配置说明

`config.json`（`python main.py init` 生成）。

### `watch` —— 推什么

全部为「与」关系；列表内部是「或」；留空表示不限制。

| 字段 | 说明 |
|---|---|
| `pickup_cities` | 取车城市，如 `["成都","重庆"]`；「成都」「成都市」都能匹配 |
| `return_cities` | 还车城市 |
| `any_cities` | 取车**或**还车任一命中即可，适合「只想离开/到达某城」 |
| `routes` | 精确路线，方向敏感：`["成都→丽江"]` 不会命中 `丽江→成都`；支持 `成都→*`、`*→丽江` |
| `brands` / `exclude_brands` | 品牌白名单/黑名单，如 `["理想","宝马"]`（由车型名解析，见下） |
| `models` / `exclude_models` | 车型关键词白名单/黑名单，如 `["帕萨特","L7"]` |
| `only_special` / `special_price` | 只推特价，默认 ≤100 元 |
| `max_price` / `min_price` | 总价区间 |
| `max_price_per_day` | 日均价上限 |
| `min_days` / `max_days` | 租期天数 |
| `date_from` / `date_to` | 取车日期窗口 |

```json
"watch": {
  "pickup_cities": ["成都"],
  "brands": ["理想", "宝马"],
  "routes": ["成都→*"],
  "max_price": 100,
  "exclude_brands": ["大众"]
}
```

### `watch_profiles` —— 多组订阅

想同时盯几条互不相干的线路时用它。**非空时优先于 `watch`**，任一订阅命中就推送，
消息里会写「🎯 订阅：成都出发」，面板也会把这些车标成 ★。

```json
"watch_profiles": [
  { "name": "成都出发", "pickup_cities": ["成都"], "max_price": 100 },
  { "name": "回丽江/三亚", "routes": ["*→丽江", "*→三亚"],
    "brands": ["宝马", "奔驰", "奥迪", "沃尔沃", "理想", "问界"] },
  { "name": "先别推这个", "enabled": false, "brands": ["大众"] }
]
```

| 字段 | 说明 |
|---|---|
| `name` | 订阅名，出现在推送与面板里；缺省为「订阅1」 |
| `enabled` | `false` 时跳过这一组 |
| 其余字段 | 与 `watch` 完全一致 |

### 车型品牌是怎么认的

`modelName` 是「品牌+车系+配置」拼在一起的字符串，程序按别名表拆开：

| 原始车型名 | 品牌 | 车系 |
|---|---|---|
| `2023沃尔沃S90` | 沃尔沃 | S90 |
| `广汽传祺M8尊享版` | 传祺 | M8 |
| `问界M9六座版` | 问界 | M9 |
| `吉利银河星耀8` | 吉利银河 | 星耀8 |
| `长安深蓝S07` | 深蓝 | S07 |

- 长别名优先：`吉利银河` 不会被 `吉利` 抢走，`奇瑞捷途` 归到 `捷途`
- 年份前缀（`2023`）与配置后缀（尊享版/六座版/两驱/乾崑…）会被剥掉
- 品牌表在 `data/brands.json`，加新品牌直接往里补 `name` + `aliases` 即可
- 面板与推送里的品牌统计都基于这套解析，不是字符串包含

### `behavior` —— 怎么推

| 字段 | 默认 | 说明 |
|---|---|---|
| `first_run` | `silent` | 首轮只建基线；改 `notify` 会把存量全推一遍 |
| `notify_new` | `true` | 推新上线 |
| `notify_relisted` | `true` | 推「回到订单库」 |
| `notify_locked` | `true` | 推「被锁定」 |
| `notify_price_drop` | `true` | 推降价 |
| `notify_expired` / `notify_closed` | `false` | 过期/关闭一般只计数，避免噪音 |
| `verify_missing` | `true` | 消失后按线路定向复查 |
| `verify_missing_limit` | `40` | 每轮最多复查多少条（控制请求量） |
| `confirm_absence_passes` | `2` | 连续几轮查不到才判定锁定 |
| `prune_expired_days` | `7` | 过期/关闭记录保留几天后硬删除 |
| `enrich_detail` | `true` | 命中后补详情拿原价、门店 |
| `detail_limit` | `10` | 每轮最多补几条事件详情 |
| `dashboard_enrich_limit` | `40` | 每轮为面板缓存几条原价（渐进覆盖，4 小时一轮约 4 天补全） |
| `max_items_per_message` | `8` | 单条消息最多列几条 |

### `poll` —— 多久拉一次

| 字段 | 默认 | 说明 |
|---|---|---|
| `interval_seconds` | `14400` | 轮询间隔，默认 4 小时，最小 120 秒 |
| `page_size` | `200` | 单页条数，上限 200 |
| `min_request_interval` | `1.2` | 请求间隔，自我限速 |

---

## 通知渠道

只填你想用的，其余留空。**密钥走环境变量**，不要提交到仓库。

| 渠道 | 配置 | 环境变量 |
|---|---|---|
| **邮件** | `notify.smtp` | `SZHITCH_SMTP_HOST` / `_PORT` / `_USER` / `_PASS` / `_TO` |
| 企业微信群机器人 | `notify.wecom_webhook` | `SZHITCH_WECOM` |
| 飞书 | `notify.feishu_webhook` + `feishu_secret` | `SZHITCH_FEISHU` |
| 钉钉 | `notify.dingtalk_webhook` + `dingtalk_secret` | `SZHITCH_DINGTALK` |
| Bark（iOS） | `notify.bark_key` | `SZHITCH_BARK` |
| Server酱 | `notify.serverchan_key` | `SZHITCH_SERVERCHAN` |
| 通用 Webhook | `notify.webhook_url` | `SZHITCH_WEBHOOK` |

### 邮件怎么配

以 QQ 邮箱为例（163、Gmail、Outlook 同理，用 **SMTP 授权码**，不是登录密码）：

```bash
export SZHITCH_SMTP_HOST=smtp.qq.com
export SZHITCH_SMTP_PORT=465
export SZHITCH_SMTP_USER=you@qq.com
export SZHITCH_SMTP_PASS=你的16位授权码
export SZHITCH_SMTP_TO=you@qq.com,other@example.com
python main.py test-notify
```

或直接写进 `config.json`：

```json
"notify": {
  "smtp": {
    "host": "smtp.qq.com",
    "port": 465,
    "user": "you@qq.com",
    "password": "授权码",
    "to": ["you@qq.com"],
    "ssl": true
  }
}
```

邮件是 HTML 卡片版：每条车一张卡，含路线、车型、现价/原价、「省 99%」、日期、门店。同一封邮件按「新上线 / 回到订单库 / 被锁定」分节。

---

## 私有订阅后台（可选，推荐公开部署时使用）

公开 Pages 不能保存管理员会话、邮箱、SMTP/支付密钥。`control-plane/` 是独立低成本后端：

- **Cloudflare Workers + D1**：管理员、私有订阅、double opt-in、签名退订、发信 outbox、审计。
- **管理员**：GitHub App 用户 OAuth + `state + PKCE(S256)` + `ADMIN_GITHUB_LOGINS` 白名单，不保存 GitHub access token。
- **邮件**：Resend HTTP API；会员接受/provider message id 与最终投递分开记录。失败指数退避，5 次后进入 dead，可在后台手动重试。
- **规则只一套**：Actions 从 `/internal/v1/subscriptions` 读取已验证订阅的 `id + rules`（不含邮箱），继续使用 Python `rules.py` 匹配，再把幂等通知任务写入 `/internal/v1/outbox`。
- **支付**：只有 `products/prices/orders/payment_events/webhook_receipts/entitlements` 和 `DisabledPaymentProvider`；任何 `/api/payments/*` 返回 `payment_disabled`，没有真实收款。

本地验证：

```bash
cd control-plane
npm install
npx wrangler d1 migrations apply szhitch-control-plane --local
cp .dev.vars.example .dev.vars   # 填入本地测试值，EMAIL_MODE=console
npm test
npx wrangler dev --local --port 8790
```

部署前必须替换 `wrangler.jsonc` 的 D1 ID、GitHub App Client ID 和公开 Worker 地址，并在 Cloudflare 配置：
`GITHUB_APP_CLIENT_SECRET`、`SESSION_SECRET`、`INTERNAL_TOKEN`、`RESEND_API_KEY`。
GitHub Actions 再加 `SZHITCH_CONTROL_PLANE_URL`、`SZHITCH_INTERNAL_TOKEN`；
面板的 `dashboard/runtime.json` 改为 `enabled: true` 并填写 Worker 地址。

> 后台未部署时 `runtime.json` 保持 `enabled: false`，面板只读、不收集邮箱。
> 旧 Issue 订阅仅在 `subscribe.backend_url` 为空时兼容存在；迁移完成后应关闭 Issue 入口并清理历史邮箱（清理前需人工确认）。

---

## 代理池

长租要一次跑 471 城 × 3 档 ≈ 1900 次请求，集中在十几分钟里，单 IP 容易被限流。
`szhitch/proxy.py` 做了两级出口：

| 来源 | 怎么配 | 说明 |
|---|---|---|
| **付费 / 自有代理**（优先） | `SZHITCH_PROXIES` 或 `SZHITCH_PROXY_FILE` | 格式 `http://user:pass@host:port`，多条用逗号或换行分隔 |
| **免费代理**（兜底） | 默认开启，无需配置 | 从公开列表拉候选，用神州网关逐个验活，能打通才进池 |

```bash
python main.py proxy                 # 拉免费列表 + 验活 + 写缓存
python main.py proxy --paid-only     # 只用 SZHITCH_PROXIES，不碰免费池
python main.py proxy --show          # 看缓存里还有多少可用
python main.py rentals --no-proxy    # 这一轮强制直连（排查用）
```

- 池子按「成功次数 / 延迟」挑出口，失败两次进冷却，冷却结束自动复活；全挂了退回直连，不会让整轮停摆
- 免费代理存活率通常只有 5%–15%，只适合兜底。要稳定就跑付费池：把代理写进
  `Settings → Secrets → SZHITCH_PROXIES`，工作流会直接用它，不再拉免费列表
- 日志里的代理地址会打码成 `http://***@host:port`，不会把账号密码写进 Actions 日志
- 只支持 HTTP/HTTPS 代理；SOCKS 需要额外依赖，未纳入（保持零第三方依赖）

## 部署

### GitHub Actions（推荐，零成本）

1. 建一个**公开**仓库（Free 套餐的 Pages 只支持公开仓库），把本目录推上去
2. **Settings → Secrets and variables → Actions** 添加通知密钥（见上表）
3. **Settings → Pages → Source** 选 `GitHub Actions`
4. 启用 Actions，手动跑一次（首次是建基线）
5. 公开订阅需要私有后台时，按“私有订阅后台”一节部署，再配置 `SZHITCH_CONTROL_PLANE_URL`、`SZHITCH_INTERNAL_TOKEN`

之后 `szhitch.yml` 每 30 分钟跑：抓取 → 通知/写入后台 outbox → 提交 `dashboard/dashboard.json` → 发布 Pages；
`rental.yml` 每天北京时间 12:00 跑：抓取 471 城 × 全车型 × 三档长租与抽样价格日历 → 提交 `dashboard/rental.json`
（不单独发布 Pages，避免用旧顺风车数据覆盖线上；下一轮 30 分钟监控会带上它）。
面板地址形如 `https://<用户名>.github.io/<仓库名>/`。

> 仓库是公开的，所以**密钥只能放 Secrets**。配置里留空即可，工作流里有一道检查，
> 一旦发现 `config.json` 出现明文 webhook/密码会直接失败并报错。

> GitHub 的 cron 用 UTC，且高峰期可能延迟数分钟到十几分钟。要更准就用云函数。

### 本机 / VPS 常驻

```bash
python main.py watch
```

```bash
sudo cp -r . /opt/shenzhou-hitch-monitor
sudo cp deploy/systemd/szhitch.service /etc/systemd/system/
sudo systemctl enable --now szhitch
journalctl -u szhitch -f
```

### Docker

```bash
docker build -t szhitch .
docker run -d --name szhitch --restart always \
  -e SZHITCH_SMTP_HOST=smtp.qq.com -e SZHITCH_SMTP_USER=you@qq.com \
  -e SZHITCH_SMTP_PASS=授权码 -e SZHITCH_SMTP_TO=you@qq.com \
  -p 8765:8765 \
  -v $(pwd)/config.json:/app/config.json -v $(pwd)/data:/app/data \
  szhitch
```

### 阿里云函数计算 / 腾讯云 SCF

处理程序分别填 `cloud.aliyun_fc.handler.handler` 与 `cloud.tencent_scf.main_handler.main_handler`，定时触发器建议 4 小时一次。
函数实例无状态，**必须**外接存储放状态：`SZHITCH_STATE=oss://bucket/szhitch/state.json` + `OSS_*` 环境变量，否则每轮都会重复推送。

---

## 常见问题

**Q：面板说「被锁定」，但 App 里明明还能订？**这是全量翻页的漏抓导致的误判。现已修复：消失的车会按「取车城市 + 还车城市」定向复查，查得到就撤销判定并记 `[纠正]` 日志；而且要连续两轮查不到才推送。历史遗留的误判可手动修：

```bash
python main.py recheck --dry-run   # 先看
python main.py recheck             # 写回
```

**Q：城市为什么是 471 个？会不会漏掉？**
471 是官方 `cityList` 接口返回的全量（`allCities` 471 + `hotCities` 24，去重后 471）。
面板里的城市选择器不再只列「当前有车的城市」，而是整份字典 —— 现在在售覆盖 190 多个城市，
剩下 200 多个暂时没车，但你想盯的时候能选到。万一列表里冒出新城市而字典还没收录，
`insights.py` 会把它补进 `city_dict`（字母归 `#`），面板不会显示不出来。

**Q：路线写了 `成都→丽江`，为什么 `丽江→成都` 的车也推了？**
现在的实现是方向敏感的，不会。早期版本把两个城市名当子串在整串里找，反向线路会误命中，
这个问题已修，并在 `tests/test_catalog_rules.py` 里用 `test_direction_is_respected` 锁死。
反过来，如果你**想**要双向，写两条：`["成都→丽江","丽江→成都"]`，或者用 `any_cities`。

**Q：`watch` 和 `watch_profiles` 同时配了，以哪个为准？**
`watch_profiles` 非空时只用订阅分组，`watch` 被忽略；想回到单规则模式就把 `watch_profiles` 清空。

**Q：面板里「只看关注」为什么是空的？**
`★` 来自服务端订阅（`watch_profiles`）的命中结果，而面板数据是监控跑完写出来的。
先在面板里选好条件、复制 `watch_profiles` 进 `config.json`，跑一轮 `python main.py once --export dashboard/dashboard.json` 就会标出来。

**Q：为什么第一次正式运行没有推送？**
首轮只建基线，否则 950+ 条会刷屏，之后也分不清哪些是真新出现。想看效果用 `--dry-run`。

**Q：一键订阅按钮是灰的？**
面板数据里没有仓库信息。GitHub Actions 部署时会自动带上（`GITHUB_REPOSITORY`）；
本地 `python main.py serve` 预览时需在 `config.json` 里加 `"subscribe": {"repo": "你的用户名/仓库名"}`。

**Q：预警记录为什么是空的？**
记录来自监控每轮的命中流水。先在 `config.json` 配好 `watch_profiles`（或用面板生成），跑一轮
`python main.py once` 之后就有记录；GitHub Actions 部署用 cache 保存状态，换仓库存（OSS）不会丢。

**Q：推送里的「原价 3048」是什么？**
顺风车本质是异地调车，`estTotalPrice` 是按日租算的正常价，`realTotalPrice` 是调车补贴价。

**Q：会被封吗？**
列表接口不需要账号。默认每 1.2 秒一个请求、4 小时一轮（约 6 轮/天、合计 300 请求/天以内），实测无风控。接口无公开文档，随时可能变。

---

## 目录结构

```
shenzhou-hitch-monitor/
├── main.py                 CLI：once / watch / serve / recheck / snapshot / cities
├── szhitch/
│   ├── gateway.py          网关客户端 + 抗漏抓翻页
│   ├── models.py           HitchItem 数据模型
│   ├── catalog.py          城市索引（拼音/首字母/省份）+ 车型品牌解析
│   ├── rules.py            筛选规则、路线方向匹配、订阅分组
│   ├── store.py            状态机 + 到达/预警日志（SQLite / JSON / OSS）
│   ├── subscriptions.py    legacy Issue 订阅（迁移期兼容）
│   ├── control_plane.py    私有后台客户端：读规则、写幂等通知任务
│   ├── monitor.py          生命周期判定、规则匹配、通知
│   ├── rentals.py          v3 长租列表参考价 + 价格日历抽样核验
│   ├── rental_pricing.py   金额分、价格日历契约与证据等级
│   ├── insights.py         面板数据 + 预警记录
│   ├── notify.py           邮件/企微/飞书/钉钉/Bark/Server酱
│   └── config.py           配置加载
├── dashboard/              静态看板（index.html + dashboard.json + runtime.json）
├── control-plane/          Workers + D1 私有后台（管理员/订阅/outbox/支付预留）
├── tools/
│   ├── build_cities.py     生成城市字典（需 pypinyin）
│   └── probe_rental_prices.py  只读核验 7/15/30 天价格日历
├── cloud/                  云函数入口
├── deploy/systemd/         systemd unit
├── data/cities.json        471 个城市 + 省份 + 拼音 + 首字母
├── data/brands.json        品牌别名表（约 100 个品牌）
├── docs/                   逆向报告 + 面板截图 + 脱敏价格样本
└── tests/                  92 项 Python 测试；control-plane 另有 7 项 Node 测试
```

```bash
python -m unittest discover -s tests -v
```

---

## 合规提醒

- 只读取**公开可见**的在售列表，不涉及登录、下单、账号数据。
- 仅用于个人比价/盯盘，请勿转售数据或用于商业用途。
- 保持克制的请求频率；接口属于对方资产，滥用可能违反服务条款。
- 库存、价格、规则以 App 内实际展示为准。
