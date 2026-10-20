# 神州顺风车 逆向分析报告

> 目标：神州租车 Android 客户端 v9.6.0（`com.szzc`）
> 目的：定位「顺风车」板块的数据接口，为监控程序提供稳定契约
> 时间：2026-10-20
> 结论可信度：本文所有接口均**实测跑通**并附原始响应；未能静态读取的 App 内部实现会明确标注为「未知」，不作推测性断言。

---

## 0. 摘要

| 项 | 结论 |
|---|---|
| 业务实质 | 「顺风车」是**租车异地调车**：车需从 A 城调回 B 城，用户以 1/50/100 元固定价开过去，**不是**网约车合乘 |
| 客户端加固 | 爱加密（ijiami），`classes.dex` 为 13.6KB 壳，业务 DEX 加密在 `assets/ijiami.dat` |
| 静态突破口 | 加固只保护 DEX；`AndroidManifest.xml` 与 `resources.arsc` 明文可读 |
| 数据通道 | 与 H5 共用网关 `POST https://m.zuche.com/api/gw.do?uri=<URI>` |
| 鉴权 | **无**签名、无 token、无 Cookie；仅校验 POST 方法与 `Origin/Referer` |
| 核心接口 | `/action/carrctapi/order/hitchList/v1`（列表）、`hitchDetail/v1`（详情）、`cityList/v1`（城市） |
| 数据规模 | 观测期间全国在售 959～963 条，`pageSize=200` 时 5 个请求拉全 |
| 风控 | 10 次连发无限流、无验证码、无设备指纹校验 |

---

## 1. 目标画像

| 属性 | 值 | 取证方式 |
|---|---|---|
| 包名 | `com.szzc` | 应用宝详情页 `pkg_name` |
| 版本 | 9.6.0（更新于 2026-10-17） | 应用宝 `version_name` / `update_time` |
| 文件大小 | 168,040,446 字节 | HTTP `Content-Length` |
| MD5 | `543E9DCDA56EE55D13E83CAE68B12A96` | 应用宝 `md_5`，下载后本地 `md5sum` **完全一致** |
| 分发来源 | 应用宝 `app_id=125412` | `https://sj.qq.com/appdetail/com.szzc` |
| 直链规律 | `https://imtt.dd.qq.com/16891/apk/<MD5>.apk` | 由 `md_5` 字段直接构造并验证 200 |
| 主体 | 海科（平潭）信息技术有限公司 | 应用宝 `developer` |

只做 MD5 校验不下载第三方站点的「绿色版」，可确保分析对象与官方分发一致。

---

## 2. 业务语义澄清（重要）

一开始容易被名字误导。顺风车在神州这里是**车辆调度产品**，不是拼车：

- 官方口径：「用户租用指定车型前往指定城市，无论同目的地间隔多远，都只收取 100 元费用」「通过智能车辆调度系统…合理配置车队规模」。
- 接口实证：同一条记录里 `realTotalPrice=100`、`estTotalPrice=3048`（详见表 6.2）。`estTotalPrice` 是按日租算出的正常价，`realTotalPrice` 是调车补贴价。
- UI 资源名 `shunfeng_car_price_old`（原价）与 `shunfeng_car_price`（现价）并排存在，说明「划线原价」本来就是产品设计的一部分。

因此监控的价值点不是「有没有车」，而是**哪条线路放出了高原价、低现价的调车单**——原价越高、现价越低，越值得抢。

---

## 3. 静态取证与阻碍

### 3.1 APK 基本面

```
classes.dex          13,600 字节 / 190 个字符串   ← 异常小
AndroidManifest.xml 206,508 字节 / 863 个字符串   ← 未加固
resources.arsc    6,602,984 字节                  ← 未加固
lib/               82 个 .so，含 arm64-v8a / armeabi-v7a
```

官方 App 正常应有数十 MB DEX、数万方法。13.6KB 且字符串只有 190 个，典型**加壳 stub**。

### 3.2 加固识别

stub DEX 字符串池里出现 `/libexec.so`、`/libexecmain.so`、`/proc/self/maps`、`Landroid/app/AppComponentFactory;`，配合以下文件，可判定为**爱加密（ijiami）**：

| 文件 | 大小 | 作用 |
|---|---|---|
| `assets/ijiami.dat` | 15,804,567 | 加密后的业务 DEX |
| `assets/ijiami.ajm` | 7,332,612 | 第二段加密载荷（`indl01` 魔数） |
| `lib/arm64-v8a/libexec.so` | 636,696 | 脱壳运行时 |
| `lib/arm64-v8a/libexecmain.so` | 175,328 | 壳入口 |
| `lib/arm64-v8a/libexec_x86.so` | 667,144 | x86 模拟器分支 |
| `lib/arm64-v8a/libijmdetect-drisk.so` | 633,524 | 反调试/风险检测 |

**结论**：`ijiami.dat` 的密钥按 App 派生、由 `libexec.so` 在运行时解密，纯静态无解。要么 Frida 脱壳（需 root 设备/模拟器），要么换一条取证路径。

本机无 adb、无 JDK/JADX、无可用设备，脱壳成本远高于收益，因此转向下面两条**成本低得多且结论等价**的路径。

### 3.3 未加固面：Manifest

`AndroidManifest.xml` 是二进制 AXML，但**没有加密**。按 AXML 规范解析字符串池（`flags=0` → UTF-16，`stringsStart=3480`，863 条），拿到 651 个组件类名。

其中顺风车相关 6 个：

```
com.zuche.component.domesticcar.hitchride.activity.HitchRideListActivity
com.zuche.component.domesticcar.hitchride.activity.HitchRideQueryListActivity
com.zuche.component.domesticcar.hitchride.activity.HitchRideDetailActivity
com.zuche.component.internalcar.shorttermlease.shortrent.hitchride.activity.HitchRideMainActivity
com.zuche.component.internalcar.shorttermlease.shortrent.hitchride.activity.HitchRideQueryListActivity
com.zuche.component.internalcar.shorttermlease.shortrent.hitchride.activity.ReserveHitchRideActivity
```

同时确认 Deep Link scheme 为 `zuche`（`action=zuche`）。

### 3.4 未加固面：资源表

`resources.arsc` 可提取 53,191 条 ASCII 字符串，其中 252 条含中文、61 条命中 `hitch/shunfeng`。关键布局与控件 ID：

```
activity_hitch_ride__main_layout      activity_activity_reserve_hitch_ride_layout
hitch_ride_layout / hitch_ride_container
shunfeng_from_city / shunfeng_to_city        ← 取车/还车城市
shunfeng_from_time / shunfeng_to_time        ← 取车/还车时间
shunfeng_car_name / shunfeng_car_price       ← 车型 / 现价
shunfeng_car_price_old                       ← 划线原价（印证 estTotalPrice）
shunfeng_load_car_iconFont / shunfeng_more
```

**推论（inferred）**：模块内部代号是 `hitchride`，列表页是「取车城市 + 还车城市 + 时间 + 车型/价格」的卡片流，且产品上强调原价对比。这一推论后来被接口字段完全印证。

### 3.5 为什么没有继续脱壳

脱壳只能拿到「App 怎么调接口」，而接口契约本身可以从网关侧直接观测。对一个监控程序来说，**契约的可验证性 > 实现的完整性**。所以把预算放在第 4、5 节的协议实测上，而不是 Frida 脱壳。

若后续确实需要（例如接口加了 App 专属签名），路线见第 11 节。

---

## 4. 网关协议

### 4.1 请求形态（H5 侧实证）

`m.zuche.com` 的 `scripts/commons.js`（396KB，React SPA）里封装了统一请求器：

```js
n.data = qs.stringify({ data: JSON.stringify(r) })   // 表单体：data=<params 的 JSON 字符串>
n.url  = "/api/gw.do?uri=" + e.api                   // 接口 URI 走 query
// chooseCar 额外走随机网关：/api/random/gw.do?v=<ts>&uri=<uri>
```

实测等价 curl：

```bash
curl -X POST 'https://m.zuche.com/api/gw.do?uri=/action/carrctapi/order/hitchList/v1' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -H 'Origin: https://m.zuche.com' -H 'Referer: https://m.zuche.com/' \
  --data-urlencode 'data={"pageNo":1,"pageSize":200}'
```

### 4.2 命名空间：`/action/` vs `/resource/`

H5 首页 bundle 里只有 `/resource/carrctapi/...`（试驾、翻译等），且 `/resource/carrctapi/order/...` 全部返回「不支持的 api 请求」。真实业务接口在 **`/action/`** 命名空间下。

这个区别很关键——按 `/resource/` 去猜顺风车接口会一直 404。

### 4.3 错误信封 → 探测 oracle

网关的错误码足够细分，可以用来判定「URI 是否存在」，无需盲猜参数：

| 场景 | 响应 | 含义 |
|---|---|---|
| 成功 | `{"code":1,"msg":"成功","status":"SUCCESS","content":{…}}` | 正常 |
| URI 不存在 | `{"code":6,"status":"CID_AID_ROUTELIST_EMPTY","msg":"不支持的api请求…"}` | 未被注册 |
| 参数缺失/非法 | `{"code":6,"busiCode":"BASE001","status":"BASE_ERROR","msg":"参数不能为空"}` | URI 存在，参数不对 |
| 用 GET | `{"code":6,"msg":"请求来源不正确"}` | 必须 POST |

据此可以安全枚举：**「不支持的api请求」= 不存在**；**别的错误 = 存在**。`hitchDetail/v1` 就是这样确认的（`hitchList/v1` 的命名规律来自第 5 节的线索）。

> 线索来源：一篇 CSDN 博客（2024-03）提到过 `/action/carrctapi/order/hitchList/v1`，但作者当时请求失败（未带参数，服务端报「服务器内部错误」）。本报告对该 URI **独立复现并跑通**，同时补齐了参数、分页、详情、城市字典，均以实测为准。

---

## 5. 接口清单（全部实测）

### 5.1 顺风车列表

```
POST https://m.zuche.com/api/gw.do?uri=/action/carrctapi/order/hitchList/v1
```

| 参数 | 类型 | 必需 | 说明 |
|---|---|---|---|
| `pageNo` | int | 是 | 页码，从 1 开始；越界返回空数组且 `code=1` |
| `pageSize` | int | 是 | 每页条数；实测 10/20/50/100/200 均按请求返回，200 可用 |
| `pickupCityId` | string | 否 | 取车城市 ID，如 `"335"`（张掖）。**传错 ID 返回 0 条，不报错** |
| `returnCityId` | string | 否 | 还车城市 ID |

实测：传 `pickupCityId=335` → `count=7`，返回全部为张掖出发，证明是服务端真实筛选。
这些名称类参数（`pickupCityName`/`beginDate`/`maxPrice`/`sortType` 等）传入后 `count` 无变化，**均被忽略**——服务端只认 ID。

响应外层：`count` 为总数，`hitchList` 为数组，另有 `picUrl`（运营图）与 `threshold`（语义未知）。

### 5.2 顺风车详情

```
POST …?uri=/action/carrctapi/order/hitchDetail/v1     body: {"hitchId":"9515098"}
```

比列表多出：取还车门店（`pickupDept`/`returnDept`，含 `deptId`、`workTime`）、原价 `estTotalPrice`、车型描述 `modelDesc`、城市 ID、经纬度、订单规格说明 `specification`。免登录。

### 5.3 城市字典

```
POST …?uri=/action/carrctapi/order/cityList/v1        body: {}
```

返回 `hotCities`（热门）与 `allCities`（471 个），字段含 `cityId`、`cityName`、`enName`、行政区划码 `code`、经纬度、上门服务标记。已导出为项目内 `data/cities.json`。

---

## 6. 字段字典

### 6.1 `hitchList` 单条（12 字段，全量）

| 字段 | 示例 | 说明 |
|---|---|---|
| `hitchId` | `"9515098"` | 主键，跨轮次去重依据 |
| `modelId` | `4151` | 车型 ID |
| `modelName` | `"理想L7"` | 车型名 |
| `modelPic` | `https://dfs.zuchecdn.com/…png` | 车型图 |
| `realTotalPrice` | `"100"` | **顺风车现价**（字符串） |
| `pickupCityName` | `"张掖"` | 取车城市 |
| `returnCityName` | `"揭阳"` | 还车城市 |
| `beginTime` | `"2026.10.20"` | 取车日（点号分隔） |
| `endTime` | `"2026.10.22"` | 还车日（点号分隔） |
| `beginDate` | `"10月20日"` | 展示用文案 |
| `endDate` | `"10月22日"` | 展示用文案 |
| `limitTenancyDay` | `6` | 租期天数上限 |

注意：价格是**字符串**，日期是**点号分隔**且另有中文文案字段，解析时都要兜底。

### 6.2 `hitchDetail` 关键补充

| 字段 | 示例 | 说明 |
|---|---|---|
| `estTotalPrice` | `"3048"` | 按日租算的原价（营销对比用） |
| `realTotalPrice` | `"100"` | 顺风车价 |
| `limitTenancyDay` | `6` | 租期 |
| `pickupDept.deptName` | `"张掖机场自助点"` | 取车门店 |
| `returnDept.deptName` | `"揭阳国际机场服务点"` | 还车门店 |
| `pickupDept.workTime` | `"00:00-24:00"` | 门店营业时间 |
| `modelDesc` | `"42kWh增程式 \| SUV 5座"` | 车型规格 |
| `bafTips` | `"燃油/过路费自理"` | 费用提示 |
| `specification` | 长文本 | 顺风车订单规则（30 分钟内预付全款、不支持换车等） |
| `pickupCityId` / `returnCityId` | `"335"` / `"236"` | 可用于服务端筛选 |

---

## 7. 参数与边界（实测数据）

| 行为 | 观测结果 |
|---|---|
| `pageSize` 上限 | 200 条按请求全量返回，未见截断 |
| 超出末页 | `pageNo=97`（总数 960）→ `hitchList=[]`，`code=1` |
| 总数波动 | 连续观测 959→960→961→962→963，库存实时变化，不能当固定值 |
| 分页一致性 | 翻页期间有新车上架会导致跨页重复/漏项，必须按 `hitchId` 去重 |
| 10 次连发 | 全部 `code=1`，无限流、无验证码 |
| 单轮成本 | `pageSize=200` → 5 次请求，约 5.4 秒；补 10 条详情约 17 秒 |
| 时区 | 时间字段为东八区自然日 |

---

## 8. 鉴权与风控评估

| 维度 | 结论 | 证据 |
|---|---|---|
| 登录态 | 不需要 | 无 Cookie 请求列表/详情均返回 `code=1` |
| 签名 | 无 | H5 请求体只有 `data` 字段，无 `sign`/`timestamp`/`nonce` |
| 设备指纹 | 列表接口未校验 | 仅 UA + `Origin`/`Referer` |
| 方法限制 | 必须 POST | GET 返回「请求来源不正确」 |
| 频率限制 | 未观测到 | 10 次连发全部成功 |
| 反爬 | 未见 JS 挑战/滑块 | 响应无挑战标记 |

**风险判断**：当前接口开放度高，但**没有公开文档意味着随时可能变更**。程序里做了三件事对冲：
1. 请求间隔 1.2 秒、轮询默认 10 分钟，把压力控制在正常用户行为量级；
2. 网关错误分类型抛异常（`RouteNotFound` / `GatewayError`），接口一旦改名会立刻暴露而不是静默失败；
3. 状态库按 `hitchId` 去重，接口短暂抖动不会造成重复推送。

---

## 9. 结论可信度分级

| 编号 | 结论 | 级别 | 依据 |
|---|---|---|---|
| E1 | APK 为官方 v9.6.0，MD5 与商店一致 | 观测 | 下载后 `md5sum` 比对 |
| E2 | DEX 被爱加密加固，无法静态读取 | 观测 | stub 13,600B + `libexec*.so` + `ijiami.dat` |
| E3 | 顺风车模块代号 `hitchride` | 观测 | Manifest 6 个 Activity 类名 |
| E4 | UI 含「现价/原价」双价格 | 观测 | `resources.arsc` 资源名 |
| E5 | 网关为 `POST /api/gw.do?uri=` + `data=` 表单 | 观测 | `commons.js` 源码 + 实测 |
| E6 | 业务命名空间是 `/action/` | 观测 | 实测对比两种前缀 |
| E7 | `hitchList` / `hitchDetail` / `cityList` 可用且免登录 | 观测 | 原始响应（本文全部示例） |
| E8 | `pickupCityId`/`returnCityId` 为服务端筛选，其余参数被忽略 | 观测 | 传参前后 `count` 对比 |
| E9 | 无签名、无限流 | 观测 | 请求头与连发实验 |
| E10 | 顺风车=异地调车而非合乘 | 观测+推断 | 官方表述 + `estTotalPrice` 逻辑 |
| U1 | `threshold=6` 的业务含义 | **未知** | 未在响应中体现差异，程序未使用 |
| U2 | 是否还有 App 专属的顺风车接口（如订阅、提醒） | **未知** | 加固包内部未展开 |
| U3 | 筛选参数是否还有未穷举的（如价格区间） | **未知** | 仅验证了常见命名，未做全字典枚举 |

程序实现只依赖 E5～E9，对 U1～U3 不做任何假设。

---

## 10. 可复现步骤

```bash
# 1) 取官方包（MD5 来自应用宝，可自行校验）
curl -L -o com.szzc.apk \
  https://imtt.dd.qq.com/16891/apk/543E9DCDA56EE55D13E83CAE68B12A96.apk
md5sum com.szzc.apk    # 应为 543E9DCDA56EE55D13E83CAE68B12A96

# 2) 判定加固
unzip -p com.szzc.apk classes.dex | wc -c        # 13600
unzip -l com.szzc.apk | grep -E 'libexec|ijiami'

# 3) 读未加固的 Manifest 字符串（AXML：UTF-16 pool，stringsStart=3480）
#    可见 com.zuche.component.*.hitchride.activity.* 共 6 个

# 4) 打网关
curl -X POST 'https://m.zuche.com/api/gw.do?uri=/action/carrctapi/order/hitchList/v1' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -H 'Origin: https://m.zuche.com' -H 'Referer: https://m.zuche.com/' \
  --data-urlencode 'data={"pageNo":1,"pageSize":3}'
```

---

## 11. 如果以后要彻底打开加固包

接口一旦加上 App 专属签名，H5 通道失效，就需要脱壳：

1. **环境**：Android 模拟器（arm64 镜像）+ root + Frida（`frida-server`）。
2. **时机**：在 `Application.attachBaseContext` 之后、首屏渲染前挂载，遍历 `DexFile`/`BaseDexClassLoader` 的 `cookie`。
3. **落地**：`frida-dexdump` 或自写脚本把内存中的 DEX dump 出来，再交 JADX 复核。
4. **注意**：爱加密带 `libijmdetect-drisk` 反调试，需要先处理 `ptrace` 检测与 `/proc/self/maps` 校验；这一步的对抗成本会随版本升级上升。
5. **替代**：若只为拿契约，优先看 H5/小程序侧——同一网关，往往比 App 更少防护。

---

## 12. 合规

- 本分析仅面向**公开可见的商品列表**，未触及登录、下单、支付、用户数据。
- 未破解、未绕过任何访问控制；加固包在识别后即停止分析，未做脱壳。
- 请求频率刻意控制在正常用户量级（约 30 请求/小时）。
- 接口与数据归神州租车所有，请勿转售、批量分发或用于商业竞争。个人比价自用为宜。

---

## 13. 附：生命周期判定（v1.1 增补）

监控上线后需要回答三个新问题：车被锁定了吗？订单取消回来了吗？过期的怎么剔除？
这一节记录判定依据、踩过的坑与最终方案。

### 13.1 接口能提供什么

对全部 956 条在售记录做字段并集统计，只有 12 个字段，且**每条都全量存在**：

```
modelPic / modelId / modelName / limitTenancyDay / realTotalPrice
pickupCityName / returnCityName / beginTime / endTime / beginDate / endDate / hitchId
```

没有任何 `status` / `locked` / `soldOut` / `canBook` 字段。详情接口也一样（21 个字段，`inventoryAbleFlag` 是门店库存标记，不是订单锁）。

**结论（观测）**：列表接口只返回「可预订」的车，锁定状态不通过字段表达，只能由「是否出现在列表里」推断。

### 13.2 三个可用的信号

| 信号 | 含义 | 依据 |
|---|---|---|
| 从列表消失 | 不再可预订 | 列表 = 可订池 |
| 详情报「顺风车工单不存在」 | 工单已关闭 | `hitchDetail` 错误文案 |
| 重新出现在列表 | 订单被取消，回到订单库 | 同一 `hitchId` 复现 |

### 13.3 踩坑：偏移分页导致的误报

第一版逻辑是「消失即锁定」，结果误报了。复现过程：

1. 连续两轮全量扫描，各自 950 条、无重复、集合完全一致 —— 看起来分页是稳的；
2. 但跨轮次比对时出现 `total=963 / 抓到=962` 的差 1 条，说明**翻页期间数据在动**；
3. 用户反馈「林芝→重庆 理想L7（9515088）面板显示被锁定，App 里还能订」；
4. 用 `pickupCityId=1101&returnCityId=12` 定向查询该线路，**这条车就在里面**（该线路当时仅 1 条）。

根因：`pageNo/pageSize` 是**偏移分页**，没有游标。翻页的几秒内有新车上架，列表整体后移，某条会被挤到上一页已读过的位置，于是漏抓。漏抓 → 误判成「被锁定」。

另一个附带问题：`count` 只在首页返回，后续页为 0，早期代码用最后一页的 0 覆盖了总数，导致提前终止翻页。

### 13.4 修复方案（当前实现）

1. **翻页补抓**：记录页内重复数；一旦出现重复或抓取数少于 `count`，立刻重新全量拉一遍并取并集。数据位移才加倍请求，正常轮次仍是 5 个请求。
2. **定向复核**：任何「消失」的车，用本地城市字典（`data/cities.json`，471 条，来自 `cityList`）解析出 `pickupCityId`/`returnCityId`，把这条线路单独查一遍。
   - 查得到 → 判定为漏抓，撤销锁定，日志记 `[纠正]`
   - 线路为空 → 确实没车了
   - 线路超过一页 / 请求失败 → 本轮不下结论
3. **连续确认**：连续 `confirm_absence_passes`（默认 2）轮复核都查不到才推送「被锁定」，宁可晚一轮，不误报。
4. **过期剔除**：`endDate < 今天` 直接判 `expired`，不进可订池；超过 `prune_expired_days`（默认 7 天）从库里硬删除。

### 13.5 代价

每轮请求数 = 5（全量） + N（复核，仅消失的车） + M（详情，命中项）。正常情况下 N=0～5、M≤10，仍在 40 请求/小时量级。

### 13.6 状态机

```
             ┌──────────── 重新出现在列表 ────────────┐
             ▼                                        │
        available ──消失且复核查不到(连续2轮)──▶ locked ──还车日过期──▶ expired
             │                                        │
             └──消失且复核查不到 + 工单不存在──▶ closed ──保留期到──▶ 删除
```

新增可信度条目：

| 编号 | 结论 | 级别 |
|---|---|---|
| E11 | 列表接口无锁定字段，只能由「是否在列」推断 | 观测（956 条字段并集） |
| E12 | 偏移分页在数据变动时会漏抓，必须补抓/复核 | 观测（差 1 条 + 定向查询复现） |
