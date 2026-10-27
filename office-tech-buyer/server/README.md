# 办公设备易采买 —— 云端服务

为 WorkBuddy / OpenClaw 「办公设备易采买」技能提供后端能力。技能端只做 HTTP 转发，
所有上游请求、限流、品类识别、耗材映射与未命中日志落盘都在本服务内完成。

## 1. 快速开始

依赖与解释器由 [uv](https://docs.astral.sh/uv/) 管理（`pyproject.toml` + `uv.lock`），
本机无需预装 Python：uv 会按 `.python-version`（3.11）自动准备。

```bash
cd server

# 安装 uv（若尚未安装）
# Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
# macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh

# 安装依赖（含开发/测试组）
uv sync --all-groups

# 开发运行（自动重载）
uv run uvicorn app.main:app --host 0.0.0.0 --port 8446 --reload

# 或直接运行
uv run python -m app.main
```

启动后访问：

- 服务信息 <http://localhost:8446/>
- 健康检查 <http://localhost:8446/health>
- 交互式文档 <http://localhost:8446/docs>

> 服务默认监听 `8446`（`app/config.py` 中的 `Settings.port`），可用 `PORT` 环境变量覆盖。
> 技能端默认连生产地址，本机自测时设 `OFFICE_BUYER_SERVER_URL=http://localhost:8446`。

## 2. 接口一览

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/search` | 品类识别 + 主设备/耗材/配件并发比价 |
| GET | `/api/v1/detail` | 购买链接、淘口令、专票与质保解析 |
| GET | `/api/v1/consumable/mapping` | 设备 → 耗材/配件映射查询 |
| GET | `/api/v1/consumable/unmatched` | 未命中关键词统计（运维） |
| POST | `/api/v1/consumable/reload` | 热重载 `app/data/*.json`（运维） |
| GET | `/health` | 健康检查（含映射表条数与日志滚动状态） |

### 示例

```bash
# 关键词命中映射表（打印机会自动带出原装 + 兼容耗材）
curl "http://localhost:8446/api/v1/search?keyword=%E6%83%A0%E6%99%AE%20M126a&source=0&page=1"

# 关键词未命中（写入 logs/unmatched.log，返回 matched=false）
curl "http://localhost:8446/api/v1/search?keyword=%E6%9C%AA%E7%9F%A5%E5%86%B7%E9%97%A8%E8%AE%BE%E5%A4%87%E5%9E%8B%E5%8F%B70123"

# 商品详情
curl "http://localhost:8446/api/v1/detail?source=2&id=123456789"

# 映射查询
curl "http://localhost:8446/api/v1/consumable/mapping?device=M126a"
```

### `search` 响应结构

```jsonc
{
  "matched": true,                  // false 时客户端宿主 AI 需自行推理耗材型号并二次查询
  "keyword": "惠普 M126a",
  "category": "多功能一体机",
  "matched_by": "consumables",      // consumables | server_hardware | paper_spec | none
  "match_key": "M126a",
  "device_name": "惠普 HP LaserJet Pro MFP M126a",
  "attributes": { "consumable_type": "碳粉盒/硒鼓", "model_code": "CC388A / 88A", "match_how": "key_substring" },
  "unit_calculation": null,         // 纸品/文具才有值，如 calculate_per_pack_price
  "queries": [
    {
      "keyword": "惠普 HP LaserJet Pro MFP M126a",
      "kind": "primary",            // primary | original | compatible | accessory | variant
      "label": "主设备/主商品",
      "platform": "全部",
      "status": "OK",               // OK | NO_RESULTS | UPSTREAM_ERROR
      "count": 20,
      "items": [
        {
          "goodsId": "…", "source": 2, "platform": "京东",
          "title": "…", "shopName": "…",
          "originalPrice": 1299.0, "actualPrice": 1099.0, "couponPrice": null,
          "coupon_desc": "原价 ¥1299.0，券后 ¥1099.0，省 ¥200.0",
          "attributes": { "supply_type": "原装", "model_code": "CC388A" },
          "unit_price": null          // 整机/耗材类恒为 null，仅纸品文具类有折算结果
        }
      ]
    },
    { "kind": "original",   "label": "原装耗材",       "items": [] },
    { "kind": "compatible", "label": "第三方兼容耗材", "items": [] }
  ]
}
```

纸品/文具类的 `unit_price` 结构：

```jsonc
{
  "total_price": 69.9,
  "unit_price": 13.98,
  "unit": "包",
  "basis": "box",                     // box | multi_pack | sheet_count | single_pack
                                      // per_sheet | basis_unknown | ambiguous | no_price
  "basis_note": "按整箱 5包折算",
  "unit_calculation": "calculate_per_pack_price",
  "candidates": []                    // 仅 basis=ambiguous 时非空，列出各解读供客户端并列展示
}
```

未命中时响应形如：

```jsonc
{
  "matched": false,
  "keyword": "未知冷门设备型号123",
  "log_line": "[2026-10-26 12:00:00] [UNMATCHED] Keyword: 未知冷门设备型号123 | IP: 127.0.0.1",
  "hint": "云端静态映射表未命中该设备。请宿主 AI 依据自身知识推理其标准耗材/配件型号，…",
  "queries": [{ "kind": "primary", "label": "原始关键词直搜", "items": [] }]
}
```

## 3. 目录结构

```text
server/
├── app/
│   ├── main.py                    # FastAPI 入口、lifespan、全局异常处理
│   ├── config.py                  # 环境变量与配置（全部可覆盖）
│   ├── api/
│   │   ├── deps.py                # 客户端真实 IP 解析
│   │   └── v1/
│   │       ├── search.py          # 搜索路由（并发多查询）
│   │       ├── detail.py          # 详情路由（链接/专票/质保）
│   │       └── consumable.py      # 映射查询与未命中统计
│   ├── services/
│   │   ├── maishou88.py           # 上游封装：重试/限流/请求头/清洗/专票质保解析
│   │   └── category_engine.py     # 映射匹配、未命中日志、单价换算
│   └── data/
│       ├── consumables.json       # 打印机/复合机 → 墨盒/硒鼓/色带映射
│       ├── server_hardware.json   # 服务器/电脑 → 内存/硬盘/配件映射
│       └── paper_spec.json        # 复印纸/文具规格换算表
├── logs/
│   ├── unmatched.log              # 未命中关键词记录（运行时生成）
│   └── unmatched.log.1 … .N       # 滚动分片（运行时生成）
├── tests/
│   ├── conftest.py                # 公共夹具（打桩上游 + ASGI 客户端）
│   ├── test_search.py             # 搜索接口：命中/未命中/降级/校验
│   ├── test_detail.py             # 详情接口与专票、质保解析
│   ├── test_category_engine.py    # 映射匹配与单价换算的保守性
│   └── smoke_test.py              # 独立端到端冒烟测试（不依赖 pytest）
├── pyproject.toml                 # 依赖、开发组与 pytest 配置
├── uv.lock                        # 依赖锁定（提交入库，保证各处一致）
├── .python-version                # 固定解释器版本
└── Dockerfile
```

## 4. 未命中日志

`category_engine.build_match()` 在三张映射表全部未命中时，追加写入 `logs/unmatched.log`：

```text
[2026-10-26 12:00:00] [UNMATCHED] Keyword: 奔图 P2200X | IP: 127.0.0.1 | source=0 page=1
```

设计要点：

- **按大小滚动**（详见下节）：单文件默认上限 5 MiB，保留 5 个历史分片，
  即单个部署最多占用约 30 MiB，不会随时间无限增长。
- **加锁追加** + **按整行写入**：一次 `write()` 写完一行，多 worker 下不会互相
  覆盖，也不会写出半行。
- **落盘失败不影响主流程**：磁盘只读、配额耗尽等 `OSError` 仅打印告警，
  接口照常返回 `matched: false`。
- **关键词按原文记录**（不做归一化，便于按用户真实说法补齐映射表），
  但会截断到 120 字符，保证单行大小有界。
- `GET /api/v1/consumable/unmatched` 返回总数、去重数与 Top20 高频未命中词，
  **统计口径包含全部历史分片**，因此补齐映射表的效果不会被滚动吃掉。
  **优先补齐高频机型是提升命中率的最高性价比手段。**

### 4.1 日志滚动

未命中日志是持续增长的运维数据，按大小滚动，无需外部 logrotate：

```text
unmatched.log  →  unmatched.log.1  →  unmatched.log.2  →  …  →  unmatched.log.5（超出即丢弃）
   最新分片          次新分片            更旧分片                      最旧分片
```

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `OFFICE_BUYER_LOG_MAX_BYTES` | `5242880`（5 MiB） | 单个分片大小上限，超出即滚动；`0` 表示关闭滚动 |
| `OFFICE_BUYER_LOG_BACKUP_COUNT` | `5` | 保留的历史分片数；`0` 表示滚动时直接丢弃旧内容 |

实现是自带的 `SizeRotatingLog`（约 60 行，不引入依赖），几个刻意的取舍：

- **滚动按整行发生**：先判断「当前大小 + 本行大小」是否超限，超限才轮转，
  因此不会把一行劈成两半。
- **读取自带兜底**：`read_unmatched()` / `unmatched_stats()` 会把当前分片与全部
  历史分片按时间顺序拼接，任一环节文件缺失都当作「暂无日志」，不向上抛异常。
- **多 worker 的精度**：每个 uvicorn worker 各持一个实例、各自判断大小，滚动点
  可能不完全同步。日志只用于运维统计，这个精度足够；如需严格串行，可把
  `--workers` 降到 1 或改用外部 logrotate（此时把 `OFFICE_BUYER_LOG_MAX_BYTES=0`）。
- **当前状态可查**：`GET /health` 返回 `unmatched_log_rotation`
  （上限、备份数、现存分片路径），便于确认滚动是否按预期发生。

## 5. 配置项（环境变量，均有默认值）

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `HOST` / `PORT` | `0.0.0.0` / `8446` | 监听地址与端口 |
| `OFFICE_BUYER_DATA_DIR` | `app/data` | 映射表目录 |
| `OFFICE_BUYER_LOG_DIR` | `logs` | 日志目录 |
| `OFFICE_BUYER_LOG_MAX_BYTES` | `5242880` | 未命中日志单分片大小上限（字节），`0` 关闭滚动 |
| `OFFICE_BUYER_LOG_BACKUP_COUNT` | `5` | 未命中日志保留的历史分片数 |
| `OFFICE_BUYER_TRUST_FORWARDED_FOR` | `true` | 是否信任 `X-Forwarded-For`（生产置于反代后开启） |
| `OFFICE_BUYER_CORS_ORIGINS` | `*` | 允许的来源，逗号分隔 |
| `OFFICE_BUYER_UPSTREAM_TIMEOUT` | `15.0` | 上游超时（秒） |
| `OFFICE_BUYER_UPSTREAM_RETRIES` | `3` | 上游重试次数（指数退避 + 抖动） |
| `OFFICE_BUYER_UPSTREAM_CONCURRENCY` | `5` | 上游并发上限 |
| `OFFICE_BUYER_UPSTREAM_MIN_INTERVAL` | `0.15` | 上游最小请求间隔（秒），防限流 |
| `OFFICE_BUYER_MAX_RELATED_QUERIES` | `2` | 每次搜索最多附加的关联查询数 |
| `OFFICE_BUYER_MAX_ITEMS_PER_QUERY` | `20` | 每条查询返回的商品上限 |
| `OFFICE_BUYER_UPSTREAM_OPENID` 等 | 见 `config.py` | 上游鉴权参数 |

## 6. Docker 部署

镜像内同样用 uv 安装依赖：`uv` 二进制从官方镜像拷入，`uv sync --frozen` 按
`uv.lock` 精确安装，构建产物落在 `/app/.venv`，因此构建可复现且不依赖 pip。

```bash
# 构建
docker build -t office-tech-buyer:1.1.0 .

# 运行（日志目录挂载到宿主机，便于排查未命中关键词）
docker run -d --name office-buyer \
  -p 8446:8446 \
  -v "$(pwd)/logs:/app/logs" \
  -e OFFICE_BUYER_TRUST_FORWARDED_FOR=1 \
  office-tech-buyer:1.1.0
```

`docker-compose.yml` 片段：

```yaml
services:
  office-buyer:
    build: .
    ports:
      - "8446:8446"
    volumes:
      - ./logs:/app/logs
    environment:
      OFFICE_BUYER_TRUST_FORWARDED_FOR: "1"
      OFFICE_BUYER_UPSTREAM_RETRIES: "3"
    restart: unless-stopped
```

## 7. 功能验证

pytest 套件（推荐，覆盖 89 项用例）。上游被 `httpx.MockTransport` 完全替换，
无需联网、不依赖 maishou88 可用性，也不会产生真实上游请求：

```bash
cd server
uv sync --all-groups
uv run pytest
```

按模块单跑：

```bash
uv run pytest tests/test_search.py -q
uv run pytest tests/test_category_engine.py -q
```

另有不依赖 pytest 的独立冒烟脚本（62 项断言），适合在无开发依赖的环境里快速自检：

```bash
uv run python -X utf8 tests/smoke_test.py
```

覆盖内容：映射命中、未命中日志落盘与格式、纸品单价换算的保守性
（含标题矛盾时判为 `ambiguous`）、详情专票/质保解析、未授权渠道告警、
上游 5xx 重试降级、参数校验、映射热重载、代理头信任开关。

连接真实服务器时：

```bash
# 1. 映射命中：应返回 matched=true，且 queries 含 original / compatible 两条耗材查询
uv run python ../skill/scripts/main.py search --keyword="惠普 M126a"

# 2. 未命中：应返回 matched=false，同时 logs/unmatched.log 新增一行
uv run python ../skill/scripts/main.py search --keyword="未知冷门设备型号123"

# 3. 纸品单价换算：unit_price.basis 应为 box / single_pack / ambiguous 之一
uv run python ../skill/scripts/main.py search --keyword="A4 复印纸 70g"

# 4. 详情：应返回 buy_url / invoice / warranty
uv run python ../skill/scripts/main.py detail --source=2 --id=123456789

# 5. 未命中统计
uv run python ../skill/scripts/main.py unmatched
```

## 8. 维护映射表

1. 编辑 `app/data/*.json`（保留 `_meta` 块，其余键为设备型号特征串）。
2. 无需重启：`curl -X POST http://localhost:8446/api/v1/consumable/reload`。
3. 用 `/api/v1/consumable/unmatched` 的 Top 关键词决定优先补录哪些机型。
4. `aliases` 数组请尽量补全同系列型号（如 `M126a` 补上 `M126nw/P1106/P1108`），
   它们共享同一耗材，能显著提高命中率。
