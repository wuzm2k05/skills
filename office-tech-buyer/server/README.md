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
uv run uvicorn app.main:app --host 0.0.0.0 --port 11443 --reload

# 或直接运行
uv run python -m app.main
```

启动后访问：

- 服务信息 <http://localhost:11443/>
- 健康检查 <http://localhost:11443/health>
- 交互式文档 <http://localhost:11443/docs>

> 服务默认监听 `11443`（`app/config.py` 中的 `Settings.port`），可用 `PORT` 环境变量覆盖。
> 技能端默认连生产地址，本机自测时设 `OFFICE_BUYER_SERVER_URL=http://localhost:11443`。
>
> 上面两条是开发用的前台运行（终端一关就没了）。要在服务器上**后台常驻**，见
> [第 7 节：后台常驻运行](#7-后台常驻运行启动脚本) —— `./scripts/start.sh` 一行即可。

## 2. 接口一览

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/api/v1/search` | 品类识别 + 主设备/耗材/配件并发比价 |
| GET | `/api/v1/detail` | 购买链接、淘口令、专票与质保解析 |
| GET | `/api/v1/consumable/unmatched` | 未命中关键词统计（运维） |
| POST | `/api/v1/consumable/reload` | 热重载 `app/data/*.json`（运维） |
| GET | `/health` | 健康检查（含映射表条数与日志滚动状态） |

### 示例

```bash
# 关键词命中映射表（打印机会自动带出原装 + 兼容耗材）
curl "http://localhost:11443/api/v1/search?keyword=%E6%83%A0%E6%99%AE%20M126a&source=0&page=1"

# 关键词未命中（写入 logs/unmatched.log，返回 matched=false）
curl "http://localhost:11443/api/v1/search?keyword=%E6%9C%AA%E7%9F%A5%E5%86%B7%E9%97%A8%E8%AE%BE%E5%A4%87%E5%9E%8B%E5%8F%B70123"

# 商品详情
curl "http://localhost:11443/api/v1/detail?source=2&id=123456789"
```

### `search` 响应结构

```jsonc
{
  "matched": true,                  // false 时客户端宿主 AI 需用「设备型号 + 耗材类别词」再搜一轮
  "keyword": "惠普 M126a",
  "category": "多功能一体机",
  "matched_by": "consumables",      // consumables | server_hardware | paper_spec | none
  "match_key": "M126a",
  "device_name": "惠普 HP LaserJet Pro MFP M126a",
  "attributes": { "consumable_type": "碳粉盒/硒鼓", "model_code": "CC388A / 88A",
                  "model_keywords": ["惠普 88A 碳粉盒"], "match_how": "key_substring" },
  "unit_calculation": null,         // 纸品/文具才有值，如 calculate_per_pack_price
  "channels_covered": [             // 本次**实际**返回数据的渠道，不是请求参数的回显
    { "source": 1, "platform": "淘宝/天猫", "count": 37 },
    { "source": 2, "platform": "京东",      "count": 42 },
    { "source": 3, "platform": "拼多多",     "count": 39 }
  ],
  "queries": [
    {
      "keyword": "惠普 HP LaserJet Pro MFP M126a",
      "kind": "primary",            // primary | consumable | accessory | variant
      "label": "主设备/主商品",
      "platform": "全部",
      "status": "OK",               // OK | NO_RESULTS | UPSTREAM_ERROR
      "count": 40,
      "channels": [ { "source": 2, "platform": "京东", "count": 12 } ],
      "items": [
        {
          "goodsId": "…", "source": 2, "platform": "京东",
          "title": "…", "shopName": "…",
          "originalPrice": 1299.0, "actualPrice": 1099.0, "couponPrice": null,
          "coupon_desc": "原价 ¥1299.0，券后 ¥1099.0，省 ¥200.0",
          "attributes": { "supply_type": "原装", "supply_tier": "oem_likely",
                          "model_code": "CC388A" },
          "unit_price": null          // 整机/耗材类恒为 null，仅纸品文具类有折算结果
        }
      ]
    },
    { "kind": "consumable", "label": "惠普 88A 碳粉盒", "items": [] }
  ]
}
```

> ⚠️ **`kind` 是查询分类，不是供应类型判定**。`original` / `compatible` 这组
> 旧标签已下线：上游对关键词里的「原装」「适用」**不做筛选**——「惠普 88A 碳粉盒
> 原装」上游 52 条里 `oem_likely` 只有 2 条（¥529 / ¥1009），`compatible` 47 条
> （最低 ¥5.0、中位 ¥28.0）；「联想 LT2451 粉盒 原装」48 条里 `oem_likely` 4 条
> （¥279–365）、`compatible` 28 条（¥12.6 起）。
> 供应类型一律看每行的 `attributes.supply_type`（文本信号）与
> `attributes.supply_tier`（更保守的旁路判断）。

> ⚠️ **`source=0`（"全部"）实测只覆盖淘宝/天猫、京东、拼多多三家**。跨 8 个
> 关键词 × 2 页的并集为 `{1:309, 2:320, 3:170}`，苏宁/唯品会/抖音/快手零条；
> 而单独指定 `source=4/5/7/8` 各能返回 20 条。因此覆盖范围**必须**读
> `channels_covered`，不要从请求参数推断。

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

服务器配件的 `attributes.spec_match`（**只标注，绝不过滤**）：

```jsonc
{
  "supply_type": "原装", "supply_tier": "unknown",
  "spec_match": {
    "verdict": "conflict",            // match | conflict | unknown
    "matched": [],
    "conflicts": ["3.5寸"]            // 要 2.5 寸却出现 3.5 寸这类同族冲突
  }
}
```

> 同批结果里 RDIMM 与 UDIMM 会共现（「戴尔 R750 服务器内存 DDR4 ECC」20 条里
> 两者都有），2.5 寸与 3.5 寸也 6/6 共现——按规格过滤会把结果清空，所以只标
> `conflict` 交给宿主 AI 提示用户核对。

未命中时响应形如：

```jsonc
{
  "matched": false,
  "keyword": "未知冷门设备型号123",
  "log_line": "[2026-10-26 12:00:00] [UNMATCHED] Keyword: 未知冷门设备型号123 | IP: 127.0.0.1",
  "hint": "云端静态映射表未命中该设备。请宿主 AI 改用「设备型号 + 耗材类别词」再次调用本接口…",
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
│   │       └── consumable.py      # 未命中统计与映射表热重载（运维）
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
├── run/                           # 常驻脚本的运行期文件（运行时生成，不入库）
│   ├── office-buyer.pid           # 守护循环 PID
│   ├── office-buyer.state         # 本次实例实际使用的端口/地址
│   ├── office-buyer.stop          # 停止标记（存在即表示人工停机中）
│   └── daemon.log                 # 看门狗与 uvicorn 的输出
├── scripts/                       # 常驻脚本（裸机/云主机部署用）
│   ├── start.sh                   # 后台启动 + 看门狗
│   ├── stop.sh                    # 优雅停止（超时自动升级 SIGKILL）
│   ├── restart.sh                 # stop 后 start
│   ├── status.sh                  # 进程/端口/健康检查/日志摘要
│   └── _common.sh                 # 公共函数（被上面四个 source）
├── tests/
│   ├── conftest.py                # 公共夹具（打桩上游 + ASGI 客户端）
│   ├── test_search.py             # 搜索接口：命中/未命中/降级/校验
│   ├── test_detail.py             # 详情接口与专票、质保解析
│   ├── test_category_engine.py    # 映射匹配与单价换算的保守性
│   └── smoke_test.py              # 独立端到端冒烟测试（不依赖 pytest）
├── pyproject.toml                 # 依赖、开发组与 pytest 配置
├── uv.lock                        # 依赖锁定（提交入库，保证各处一致）
├── .python-version                # 固定解释器版本
├── office-buyer.conf.example      # 常驻脚本配置模板（复制为 office-buyer.conf 后生效）
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
| `HOST` / `PORT` | `0.0.0.0` / `11443` | 监听地址与端口 |
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
| `OFFICE_BUYER_MAX_ITEMS_PER_QUERY` | `40` | 每条查询返回的商品上限（见下方说明） |
| `OFFICE_BUYER_UPSTREAM_OPENID` 等 | 见 `config.py` | 上游鉴权参数 |

> `OFFICE_BUYER_MAX_ITEMS_PER_QUERY` 不建议调低。上游把多渠道结果按相关度**交错**
> 返回（不是按渠道分段），截断因此是按渠道偏斜的——同样的行数，砍前 20 条会让
> 某一个渠道几乎清零。实测（`sourceType=0`）：
>
> | 关键词 | 上游（按渠道） | limit=20 | limit=40 |
> |---|---|---|---|
> | 戴尔 PowerEdge R750 | `{1:18, 2:20}` | `{1:18, 2:2}` | `{1:18, 2:20}` |
> | 兄弟 DCP-7180DN | `{1:18, 2:20, 3:18}` | `{1:9, 2:1, 3:10}` | `{1:17, 2:10, 3:13}` |
> | 惠普 M126a 碳粉盒 | `{1:17, 2:20, 3:20}` | `{1:11, 3:9}` | `{1:15, 2:10, 3:15}` |
> | 联想 LT2451 粉盒 | `{1:17, 2:20, 3:14}` | `{1:5, 2:4, 3:11}` | `{1:17, 2:10, 3:13}` |
>
> 被清零的渠道每轮不同（京东、天猫都出现过），但「limit=20 时必有一个渠道只剩
> 个位数」稳定成立；默认 40 能让所有渠道恢复（上游单查询上限约 60）。

## 6. Docker 部署

镜像内同样用 uv 安装依赖：`uv` 二进制从官方镜像拷入，`uv sync --frozen` 按
`uv.lock` 精确安装，构建产物落在 `/app/.venv`，因此构建可复现且不依赖 pip。

```bash
# 构建
docker build -t office-tech-buyer:1.1.0 .

# 运行（日志目录挂载到宿主机，便于排查未命中关键词）
docker run -d --name office-buyer \
  -p 11443:11443 \
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
      - "11443:11443"
    volumes:
      - ./logs:/app/logs
    environment:
      OFFICE_BUYER_TRUST_FORWARDED_FOR: "1"
      OFFICE_BUYER_UPSTREAM_RETRIES: "3"
    restart: unless-stopped
```

## 7. 后台常驻运行（启动脚本）

裸机 / 云主机部署（没有 Docker、也没有 systemd 托管）时用 `scripts/` 下的四个脚本，
让服务在后台长时间运行，崩了自动拉起：

```bash
cd server

./scripts/start.sh          # 后台启动，等健康检查通过后返回
./scripts/status.sh         # 查看进程、端口、映射表条数、日志位置
./scripts/restart.sh        # 重启
./scripts/stop.sh           # 停止

./scripts/start.sh --help   # 每个脚本都支持 --help
```

### 7.1 进程结构与看门狗

```text
start.sh（拉起守护循环后立即退出）
  └── 守护循环        ← PID 记在 run/office-buyer.pid，全程不变
        └── uvicorn   ← 退出后由循环重新拉起
```

- **崩溃自愈**：uvicorn 异常退出后按 `RESTART_DELAY`（默认 3 秒）重启；连续崩溃时
  退避时间翻倍，封顶 60 秒，恢复成功后立刻回到 3 秒。启动即崩（端口冲突、配置写错）
  不会把 CPU 和日志刷爆。
- **退出码 0 视为正常收工**，立即重启，不进退避。
- **PID 文件记的是守护循环**而不是 uvicorn：循环 PID 在整个运行期不变，stop/status
  才有稳定的抓手（每次重启后 uvicorn 的 PID 都会变）。

### 7.2 停止为什么必须用 stop.sh

看门狗的职责就是把 uvicorn 拉起来，所以**直接 `kill` uvicorn 会被立刻重启**。
`stop.sh` 按固定顺序做三件事：

1. 落一个停止标记 `run/office-buyer.stop` —— 守护循环看到它就不再拉起新进程；
2. 对「守护循环 + 整棵 uvicorn 子进程树」发 `SIGTERM` —— 走 graceful shutdown；
3. 等待退出，超过 `STOP_TIMEOUT`（默认 20 秒）自动升级为 `SIGKILL` 并确认端口已释放。

只杀守护循环是不够的：uvicorn 会被 init 收养、继续占着端口，下一次 `start.sh`
会因「端口被占用且无 PID 文件」而拒绝启动。因此 `stop.sh` 除了按 PID 树收，
还会按「命令行含 `app.main:app` 且含本次端口」全量扫描一次，把进程树已断的残留
一并收掉；Windows（Git Bash）下还会用 `netstat` 反查端口占用者并补一刀 `taskkill /T /F`。

### 7.3 配置

优先级：**命令行 / 环境变量 > `office-buyer.conf` > 脚本内置默认值**。

```bash
# 一次性覆盖
PORT=9000 WORKERS=1 ./scripts/start.sh

# 长期生效：复制模板后修改（office-buyer.conf 不入库）
cp office-buyer.conf.example office-buyer.conf
```

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `PORT` | `app/config.py` 中的 `Settings.port`（当前 `11443`） | 监听端口；脚本从源码读取，代码改了不必同步改脚本 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `WORKERS` | `2` | uvicorn worker 数 |
| `LOG_LEVEL` | `info` | uvicorn 日志级别 |
| `LOG_DIR` | `server/logs` | 未命中日志目录（即 `OFFICE_BUYER_LOG_DIR`） |
| `TRUST_PROXY` | `1` | 是否信任 `X-Forwarded-For`（即 `OFFICE_BUYER_TRUST_FORWARDED_FOR`） |
| `RESTART_DELAY` | `3` | 崩溃后重启前等待秒数（连续崩溃自动退避，封顶 60 秒） |
| `START_TIMEOUT` | `30` | `start.sh` 等待健康检查通过的秒数 |
| `STOP_TIMEOUT` | `20` | `stop.sh` 等待优雅退出的秒数 |
| `CHILD_EXIT_TIMEOUT` | `20` | 守护循环等待 uvicorn 退干净、才补 `SIGKILL` 的秒数 |
| `OFFICE_BUYER_RUNTIME_DIR` | `server/run` | PID / 状态 / 停止标记 / 守护日志目录 |
| `VENV_DIR` | `server/.venv` | 虚拟环境目录 |

端口用 `PORT=9000 ./scripts/start.sh` 起过之后，`status.sh` / `stop.sh` **不带 `PORT`
也会找到那个实例**——启动时把实际端口写进了 `run/office-buyer.state`。停止时记得
沿用启动时的参数。

### 7.4 交接给 systemd / Docker

需要开机自启时，把守护循环当前台进程交给 systemd（`Type=simple`）即可：

```ini
[Service]
WorkingDirectory=/opt/office-buyer
Environment=OFFICE_BUYER_TRUST_FORWARDED_FOR=1
ExecStart=/opt/office-buyer/scripts/start.sh --foreground
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=mixed
```

`--foreground` 模式下不再 `setsid`，脚本本身就是主进程，`systemctl stop` 发出的
`SIGTERM` 会被转达给 uvicorn 并等它退干净。此时**不要**再设 `Restart=always`——
脚本内置的看门狗已经在做重启，两层叠加只会让退避策略互相打架。

脚本启动前会解析 `server/.venv` 下的 uvicorn；找不到时自动执行一次
`uv sync --frozen --no-dev`（与 Dockerfile 同一条命令，按 `uv.lock` 精确安装），
因此新机器上只要装了 `uv`，`./scripts/start.sh` 一把就能跑起来。

## 8. 功能验证

pytest 套件（推荐，覆盖 112 项用例）。上游被 `httpx.MockTransport` 完全替换，
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

另有不依赖 pytest 的独立冒烟脚本（71 项断言），适合在无开发依赖的环境里快速自检：

```bash
uv run python -X utf8 tests/smoke_test.py
```

覆盖内容：映射命中、未命中日志落盘与格式、纸品单价换算的保守性
（含标题矛盾时判为 `ambiguous`）、详情专票/质保解析与 `goodsId` 回填、未授权渠道告警、
配件关键词的词序与规格标注、上游 5xx 重试降级、参数校验、映射热重载、代理头信任开关。

连接真实服务器时：

```bash
# 1. 映射命中：应返回 matched=true，queries 含 primary + consumable，
#    channels_covered 应只出现 1/2/3（source=0 实测只覆盖这三家）
uv run python ../skill/scripts/main.py search --keyword="惠普 M126a"

# 2. 未命中：应返回 matched=false，同时 logs/unmatched.log 新增一行
uv run python ../skill/scripts/main.py search --keyword="未知冷门设备型号123"

# 3. 纸品单价换算：unit_price.basis 应为 box / single_pack / ambiguous 之一
uv run python ../skill/scripts/main.py search --keyword="A4 复印纸 70g"

# 4. 鼓粉分离机型：应同时出现粉盒与硒鼓两条 consumable 查询
uv run python ../skill/scripts/main.py search --keyword="兄弟 DCP-7180DN"

# 5. 详情：应返回 buy_url / invoice / warranty，且 goodsId 等于传入的 id
uv run python ../skill/scripts/main.py detail --source=2 --id=123456789

# 6. 未命中统计
uv run python ../skill/scripts/main.py unmatched
```

## 9. 维护映射表

1. 编辑 `app/data/*.json`（保留 `_meta` 块，其余键为设备型号特征串）。
2. 无需重启：`curl -X POST http://localhost:11443/api/v1/consumable/reload`。
3. 用 `/api/v1/consumable/unmatched` 的 Top 关键词决定优先补录哪些机型。
4. `aliases` 数组请尽量补全同系列型号（如 `M126a` 补上 `M126nw/P1106/P1108`），
   它们共享同一耗材，能显著提高命中率。

### 写关键词的三条硬规矩（都有实测依据）

这些规律来自对上游的真实探测，不遵守就会白占 `max_related_queries` 配额或直接归零：

| 规矩 | 依据 |
| :--- | :--- |
| 词序必须是 `{设备} {类别词} {规格...}` | 「戴尔 R750 服务器内存 ECC RDIMM」21 条，把规格挪到类别词之前只剩 **1 条**（`sourceType=2` 上为 0） |
| 不写容量（32G/64G/128G）、不写频率 | 「戴尔 R750 服务器内存」40 条 → 加 `32G` 25 条 → 加 `DDR4 ECC 32G` 14 条；加 `ECC RDIMM 3200MHz` **0 条**。容量与频率由用户需求决定 |
| 不写形态词（RDIMM/UDIMM）与代际（DDR4/DDR5） | 上游把它们当精确筛选：8 台服务器同一批 base，只追加 `ECC` 246 条，追加到 `DDR4 ECC` 190 条，再到 `DDR4 ECC RDIMM` 只剩 **70 条**。形态与代际交给 `spec_match` 逐行标注 |
| 不写「原装 / 适用」后缀 | 上游不按这两个词筛选：「惠普 88A 碳粉盒 原装」52 条里 `oem_likely` 只有 2 条（¥529/¥1009），`compatible` 47 条（中位 ¥28.0） |

鼓粉分离的机型（`consumable_type` 含「+」）必须在 `model_keywords` 里给出**两条**，
让粉盒与硒鼓各自比价——DCP-7180DN 的粉盒与硒鼓差一个价位档（3 轮中位价：粉盒
¥32.4 / ¥90.0 / ¥35.9，硒鼓 ¥82.1 / ¥92.5 / ¥87.5），漏掉的是更贵的一半。

### 上游的两个已知局限

- **同关键词两次请求结果偶尔整批换**：7 个关键词 × 3 组实测 Jaccard 0.72–1.0
  （多数在 0.9 以上），但低谷出现过 0.72。做「这条查询有没有用」的判断时仍应
  多轮并集，单轮对比会被噪声淹没。
- **`sourceType=0` 只覆盖 1/2/3**：苏宁、唯品会、抖音、快手需要显式传 `source=4/5/7/8`。
