# 办公设备易采买 —— Server 配置手册

本文是 `office-tech-buyer/server` 的**完整配置参考**：配置从哪里来、有哪些可调项、
每种部署方式怎么落地、改完怎么验证、出问题怎么查。

> 想先跑起来看效果，看 [README 第 1 节「快速开始」](../README.md#1-快速开始)；
> 本文关注的是**把服务按你的环境配好并长期稳定运行**。
>
> 目标读者：部署/运维本服务的人（不要求读过 Python 源码，但需要一台机器和 shell）。

---

## 目录

1. [配置全景：三层来源与优先级](#1-配置全景三层来源与优先级)
2. [服务端配置项总表（环境变量）](#2-服务端配置项总表环境变量)
   - [2.1 服务自身](#21-服务自身)
   - [2.2 数据与日志路径](#22-数据与日志路径)
   - [2.3 未命中日志滚动](#23-未命中日志滚动)
   - [2.4 上游 maishou88 接入](#24-上游-maishou88-接入)
   - [2.5 上游容错与限流](#25-上游容错与限流)
   - [2.6 业务策略](#26-业务策略)
   - [2.7 LLM 智能化运维](#27-llm-智能化运维)
3. [常驻脚本配置项（office-buyer.conf）](#3-常驻脚本配置项office-buyerconf)
4. [四种部署方式的配置落地](#4-四种部署方式的配置落地)
   - [4.1 本机开发](#41-本机开发)
   - [4.2 Linux 云主机常驻（推荐）](#42-linux-云主机常驻推荐)
   - [4.3 Docker / docker-compose](#43-docker--docker-compose)
   - [4.4 systemd 托管](#44-systemd-托管)
   - [4.5 Nginx 反向代理](#45-nginx-反向代理)
5. [配置生效方式与验证](#5-配置生效方式与验证)
6. [客户端（技能端）如何指向本服务](#6-客户端技能端如何指向本服务)
7. [常见配置错误与排查](#7-常见配置错误与排查)
8. [附录：可直接抄的完整配置样例](#8-附录可直接抄的完整配置样例)

---

## 1. 配置全景：三层来源与优先级

服务由**两部分**组成，各自有自己的配置通道，互不覆盖：

```text
                    ┌─────────────────────────────────────────┐
   业务/运行参数 →  │ app/config.py 读取 OFFICE_BUYER_* 环境变量 │ → 限流、上游地址、
                    │ （映射表路径、日志滚动、LLM 等）           │   业务策略、日志目录
                    └─────────────────────────────────────────┘
                    ┌─────────────────────────────────────────┐
   进程/脚本参数 →  │ scripts/*.sh 读取 shell 变量 + conf 文件   │ → 端口、worker 数、
                    │ （HOST / PORT / WORKERS / …）             │   重启策略、运行目录
                    └─────────────────────────────────────────┘
                        两个通道共用 HOST / PORT 两个名字
```

**脚本层的优先级**（`scripts/_common.sh` 实现）：

```text
命令行 / 环境变量  >  server/office-buyer.conf  >  脚本内置默认值
   PORT=9000 ./start.sh        office-buyer.conf          （端口还会回落读 app/config.py）
```

**业务层的优先级**：

```text
环境变量 OFFICE_BUYER_*  >  app/config.py 里的默认值
```

### 三个必须记住的规则

| 规则 | 说明 |
| :--- | :--- |
| **`OFFICE_BUYER_*` 只认环境变量** | 没有配置文件、没有命令行开关。要用 systemd 就写 `Environment=`，要用 Docker 就写 `environment:` |
| **PHP 风格的空串等于没设** | `config.py` 里空串一律回落默认值。想「清空」某个值做不到，只能改默认值或换变量值 |
| **改环境变量要重启进程** | `Settings` 在进程启动时固化（`settings = Settings()`）。只有映射表 JSON 能热重载 |

> ⚠️ `PORT` / `HOST` 是**两个通道共用**的名字。用 `scripts/start.sh` 启动时由脚本读取并
> `export` 给 uvicorn；用 `docker run` 启动时由 Dockerfile 的 `ENV PORT=11443` 提供。
> 手工跑 `uvicorn` 时，`PORT` 环境变量**不会**被自动使用——uvicorn 只认 `--port` 参数
> （`python -m app.main` 是唯一例外，它自己会读 `settings.port`）。

---

## 2. 服务端配置项总表（环境变量）

以下全部定义在 `app/config.py`，**全部有默认值**，零配置即可在本机启动。

### 2.1 服务自身

| 变量 | 默认值 | 类型 | 说明 |
| :--- | :--- | :--- | :--- |
| `HOST` | `0.0.0.0` | str | 监听地址。只在 `python -m app.main` 路径下自动生效；用 uvicorn/脚本启动时由它们传 `--host` |
| `PORT` | `11443` | int | 监听端口。同上：脚本与 Docker 都会读它，手工 uvicorn 需自己传 `--port` |
| `OFFICE_BUYER_CORS_ORIGINS` | `*` | 逗号分隔 | 允许的浏览器来源。**给其他网页调用才需要**；技能端是服务端转发的 CLI，不受 CORS 限制 |
| `OFFICE_BUYER_TRUST_FORWARDED_FOR` | `true` | bool | 是否信任 `X-Forwarded-For` / `X-Real-IP` 作为客户端 IP |

关于 `TRUST_FORWARDED_FOR`，这是**安全相关**的一项，两种场景必须选对：

| 部署形态 | 应设 | 原因 |
| :--- | :--- | :--- |
| 置于 Nginx / SLB / 网关之后 | `1`（默认） | 未命中日志才能记到真实用户 IP；此时 TCP 对端是网关地址，没有参考价值 |
| 直接暴露公网 / 端口对公网开放 | `0` | 否则任何人加一个 `X-Forwarded-For` 头就能往日志里写任意 IP，日志不可信 |

> 布尔值的合法写法：`1 / true / yes / on / y`（不区分大小写）为真，其余为假。

### 2.2 数据与日志路径

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `OFFICE_BUYER_DATA_DIR` | `server/app/data` | 三张映射表 JSON 所在目录（`consumables.json` / `server_hardware.json` / `paper_spec.json`） |
| `OFFICE_BUYER_LOG_DIR` | `server/logs` | 未命中关键词日志目录 |

两个目录服务启动时会自动 `mkdir -p`，但**要求父目录可写**。生产上建议：

- `OFFICE_BUYER_LOG_DIR` 指到一个独立的数据盘/日志分区（`/var/log/office-buyer`），
  避免日志把系统盘写满；
- `OFFICE_BUYER_DATA_DIR` 用默认位置即可——除非你要用「配置即代码」的方式管理映射表，
  把它指到一个 git 仓库目录，再用 `/api/v1/consumable/reload` 热重载。

> 如果开了 LLM 智能化运维（见 2.7），`DATA_DIR` **必须可写**——它会直接改写 JSON 文件。

### 2.3 未命中日志滚动

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `OFFICE_BUYER_LOG_MAX_BYTES` | `5242880`（5 MiB） | 单个分片上限，超出即滚动；`0` 表示关闭滚动（文件会一直长） |
| `OFFICE_BUYER_LOG_BACKUP_COUNT` | `5` | 保留的历史分片数；`0` 表示滚动时直接丢弃旧内容 |

滚动形如：

```text
unmatched.log → unmatched.log.1 → unmatched.log.2 → … → unmatched.log.5（超出即丢弃）
   最新分片        次新分片           更旧分片                   最旧分片
```

**容量上限 = `LOG_MAX_BYTES × (LOG_BACKUP_COUNT + 1)`**，默认约 30 MiB，
不会随时间无界增长。运维统计（`/api/v1/consumable/unmatched`）会读**全部历史分片**，
所以补齐映射表的效果不会被滚动吃掉。

两个刻意的取舍，配置时要心里有数：

- **滚动由各 worker 独立判断**。`WORKERS=2` 时两个进程各自统计大小，滚动点可能不同步。
  日志只用于运维统计，这个精度通常够；要严格串行就把 `WORKERS=1`，或改用外部
  logrotate（此时把 `LOG_MAX_BYTES=0` 关掉自带滚动，避免两套轮转互相打架）。
- **滚动按整行发生**（先算「当前大小 + 本行大小」再决定是否轮转），不会写出半行。

### 2.4 上游 maishou88 接入

上游是「什么值得买式」的聚合返利接口。以下默认值**内置可用**，一般不需要改：

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `OFFICE_BUYER_UPSTREAM_SEARCH_URL` | `https://appapi.maishou88.com/api/v1/homepage/searchList` | 搜索接口 |
| `OFFICE_BUYER_UPSTREAM_DETAIL_URL` | `https://appapi.maishou88.com/api/v3/goods/detail` | 商品详情接口 |
| `OFFICE_BUYER_UPSTREAM_TARGET_URL` | `https://msapi.maishou88.com/api/v1/share/getTargetUrl` | 转链接口（购买链接 / 淘口令） |
| `OFFICE_BUYER_UPSTREAM_OPENID` | 内置值 | 上游鉴权 openid，**同时是请求头与表单字段** |
| `OFFICE_BUYER_UPSTREAM_VERSION` | `3.7.7.2` | App 版本号（上游会校验） |
| `OFFICE_BUYER_UPSTREAM_INVITE_CODE` | `95953233` | 转链/详情用的邀请码（返利归属） |
| `OFFICE_BUYER_UPSTREAM_APP_UA` | `MaiShouApp/3.7.7 (iPhone; iOS 26.3; Scale/3.00)` | 搜索接口的 UA 伪装 |
| `OFFICE_BUYER_UPSTREAM_WEB_UA` | `Mozilla/5.0 AppleWebKit/537 …` | 详情/转链接口的 UA 伪装 |
| `OFFICE_BUYER_UPSTREAM_REFERER` | `https://hnbc018.kuaizhan.com/` | Referer 伪装 |

**什么时候需要改这里：**

| 场景 | 动作 |
| :--- | :--- |
| 上游换域名 / 换路径 | 改 `*_URL`，重启服务即可，无需改代码 |
| 换一个返利账号（换 openid / 邀请码） | 改 `UPSTREAM_OPENID` + `UPSTREAM_INVITE_CODE`。**注意**：`openid` 同时作为搜索接口的请求头和请求体字段，两个变量必须配套改 |
| 上游升级 App 版本，旧版本被拒 | 改 `UPSTREAM_VERSION` 与两个 UA（UA 里的版本号建议同步） |

> ⚠️ 这些默认值是**公开的上游客户端凭据**，不含你个人的密钥。但如果上游按 openid
> 做风控，把默认 openid 用于生产可能与他人共享配额——上线前建议换成自己的账号值。
> 这些值通过环境变量注入即可，**不要**改源码（入库会泄露，也容易在合并时冲突）。

### 2.5 上游容错与限流

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `OFFICE_BUYER_UPSTREAM_TIMEOUT` | `15.0` | 单次请求总超时（秒） |
| `OFFICE_BUYER_UPSTREAM_CONNECT_TIMEOUT` | `5.0` | 建连超时（秒） |
| `OFFICE_BUYER_UPSTREAM_RETRIES` | `3` | 最大尝试次数（含首次）。5xx / 429 / 超时 / 非 JSON 都重试 |
| `OFFICE_BUYER_UPSTREAM_BACKOFF` | `0.4` | 重试退避基数（秒），实际为 `backoff × 2^(n-1)` + 0~30% 抖动 |
| `OFFICE_BUYER_UPSTREAM_CONCURRENCY` | `5` | **单进程**对上上游的并发上限（asyncio 信号量） |
| `OFFICE_BUYER_UPSTREAM_MIN_INTERVAL` | `0.15` | **单进程**内两次上游请求的最小间隔（秒），防触发风控 |

两个容易踩的坑：

1. **限流是「进程内」的，`WORKERS` 会成倍放大它。**
   有效上游速率 ≈ `WORKERS ÷ UPSTREAM_MIN_INTERVAL`。默认 `WORKERS=2` + `0.15s`
   约等于 13 请求/秒。把 `WORKERS` 提到 8，就等于 53 请求/秒——很可能被上游风控。
   **调大 worker 数时同步调大 `MIN_INTERVAL`**，或者先压测再决定。
2. **超时是「每次尝试」的，不是整条请求的。**
   `TIMEOUT=15` + `RETRIES=3` 时，最坏情况一条上游请求会占 45 秒以上。
   客户端（技能端）的 HTTP 超时是 60 秒，`search` 接口一次要发多条上游请求
   （主查询 + 关联查询），**不要**把 `TIMEOUT` 调到 30 秒以上，否则客户端会先超时。

### 2.6 业务策略

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `OFFICE_BUYER_MAX_RELATED_QUERIES` | `2` | 每次搜索**最多**附加的关联查询数（如「原装耗材」「兼容耗材」各一条） |
| `OFFICE_BUYER_MAX_ITEMS_PER_QUERY` | `40` | 每条查询保留的商品上限 |
| `OFFICE_BUYER_DEFAULT_PACKS_PER_BOX` | `5` | 纸品折算：标题没写整箱包数时的默认假设值 |
| `OFFICE_BUYER_DEFAULT_SHEETS_PER_PACK` | `500` | 纸品折算：标题没写单包张数时的默认假设值 |

**`MAX_ITEMS_PER_QUERY` 不建议调低**（这一点有实测依据，不是保守猜测）：

上游把多渠道结果按相关度**交错**返回，不是按渠道分段，所以截断是按渠道偏斜的。
实测（`source=0`）：

| 关键词 | 上游条数（按渠道） | `limit=20` | `limit=40` |
| :--- | :--- | :--- | :--- |
| 戴尔 PowerEdge R750 | `{1:18, 2:20}` | `{1:18, 2:2}` | `{1:18, 2:20}` |
| 兄弟 DCP-7180DN | `{1:18, 2:20, 3:18}` | `{1:9, 2:1, 3:10}` | `{1:17, 2:10, 3:13}` |
| 惠普 M126a 碳粉盒 | `{1:17, 2:20, 3:20}` | `{1:11, 3:9}` | `{1:15, 2:10, 3:15}` |
| 联想 LT2451 粉盒 | `{1:17, 2:20, 3:14}` | `{1:5, 2:4, 3:11}` | `{1:17, 2:10, 3:13}` |

`limit=20` 时**总有一个渠道只剩个位数**（被清零的渠道每轮不同），默认 40 能让所有渠道恢复。
上游单查询上限约 60，所以 40 是「完整拿到一轮」的合理值。

**`MAX_RELATED_QUERIES` 的取舍**：每条关联查询都是一次额外的上游请求。
调到 `3` 能覆盖「粉盒 + 硒鼓 + 配件」都带的机型，但单次 `search` 的上游请求数
就从 3 条涨到 4 条，延迟与风控压力同步上升。默认 2 是延迟与覆盖的平衡点。

### 2.7 LLM 智能化运维

用于**自动补齐映射表**：分析未命中日志的高频词 → 调上游搜索 → 让 LLM 提取机型与
耗材/配件映射 → 直接改写 `app/data/*.json`。

默认**关闭**。它是一个可选能力，不是服务运行的必要条件。

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `OFFICE_BUYER_LLM_ENABLED` | `false` | 总开关。`false` 时接口返回 `{"status":"disabled"}` |
| `OFFICE_BUYER_LLM_API_KEY` | 空 | API Key。**未配置时即使开关为 true 也不工作** |
| `OFFICE_BUYER_LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容的 base url（不含 `/chat/completions`） |
| `OFFICE_BUYER_LLM_MODEL` | `gpt-4o` | 模型名 |
| `OFFICE_BUYER_LLM_TIMEOUT` | `30.0` | 单次 LLM 调用超时（秒） |

启用示例（自建/国内 OpenAI 兼容网关同理，只换 `BASE_URL` 与 `MODEL`）：

```bash
export OFFICE_BUYER_LLM_ENABLED=1
export OFFICE_BUYER_LLM_API_KEY=sk-xxxxxxxx
export OFFICE_BUYER_LLM_BASE_URL=https://api.openai.com/v1
export OFFICE_BUYER_LLM_MODEL=gpt-4o
```

启用后调用（运维接口，无鉴权，**不要**把服务直接暴露公网）：

```bash
curl -X POST http://127.0.0.1:11443/api/v1/consumable/llm-update
```

启用前的三条注意事项：

- **`OFFICE_BUYER_DATA_DIR` 必须可写**，否则补录静默失败（日志里有一行 `更新映射表失败`）。
- **它会改你的数据文件**。Docker 部署时若不挂载 `app/data`，改动在容器重建后丢失。
  生产上建议先备份，或把 `DATA_DIR` 指到一个 git 管理目录，改完人工 review 再提交。
- **它每次最多处理 5 个高频未命中词**，且只做「新增键」或「合并 aliases」，
  不会删除已有条目。跑一次不等于补齐全表，需要反复跑 + 人工核对。

---

## 3. 常驻脚本配置项（office-buyer.conf）

`scripts/` 下的四个脚本（`start.sh` / `stop.sh` / `restart.sh` / `status.sh`）共用
`_common.sh`，后者按下面的顺序装载配置：

```text
1) 读 $OFFICE_BUYER_CONF（默认 server/office-buyer.conf，不存在则跳过）
2) 补内置默认值（「变量已设置就不覆盖」的写法）
```

### 3.1 变量总表

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `HOST` | `0.0.0.0` | 监听地址，传给 `uvicorn --host` |
| `PORT` | 从 `app/config.py` 读取（当前 `11443`） | 监听端口。**不写死在脚本里**，避免两处手改不同步 |
| `WORKERS` | `2` | uvicorn worker 数 |
| `LOG_LEVEL` | `info` | uvicorn 日志级别：`critical/error/warning/info/debug/trace` |
| `LOG_DIR` | `server/logs` | 导出为 `OFFICE_BUYER_LOG_DIR`（服务端日志目录） |
| `TRUST_PROXY` | `1` | 导出为 `OFFICE_BUYER_TRUST_FORWARDED_FOR` |
| `RESTART_DELAY` | `3` | 崩溃后重启前等待秒数；连续崩溃指数退避，封顶 60 秒 |
| `START_TIMEOUT` | `30` | `start.sh` 等待健康检查通过的秒数 |
| `STOP_TIMEOUT` | `20` | `stop.sh` 等待优雅退出的秒数，超时自动升级 `SIGKILL` |
| `CHILD_EXIT_TIMEOUT` | `20` | 守护循环等 uvicorn 退干净的秒数，超时才补 `SIGKILL` |
| `OFFICE_BUYER_RUNTIME_DIR` | `server/run` | PID / 状态 / 停止标记 / 守护日志目录 |
| `VENV_DIR` | `server/.venv` | 虚拟环境目录 |
| `OFFICE_BUYER_CONF` | `server/office-buyer.conf` | 换一个配置文件位置 |
| `HTTP_TIMEOUT` | `5` | 脚本内部健康检查请求超时（秒） |
| `NO_COLOR` | 未设 | 设任意值即关闭脚本输出的 ANSI 颜色（写日志/CI 时有用） |
| `OFFICE_BUYER_NO_GUARD` | 未设 | 设任意值则跳过「已有实例在跑」的检查（脚本编排用） |

### 3.2 配置模板与落地

模板在 `office-buyer.conf.example`，**复制后修改**（`office-buyer.conf` 已在
`.gitignore` 中忽略，每台机器可以不同）：

```bash
cd server
cp office-buyer.conf.example office-buyer.conf
$EDITOR office-buyer.conf
```

一次性覆盖（不落文件）：

```bash
PORT=9000 WORKERS=1 LOG_LEVEL=debug ./scripts/start.sh
```

### 3.3 `run/office-buyer.state`：脚本间的状态传递

`start.sh` 会把**本次实际使用**的端口/地址写进 `run/office-buyer.state`：

```text
PORT=9000
HOST=0.0.0.0
WORKERS=2
SERVER_DIR=/root/src/skills/office-tech-buyer/server
```

`stop.sh` / `status.sh` 启动时读回它，所以：

- `PORT=9000 ./scripts/start.sh` 起的服务，**不带 `PORT` 也能被 `stop.sh` 找到**；
- 反过来，**命令行显式传的 `PORT` / `HOST` 仍然优先**，方便临时指向别的实例。

`stop.sh` 结束时删除 PID 文件与 state 文件，但**保留** `run/office-buyer.stop`
停止标记（下次 `start.sh` 自动清除）——这是判断「人工停机中」的凭据。

---

## 4. 四种部署方式的配置落地

### 4.1 本机开发

零配置就能跑，依赖由 [uv](https://docs.astral.sh/uv/) 按 `pyproject.toml` + `uv.lock`
精确还原（解释器版本由 `.python-version` 固定为 3.11）：

```bash
cd server
uv sync --all-groups          # 含开发/测试组
uv run uvicorn app.main:app --host 0.0.0.0 --port 11443 --reload
```

需要临时调某个业务参数：

```bash
# PowerShell
$env:OFFICE_BUYER_MAX_ITEMS_PER_QUERY="60"; uv run uvicorn app.main:app --port 11443 --reload

# bash
OFFICE_BUYER_MAX_ITEMS_PER_QUERY=60 uv run uvicorn app.main:app --port 11443 --reload
```

### 4.2 Linux 云主机常驻（推荐）

裸机 / 云主机且没有 Docker、也没有 systemd 托管时，用自带脚本让服务后台常驻、崩溃自愈。

**进程结构：**

```text
start.sh（拉起守护循环后立即退出）
  └── 守护循环        ← PID 记在 run/office-buyer.pid，全程不变
        └── uvicorn   ← 崩溃后由循环重新拉起（指数退避，封顶 60s）
```

**配置步骤：**

```bash
cd server

# 1) 依赖（脚本也会在缺失时自动补，但提前装好启动更快）
uv sync --frozen --no-dev

# 2) 本机专属参数
cp office-buyer.conf.example office-buyer.conf
$EDITOR office-buyer.conf        # 至少确认 PORT / WORKERS / TRUST_PROXY

# 3) 启动 / 查看 / 重启 / 停止
./scripts/start.sh
./scripts/status.sh
./scripts/restart.sh
./scripts/stop.sh
```

**生产建议：**

| 建议 | 理由 |
| :--- | :--- |
| `LOG_DIR=/var/log/office-buyer` | 日志与代码分离，系统盘不被写满 |
| `TRUST_PROXY=1` + 前面挂 Nginx/SLB | 未命中日志能记到真实用户 IP |
| `WORKERS` 从 2 起，压测后再加 | 上游限流是进程内的，worker 数翻倍 = 上游压力翻倍 |
| `OFFICE_BUYER_NO_GUARD` **不要**设 | 关掉重复实例检查后，两个实例抢同一端口只会反复崩溃 |

**⚠️ 停止必须用 `stop.sh`，不要直接 `kill` uvicorn**——看门狗的职责就是把它拉起来，
直接 kill 会被立刻重启。`stop.sh` 的三步（落停止标记 → `SIGTERM` 整棵进程树 →
超时升级 `SIGKILL`）顺序不能反。

脚本还会兜住两类残留：进程树已断的 uvicorn（按「命令行含 `app.main:app` 且含本次端口」
全量扫描），以及 Windows（Git Bash）下用 `netstat` 反查端口占用者后补 `taskkill /T /F`。

### 4.3 Docker / docker-compose

镜像用 uv 按 `uv.lock` 精确安装（`uv sync --frozen --no-dev`），构建可复现、不依赖 pip。

```bash
cd server
docker build -t office-tech-buyer:1.1.0 .

docker run -d --name office-buyer \
  -p 11443:11443 \
  -v "$(pwd)/logs:/app/logs" \
  -v "$(pwd)/app/data:/app/app/data" \
  -e OFFICE_BUYER_TRUST_FORWARDED_FOR=1 \
  -e OFFICE_BUYER_LOG_MAX_BYTES=5242880 \
  office-tech-buyer:1.1.0
```

`docker-compose.yml`：

```yaml
services:
  office-buyer:
    build: .
    image: office-tech-buyer:1.1.0
    ports:
      - "11443:11443"
    volumes:
      # 未命中日志：不挂载则容器重建后丢失
      - ./logs:/app/logs
      # 映射表：只有启用 LLM 自动补录时才需要挂载（否则会被镜像内版本覆盖）
      - ./app/data:/app/app/data
    environment:
      OFFICE_BUYER_TRUST_FORWARDED_FOR: "1"
      OFFICE_BUYER_UPSTREAM_RETRIES: "3"
      OFFICE_BUYER_MAX_ITEMS_PER_QUERY: "40"
      # LLM 智能化运维（可选）
      # OFFICE_BUYER_LLM_ENABLED: "1"
      # OFFICE_BUYER_LLM_API_KEY: "sk-xxxx"
      # OFFICE_BUYER_LLM_BASE_URL: "https://api.openai.com/v1"
      # OFFICE_BUYER_LLM_MODEL: "gpt-4o"
    restart: unless-stopped
```

**Docker 相关的三个配置要点：**

1. **`PORT` 有默认值也有坑。** Dockerfile 里 `ENV PORT=11443`，`CMD` 用的是
   `${PORT:-11443}`，所以 `-e PORT=9000 -p 9000:9000` 能改，但**端口映射要自己同步改**。
2. **镜像内的 `--workers 2` 是写死的**（在 `CMD` 里），不是 `WORKERS` 变量。
   要改 worker 数得覆盖 `command`：
   ```yaml
   command: ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-11443} --workers 4 --proxy-headers --forwarded-allow-ips='*'"]
   ```
3. **`--proxy-headers --forwarded-allow-ips='*'` 已经在 CMD 里**，
   配合 `TRUST_FORWARDED_FOR=1` 才能拿到真实客户端 IP。手工起容器时别漏掉这两个参数。

**健康检查**已内置（`HEALTHCHECK` 打 `/health`），`docker ps` 的 STATUS 列会显示
`healthy` / `unhealthy`，无需额外配置。

### 4.4 systemd 托管

需要开机自启时，把守护循环当**前台进程**交给 systemd：

```ini
# /etc/systemd/system/office-buyer.service
[Unit]
Description=Office Tech Buyer (办公设备易采买 云端服务)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=office-buyer
WorkingDirectory=/opt/office-buyer/server
Environment=OFFICE_BUYER_TRUST_FORWARDED_FOR=1
Environment=OFFICE_BUYER_LOG_DIR=/var/log/office-buyer
Environment=OFFICE_BUYER_MAX_ITEMS_PER_QUERY=40
# LLM 的 KEY 建议放独立文件，权限 600，避免出现在 systemctl show 输出里
EnvironmentFile=-/etc/office-buyer.env
ExecStart=/opt/office-buyer/server/scripts/start.sh --foreground
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=mixed

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now office-buyer
systemctl status office-buyer
journalctl -u office-buyer -f
```

**三条硬性要求：**

| 要求 | 原因 |
| :--- | :--- |
| 必须带 `--foreground` | 否则脚本会 `setsid` 脱离，systemd 认为主进程已退出，服务被判为失败 |
| **不要**设 `Restart=always` | 脚本内置看门狗已在重启；两层叠加会让退避策略互相打架。用 `on-failure` |
| `KillMode=mixed` | 让 `SIGTERM` 送达到整棵进程树，uvicorn 才能优雅停机 |

### 4.5 Nginx 反向代理

服务默认监听 `0.0.0.0:11443` 且**不带任何鉴权**（运维接口也开放），
生产上**不要**把 11443 直接暴露公网，前面挂 Nginx。

**变体 A：挂在子路径下（生产实际用法）**

技能端默认地址是 `https://api.ninenode.com/api/office-buyer`，
即所有请求都带 `/api/office-buyer` 前缀：

```nginx
location /api/office-buyer/ {
    # proxy_pass 末尾的 / 表示「剥掉 location 前缀」再转发给上游
    proxy_pass http://127.0.0.1:11443/;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # 上游一次请求要打多条上游接口，超时要给足
    proxy_connect_timeout 10s;
    proxy_send_timeout    90s;
    proxy_read_timeout    90s;
}
```

> ⚠️ 剥前缀后，uvicorn 不知道自己的外部路径，`/docs` 页面里的链接会指向 `/docs` 而打不开
> ——**业务接口不受影响**（技能端自己拼 `/api/v1/...`），只有交互式文档页会坏。
> 想修好文档页，需要给 uvicorn 加 `--root-path /api/office-buyer`。
> `scripts/start.sh` 的启动命令是写死的（`app.main:app --host … --port … --workers …`），
> **不读 `--root-path`**；要加就把脚本里那行 `"$UVICORN_BIN" app.main:app \` 的参数列表
> 补上，或改用 systemd 直接托管 uvicorn 命令。

**变体 B：独占域名/端口（外部路径与内部一致）**

最简单、不会踩 `root_path` 的坑：

```nginx
server {
    listen 443 ssl;
    server_name office-buyer.example.com;

    # 业务接口
    location /api/v1/ {
        proxy_pass http://127.0.0.1:11443;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_read_timeout 90s;
    }

    # 健康检查：开放给监控，不对外暴露其余接口
    location = /health {
        proxy_pass http://127.0.0.1:11443/health;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # 运维接口：只允许内网
    location /api/v1/consumable/ {
        allow 10.0.0.0/8;
        allow 172.16.0.0/12;
        allow 192.168.0.0/16;
        deny all;
        proxy_pass http://127.0.0.1:11443;
    }

    # 交互式文档：按需开放或直接关掉
    location /docs { allow 10.0.0.0/8; deny all; proxy_pass http://127.0.0.1:11443; }
}
```

**配套要求：**

| 项目 | 配置 |
| :--- | :--- |
| 服务端 | `OFFICE_BUYER_TRUST_FORWARDED_FOR=1`，且 uvicorn 带 `--proxy-headers --forwarded-allow-ips='*'` |
| 超时 | `proxy_read_timeout` 至少 90s：`search` 要发多条上游请求，最坏情况几十秒 |
| 限流 | 建议在 Nginx 层加 `limit_req`，因为服务本身**没有速率限制** |
| 日志 | 未命中日志里记的 IP 来自 `X-Forwarded-For` 的第一段，确保中间没有别的代理覆盖它 |

---

## 5. 配置生效方式与验证

| 改了什么 | 生效方式 | 命令 |
| :--- | :--- | :--- |
| `office-buyer.conf`（端口/worker 等） | 重启服务 | `./scripts/restart.sh` |
| `OFFICE_BUYER_*` 环境变量 | 重启服务（`Settings` 在进程启动时固化） | `./scripts/restart.sh` |
| `app/data/*.json` 映射表 | **不用重启**，热重载 | `curl -X POST http://127.0.0.1:11443/api/v1/consumable/reload` |
| `app/**/*.py` 源码 | 重启服务 | `./scripts/restart.sh` |
| systemd 单元文件 | reload + restart | `systemctl daemon-reload && systemctl restart office-buyer` |
| Docker `environment` | 重建容器 | `docker compose up -d --force-recreate` |

**改完一定要验证这三项：**

```bash
# 1) 健康检查：确认版本、映射表条数、日志滚动参数都符合预期
curl -sS http://127.0.0.1:11443/health | python -m json.tool
```

关键字段解读：

| 字段 | 期望 | 不符时说明 |
| :--- | :--- | :--- |
| `status` | `ok` | 非 `ok` 说明进程没正常起来 |
| `version` | 与 `pyproject.toml` 一致 | 不一致说明跑的是旧代码，忘重启了 |
| `mappings.consumables / server_hardware / paper_spec` | 非 0（当前生产为 26 / 18 / 15） | 全为 0 说明 `OFFICE_BUYER_DATA_DIR` 指错了目录 |
| `unmatched_log` | 你配置的 `LOG_DIR` 下的路径 | 路径不符说明 `OFFICE_BUYER_LOG_DIR` 没生效 |
| `unmatched_log_rotation.max_bytes` | 你设置的值 | 不符说明 `LOG_MAX_BYTES` 没生效 |
| `unmatched_log_rotation.slices` | 现存分片列表 | 只应有 1 个文件，出现 `.1 .2` 说明滚动已发生（正常） |

> ⚠️ **`version` 一致不等于代码是最新的**：`app_version` 只在 `config.py` 里手写，
> 加功能时常常忘记 bump。真实案例——线上 `/` 的 `endpoints` 列表里没有
> `/api/v1/consumable/llm-update`，说明它跑的是加 LLM 之前的那次构建，
> 但 `/health` 的 `version` 同样是 `1.1.0`。**要判断部署是否为最新代码，
> 看 `/` 返回的 `endpoints` 列表（或比对 `/openapi.json` 的路径集合），不要只看版本号。**

```bash
# 2) 端到端：映射命中（应 matched=true，并带出耗材查询）
curl -sS "http://127.0.0.1:11443/api/v1/search?keyword=%E6%83%A0%E6%99%AE%20M126a&source=0&page=1" | head -c 400

# 3) 未命中链路：应 matched=false，同时 logs/unmatched.log 新增一行
curl -sS "http://127.0.0.1:11443/api/v1/search?keyword=配置验证用未知型号999"
tail -n 1 logs/unmatched.log
```

`status.sh` 把这些信息整理成一行行摘要，日常运维更顺手：

```bash
./scripts/status.sh            # 进程 + 端口 + 健康检查 + 映射表条数 + 日志位置
./scripts/status.sh --json     # 只输出 /health 原始 JSON（喂给监控）
./scripts/status.sh --logs 50  # 顺带打印守护日志最后 50 行
```

退出码：`0` = 运行中且健康；`1` = 未运行或健康检查失败。可直接做监控探针。

---

## 6. 客户端（技能端）如何指向本服务

技能端 `skill/scripts/main.py` 是一个极轻量的 HTTP 转发器，**只认一个配置**：

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `OFFICE_BUYER_SERVER_URL` | `https://api.ninenode.com/api/office-buyer` | 云端服务根地址（**不含** `/api/v1`，技能端会自己拼） |

```bash
# 本机自测：指向本地服务
export OFFICE_BUYER_SERVER_URL='http://localhost:11443'     # bash
$env:OFFICE_BUYER_SERVER_URL='http://localhost:11443'       # PowerShell

# 验证
uv run python ../skill/scripts/main.py search --keyword="惠普 M126a"
```

**双方必须对齐的两件事：**

1. **地址拼接规则**：技能端请求 `<SERVER_URL>/api/v1/search`。
   所以 `SERVER_URL` 填到「路由前缀的上一层」，例如生产是
   `https://api.ninenode.com/api/office-buyer`（Nginx 剥掉这段前缀后再转发）。
   填成 `https://api.ninenode.com/api/office-buyer/api/v1` 就会 404。
2. **超时**：技能端固定 60 秒（`TIMEOUT_SECONDS`）。服务端的上游超时与重试要留出余量，
   见 [2.5 节](#25-上游容错与限流)的坑 2。

---

## 7. 常见配置错误与排查

| 现象 | 最可能的原因 | 处理 |
| :--- | :--- | :--- |
| `start.sh` 报「端口 X 已被占用，但没有本脚本的 PID 文件」 | 服务由 Docker / 手工命令启动，或上次被 `kill -9` 留下残留 | `ss -lntp \| grep 11443` 看占用者；确认不是别的服务后手工结束，或 `PORT=<其他端口> ./scripts/start.sh` |
| `start.sh` 报「已有实例在运行」但你确信没有 | PID 文件残留（机器重启后 PID 被复用/进程已消失） | 脚本会自动清理失效 PID；仍报错时看 `run/office-buyer.pid` 里的 PID 是否真在跑，`status.sh` 会说明 |
| `kill` 掉进程后服务又自己起来了 | 看门狗按设计重启 | 用 `./scripts/stop.sh` 停止 |
| 服务起来但 `/health` 一直失败 | 端口没绑上（被占/权限不足 <1024）、依赖没装、配置写错 | `./scripts/status.sh --logs 50` 看守护日志，通常直接写着原因 |
| 映射表条数全是 0 | `OFFICE_BUYER_DATA_DIR` 指错 | `curl /health` 看实际路径，与 `ls app/data` 比对 |
| 未命中日志不落盘 | `OFFICE_BUYER_LOG_DIR` 目录不可写；磁盘满 | 日志里会有 `写入未命中日志失败` 告警。**落盘失败不影响接口返回**，所以只看接口是发现不了的 |
| 日志里客户端 IP 全是网关地址 | 没开 `TRUST_FORWARDED_FOR`，或 uvicorn 少了 `--proxy-headers` | 两者都要有：环境变量 `=1` + 启动命令带 `--proxy-headers --forwarded-allow-ips='*'` |
| 日志里客户端 IP 明显是伪造的 | 服务直接暴露公网却开着 `TRUST_FORWARDED_FOR=1` | 设为 `0`，或前面挂 Nginx 且只放行内网来源 |
| 上游返回 502 `UPSTREAM_ERROR` | 上游超时/限流/结构变化 | 看响应里的 `upstream_status` 与 `detail`；调大 `UPSTREAM_TIMEOUT`/`RETRIES`，或降低 `WORKERS` 减小上游压力 |
| 客户端报超时，但服务端日志正常 | 客户端 60s 超时 < 服务端最坏耗时 | 调小 `UPSTREAM_TIMEOUT` / `UPSTREAM_RETRIES`，或调小 `MAX_RELATED_QUERIES` 减少单次请求的上游次数 |
| 某个渠道总是没数据 | 上游 `sourceType=0` 实测只覆盖淘宝天猫/京东/拼多多 | 这是上游行为，不是配置问题。苏宁/唯品会/抖音/快手要显式传 `source=4/5/7/8`；覆盖范围请读响应的 `channels_covered` |
| Docker 里 LLM 补录的映射表重启就没了 | `app/data` 没挂载 | 加 `-v $(pwd)/app/data:/app/app/data` |
| `/docs` 页面能开但链接全 404 | Nginx 剥了前缀而 uvicorn 不知道 | 给 uvicorn 加 `--root-path /api/office-buyer` |
| `403` / `404` 出现在 `/api/office-buyer/...` | Nginx `proxy_pass` 少了末尾的 `/`（前缀没被剥掉） | `proxy_pass http://127.0.0.1:11443/;` 注意结尾的斜杠 |

**排查通用三步：**

```bash
# 1) 服务自身是否健康
./scripts/status.sh
curl -sS http://127.0.0.1:11443/health | python -m json.tool

# 2) 进程与端口（绕过脚本直查）
ps -ef | grep -E 'app.main:app|start.sh' | grep -v grep
ss -lntp | grep 11443            # 或 netstat -tlnp | grep 11443

# 3) 守护日志（看门狗 + uvicorn 的全部输出都在这里）
tail -n 100 run/daemon.log
```

改配置时的一条经验：**一次只改一个变量，改完立刻 `/health` 或跑一次 `search` 验证**。
`/health` 会把映射表条数和日志滚动参数回显出来，是确认配置是否真正生效的最快手段。

---

## 8. 附录：可直接抄的完整配置样例

### 8.1 生产：Linux 云主机 + Nginx + systemd

```bash
# ---- 目录布局 ----
# /opt/office-buyer/server/          代码（scripts/start.sh 自动 uv sync）
# /var/log/office-buyer/             未命中日志（独立分区）
# /etc/office-buyer.env              LLM 等敏感变量，chmod 600

# ---- /var/log/office-buyer 的权限 ----
sudo install -d -o office-buyer -g office-buyer /var/log/office-buyer
```

`office-buyer.conf`：

```bash
HOST=0.0.0.0
PORT=11443
WORKERS=2
LOG_LEVEL=info
LOG_DIR=/var/log/office-buyer
TRUST_PROXY=1
RESTART_DELAY=3
START_TIMEOUT=30
STOP_TIMEOUT=20
OFFICE_BUYER_RUNTIME_DIR=/var/run/office-buyer
```

`/etc/office-buyer.env`（systemd 的 `EnvironmentFile`）：

```bash
OFFICE_BUYER_TRUST_FORWARDED_FOR=1
OFFICE_BUYER_LOG_DIR=/var/log/office-buyer
OFFICE_BUYER_LOG_MAX_BYTES=5242880
OFFICE_BUYER_LOG_BACKUP_COUNT=5
OFFICE_BUYER_MAX_RELATED_QUERIES=2
OFFICE_BUYER_MAX_ITEMS_PER_QUERY=40
OFFICE_BUYER_UPSTREAM_TIMEOUT=15
OFFICE_BUYER_UPSTREAM_RETRIES=3
OFFICE_BUYER_UPSTREAM_CONCURRENCY=5
OFFICE_BUYER_UPSTREAM_MIN_INTERVAL=0.15
OFFICE_BUYER_LLM_ENABLED=0
#OFFICE_BUYER_LLM_API_KEY=sk-xxxxxxxx
#OFFICE_BUYER_LLM_BASE_URL=https://api.openai.com/v1
#OFFICE_BUYER_LLM_MODEL=gpt-4o
```

### 8.2 最小可用配置（只改端口）

```bash
cd server
uv sync --frozen --no-dev
PORT=9000 ./scripts/start.sh
./scripts/status.sh          # 不带 PORT 也能找到 9000 端口的那个实例
```

### 8.3 高并发场景配置

前提：已跟上游确认过配额，或接受风控风险。

```bash
WORKERS=4
LOG_LEVEL=warning                       # 降低日志量
# 上游压力 = WORKERS / MIN_INTERVAL，worker 翻倍时把它同步调大
OFFICE_BUYER_UPSTREAM_MIN_INTERVAL=0.3
OFFICE_BUYER_UPSTREAM_CONCURRENCY=8
OFFICE_BUYER_UPSTREAM_TIMEOUT=12
OFFICE_BUYER_UPSTREAM_RETRIES=2         # 高并发下减少重试放大
OFFICE_BUYER_MAX_RELATED_QUERIES=1      # 减少单次请求的上游次数
OFFICE_BUYER_MAX_ITEMS_PER_QUERY=40     # 不要低于 40，见 2.6
```

### 8.4 相关文件索引

| 文件 | 作用 |
| :--- | :--- |
| `app/config.py` | **唯一**的业务配置定义处（环境变量名与默认值都在这里） |
| `scripts/_common.sh` | 脚本层配置的装载与优先级实现 |
| `office-buyer.conf.example` | 脚本配置模板（`cp` 成 `office-buyer.conf` 后生效） |
| `Dockerfile` | 容器内的环境变量与启动命令 |
| `pyproject.toml` / `uv.lock` / `.python-version` | 依赖与解释器版本（解释器固定 3.11） |
| `README.md` | 接口说明、响应结构、映射表维护、功能验证 |
