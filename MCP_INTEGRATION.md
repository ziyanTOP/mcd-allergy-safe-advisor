# MCP 集成说明

本项目**真实依赖麦当劳 MCP Server** 获取实时业务数据。所有菜单、门店、价格、营养与优惠信息均来自 MCP 的实时调用，
静态知识库只承担"过敏原语义推理"这一层 MCP 不提供的职责。

## 1. MCP Server 信息

| 项 | 值 |
|---|---|
| 服务名称 | 麦当劳 MCP Server（官方） |
| 地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP（MCP 协议版本 2025-06-18） |
| 鉴权 | `Authorization: Bearer <MCP Token>` |
| Token 申请 | https://open.mcd.cn/mcp → 手机号登录 → 控制台 → 激活 → 同意服务协议 |
| 限流 | 每 Token 每分钟 600 次请求，超限返回 429 |
| 官方仓库 | https://github.com/M-China/mcd-mcp-server |
| **实测工具数** | **35 个**（2026-10-23 通过 `tools/list` 实测确认） |

本项目所有工具信息均通过运行时调用 `tools/list` 取得，不是抄文档。
`probe` 子命令可以随时打印当前 MCP 暴露的真实工具与必填参数：

```bash
python -m mcd_safe_order probe
```

MCP Server 版本演进（本次开发时最新 `1.0.9`，2026-09-10）：

```
1.0.1 餐品营养信息列表 → 1.0.2 麦乐送点餐与积分兑换券 → 1.0.3 到店取餐与团餐
1.0.4 积分兑换实物、商城订单查询 → 1.0.5 更换套餐组合与特制 → 1.0.6 历史订单查询
1.0.7 主题活动查询预约下单 → 1.0.8 积分抽奖 → 1.0.9 取消订单、餐具选择、备注、取餐柜二维码
```

## 2. 使用的 Tool 与调用顺序

```
用户画像（过敏原 / 忌口 / 门店 / 预算 / 营养目标）
        │
        ▼
 ① query-nearby-stores      beType + searchType + city/keyword
        │                   → 拿到 storeCode / beCode
        ▼
 ② query-meals              storeCode + orderType + beType
        │                   → data.categories[].meals[].code   ← 只有编码，没有名字！
        │                   → data.meals{code: {currentPrice, originalPrice}}
        ▼
 ③ query-meal-detail  × N   storeCode + orderType + beType + code
        │                   → name / rounds[] / modification.values[] / supportModify
        │                   → 这是餐品名、套餐组成与"可去除配料"的唯一来源
        ├──────────────┐
        ▼              ▼
 ④ list-nutrition-foods  ⑤ calculate-price
   能量/蛋白/脂肪/        组合价格核对，校验预算
   碳水/钠/钙
        └──────┬───────┘
               ▼
      三层过敏原风险分级引擎（本仓库 src/mcd_safe_order/）
        必含层 / 可去除层 / 可选加料层
               ▼
        输出：推荐清单 + 待确认项 + 组合提示 + 到店话术
```

| # | Tool | 用途 | 关键入参 | 在本项目中的作用 |
|---|---|---|---|---|
| ① | `query-nearby-stores` | 附近门店 | `beType`, `searchType`，可选 `city`/`keyword` | 定位可点门店，取 `storeCode` / `beCode` |
| ② | `query-meals` | 门店菜单 | `storeCode`, `orderType`, `beType` | 拿到分类、餐品编码与实时价格表 |
| ③ | `query-meal-detail` | 单品详情 | `storeCode`, `orderType`, `beType`, `code` | **餐品名 + 套餐子项 + 特制配料**，去险方案的来源 |
| ④ | `list-nutrition-foods` | 营养数据 | 无 | 支撑控卡/控钠/高蛋白排序与提示 |
| ⑤ | `calculate-price` | 价格计算 | `storeCode`, `orderType`, `beType`, `items[]` | 校验预算上限，输出可执行清单 |
| ⑥ | `campaign-calendar` | 活动日历 | 无 | 可选：结合限时活动给出更优组合 |
| ⑦ | `available-coupons` / `query-store-coupons` | 优惠券 | `storeCode`, `orderType`, `beType` | 可选：让"安全且便宜"的组合更便宜 |
| ⑧ | `query-my-account` | 账户与积分 | 无 | 可选：积分换购安全餐品 |

### 2.1 为什么必须两跳调用

`query-meals` 的返回体实测如下：

```json
{ "data": {
    "categories": [ { "name": "鸡肉汉堡/卷", "meals": [ { "code": "521952", "tags": ["上新"] } ] } ],
    "meals": { "521952": { "currentPrice": "15.9", "originalPrice": "24" } },
    "frequent": [ ... ] } }
```

**餐品名不在这里**，价格也分散在另一张表里。名字、套餐组成、可去除配料都在 `query-meal-detail`：

```json
{ "data": {
    "code": "521952",
    "name": "龙焰鸡腿堡",
    "rounds": [],
    "modification": { "items": [ { "maxValues": 2, "minValues": 0, "values": [
        { "code": "120549", "name": "辣椒黄油风味酱", "minQuantity": 1, "selectedQuantity": 1 },
        { "code": "120512", "name": "切块生菜",       "minQuantity": 0, "selectedQuantity": 1 } ] } ] },
    "supportModify": true } }
```

所以本项目的真实链路是 **query-meals → 逐个 query-meal-detail**。
单店 68 个餐品实测耗时约 12 秒（68 次调用），并用本地缓存（`~/.cache/mcd-safe-order-v3`）避免重复拉取。

### 2.2 `modification` 的三档语义（准确率的关键）

| MCP 字段 | 含义 | 本项目的判定 |
|---|---|---|
| `rounds[].choices[]` | 餐品必含成分 | 命中过敏原 → ⛔ |
| `values[]` 且 `minQuantity ≥ 1` | 必选配料，无法去除 | 命中过敏原 → ⛔ |
| `values[]` 且 `minQuantity == 0` 且 `selectedQuantity ≥ 1` | 默认已加，但可要求不加 | 命中过敏原 → 🟡，动作直接取自 MCP |
| `values[]` 且 `selectedQuantity == 0` | 默认不加的可选加料 | 仅提示，不算命中 |

举例：`龙焰鸡腿堡` 的"辣椒黄油风味酱"是 `minQuantity=1`（含乳且不可去除），
`小杯鲜萃咖啡` 的"奶油球"是 `minQuantity=0`（默认加但可去掉）——两者风险等级完全不同。

## 3. 为什么必须用 MCP（而不是纯静态知识库）

本项目刻意把"事实层"和"推理层"拆开：

- **事实层必须实时**：门店可售菜单每天不同（限时品、断货、区域差异），价格与优惠实时变动。
  静态知识库无法回答"这家店今天有没有巨无霸套餐"。
- **推理层 MCP 不提供**：麦当劳 MCP 当前版本**没有暴露过敏原字段**，33 个 Tool 中
  `list-nutrition-foods` 只返回能量/蛋白质/脂肪/碳水/钠/钙，不含过敏原。
  这正是本项目的价值所在——把 MCP 的实时菜单数据与过敏原语义层结合，产出 MCP 本身给不了的结论。
- **实时标签优先**：若 MCP 返回的餐品标签中含"含乳制品""花生"等关键词，引擎会**直接采信并升级风险等级**，
  静态知识库只作为补充与兜底。

## 4. 数据流示例

```
用户："我对花生过敏，还有乳糖不耐受，帮我在国贸店点一份午餐"

MCP 调用：
  query-meals(storeCode=<国贸店>)        → 12 个在售餐品，含"巨无霸/薯条/麦乐鸡/圆筒..."
  query-meal-detail(<巨无霸>)            → 套餐组成：汉堡 + 中薯 + 可乐，替换项含"去芝士/去千岛酱"
  list-nutrition-foods()                 → 巨无霸 520kcal / 钠 980mg ...

引擎判定：
  巨无霸 → 🟡 可去除后可选（含乳制品，去芝士可解；花生列为 may_contain）
  薯条   → 🟠 谨慎（中国区配方不含乳，但共用炸锅存在交叉接触）
  圆筒   → ⛔ 不建议（含乳且不可去除）

输出：
  推荐 = 巨无霸（去芝士 + 不加奶昔）
  待确认 = 薯条（到店问共用油锅）
  排除 = 圆筒
  话术 = "我对花生过敏（过敏性休克级）……"
```

## 5. 鉴权与密钥安全

- Token **仅通过环境变量 `MCD_MCP_TOKEN` 或 MCP 客户端配置文件注入**，绝不写入代码或提交到仓库。
- 本仓库 `mcp-config.example.json` 只提供 `${MCD_MCP_TOKEN}` 占位符，**不含任何真实凭据**。
- 仓库内提供的 `scripts/fill_mcd_token.py` 以隐藏输入（`getpass`）读取 Token 并写入本地配置文件（自动备份原配置），
  避免 Token 出现在 shell history 与屏幕回显中。
- 申请 Token：https://open.mcd.cn/mcp

## 6. 客户端接入方式

### Claude Code / Cursor / 通用 MCP 客户端

```json
{
  "mcpServers": {
    "mcd": {
      "type": "streamable-http",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer ${MCD_MCP_TOKEN}" }
    }
  }
}
```

### 本仓库脚本

```bash
export MCD_MCP_TOKEN=<你的 Token>
python -m mcd_safe_order probe    # 列出 MCP 实际暴露的工具与必填参数
```

> `probe` 命令会在运行时实时读取 MCP 的 Tool 列表，因此也能作为 MCP 服务可用性的自检工具。
