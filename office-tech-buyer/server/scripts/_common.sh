# shellcheck shell=bash
#
# 办公设备易采买 —— 云端服务：常驻脚本公共部分
#
# 由 start.sh / stop.sh / restart.sh / status.sh 通过 source 引入，不单独执行。
#
# 几个刻意的取舍：
#   1. PID 文件里记的是「守护循环」的 PID，uvicorn 是它的子进程。记循环而不是
#      uvicorn，是因为每次自动重启后 uvicorn 的 PID 都会变，而循环的 PID 在整个
#      运行期不变，stop/status 才有一个稳定的抓手。
#   2. 停止走两条路：先落一个停止标记文件（让循环不再拉起新进程），再发 SIGTERM
#      （让当前这轮 graceful shutdown）。只发 SIGTERM 的话，循环会立刻把服务重新
#      拉起来——这正是看门狗该有的行为，所以必须先用标记文件把它按住。
#   3. 配置优先级：命令行/环境变量 > server/office-buyer.conf > 脚本内置默认值。
#      端口默认值不写死在脚本里，直接从 app/config.py 读取，避免两处手改不同步。
#   4. 主用途是 Linux 云主机；顺带识别 Windows 下的 .venv/Scripts/，方便本机验证。

set -euo pipefail

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "$COMMON_DIR/.." && pwd)"

# 调用方是否显式指定了监听参数。要在读配置文件之前记下来，否则分不清
# 「PORT=9000 ./scripts/start.sh 传进来的」和「配置文件里写死的」。
ENV_PORT="${PORT:-}"
ENV_HOST="${HOST:-}"

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
#: 运行期文件（PID、停止标记、守护日志）。放 server/run/，已在 .gitignore 中忽略。
RUNTIME_DIR="${OFFICE_BUYER_RUNTIME_DIR:-$SERVER_DIR/run}"
PID_FILE="$RUNTIME_DIR/office-buyer.pid"
STOP_FILE="$RUNTIME_DIR/office-buyer.stop"
DAEMON_LOG="$RUNTIME_DIR/daemon.log"
#: start.sh 在这里写下「本次实际用的端口/地址」。stop.sh、status.sh 面对的是已经
#: 跑起来的那个实例，端口要以它为准——否则 `PORT=9000 ./scripts/start.sh` 起的服务，
#: 事后不带 PORT 跑 stop.sh 就会找不到它的 uvicorn 子进程。
STATE_FILE="$RUNTIME_DIR/office-buyer.state"
#: 可选配置文件，不入库（模板见 office-buyer.conf.example）
CONF_FILE="${OFFICE_BUYER_CONF:-$SERVER_DIR/office-buyer.conf}"

# --------------------------------------------------------------------------- #
# 配置装载
# --------------------------------------------------------------------------- #
# 先读配置文件，再补内置默认值。两边都用「变量已设置就不覆盖」的写法，
# 因此命令行上传进来的 PORT=9000 一定赢过配置文件，配置文件一定赢过默认值。
if [ -f "$CONF_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CONF_FILE"
fi

HOST="${HOST:-0.0.0.0}"
WORKERS="${WORKERS:-2}"
LOG_LEVEL="${LOG_LEVEL:-info}"
#: 上游真正的日志目录，服务启动前先建好，避免首启时目录不存在
LOG_DIR="${OFFICE_BUYER_LOG_DIR:-$SERVER_DIR/logs}"
#: 崩溃后重启前的等待秒数；短时间内连续崩溃会指数退避，最长 60 秒
RESTART_DELAY="${RESTART_DELAY:-3}"
#: start.sh 等待存活确认（健康检查通过）的秒数
START_TIMEOUT="${START_TIMEOUT:-30}"
#: stop.sh 等待进程退出的秒数，超时才 SIGKILL
STOP_TIMEOUT="${STOP_TIMEOUT:-20}"
#: 是否信任反代传来的 X-Forwarded-*（决定未命中日志里能不能记到真实客户端 IP）
TRUST_PROXY="${TRUST_PROXY:-1}"

# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
    C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi

log_info()  { printf '%s[start]%s %s\n' "$C_OK"   "$C_OFF" "$*"; }
log_warn()  { printf '%s[warn ]%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
log_error() { printf '%serror ]%s %s\n' "$C_ERR"  "$C_OFF" "$*" >&2; }
die()       { log_error "$@"; exit 1; }

# 打印某个脚本头部的注释块（跳过 shebang，遇到第一个非注释行就停），
# 供各脚本的 --help 复用：用法说明只写一份，不必在代码里再维护一遍。
print_usage() {
    awk 'NR == 1 { next }
         /^#/ { sub(/^# ?/, ""); print; next }
         { exit }' "$1"
}

# --------------------------------------------------------------------------- #
# 配置推导
# --------------------------------------------------------------------------- #
# 端口默认值以 app/config.py 为准，从源码里取一次，避免脚本和代码两处手改不同步。
_resolve_port() {
    if [ -n "${PORT:-}" ]; then
        printf '%s' "$PORT"
        return 0
    fi
    local from_code=""
    if [ -r "$SERVER_DIR/app/config.py" ]; then
        from_code="$(sed -n 's/.*_int("PORT", *\([0-9][0-9]*\)).*/\1/p' \
            "$SERVER_DIR/app/config.py" | head -n 1 || true)"
    fi
    printf '%s' "${from_code:-11443}"
}

# 服务可能绑在 0.0.0.0 上，本机探测一律走回环地址
probe_host() {
    if [ "$HOST" = "0.0.0.0" ] || [ "$HOST" = "::" ]; then
        printf '127.0.0.1'
    else
        printf '%s' "$HOST"
    fi
}

health_url() { printf 'http://%s:%s/health' "$(probe_host)" "$PORT"; }

# 端口在这里定稿：_resolve_port 只读 app/config.py，不碰环境变量，所以下面的
# 赋值顺序天然就是「命令行 > 配置文件 > 源码默认值」。
PORT="$(_resolve_port)"

# --------------------------------------------------------------------------- #
# 已运行实例的状态
#
# start.sh 在自己起的那次运行里不做这件事（它拿的就是当次参数），stop.sh /
# status.sh 则必须先 load_state：它们要操作的是「已经在跑的那个实例」，端口和地址
# 以启动时记下的为准。命令行显式传的 PORT/HOST 仍然优先，方便临时指向别的实例。
# --------------------------------------------------------------------------- #
load_state() {
    [ -f "$STATE_FILE" ] || return 0
    local key value
    while IFS='=' read -r key value; do
        case "$key" in
            PORT) [ -n "$ENV_PORT" ] && continue; PORT="$value" ;;
            HOST) [ -n "$ENV_HOST" ] && continue; HOST="$value" ;;
            *) ;;
        esac
    done < "$STATE_FILE"
}

save_state() {
    mkdir -p "$RUNTIME_DIR"
    {
        printf 'PORT=%s\n' "$PORT"
        printf 'HOST=%s\n' "$HOST"
        printf 'WORKERS=%s\n' "$WORKERS"
        printf 'SERVER_DIR=%s\n' "$SERVER_DIR"
    } > "$STATE_FILE"
}

# --------------------------------------------------------------------------- #
# 运行期解析：解释器与 uvicorn 入口
# --------------------------------------------------------------------------- #
# 解析结果写入 UVICORN_BIN / VENV_PYTHON（找不到时 UVICORN_BIN 为空串）
UVICORN_BIN=""
VENV_PYTHON=""

# shellcheck disable=SC2034  # LOG_DIR 供各脚本与守护模式使用
resolve_runtime() {
    local venv="${VENV_DIR:-$SERVER_DIR/.venv}"
    if [ -x "$venv/bin/uvicorn" ]; then
        UVICORN_BIN="$venv/bin/uvicorn"
        VENV_PYTHON="$venv/bin/python"
    elif [ -x "$venv/Scripts/uvicorn.exe" ]; then
        # Windows 下的 .venv 布局（仅为本机验证服务，生产走上面的分支）
        UVICORN_BIN="$venv/Scripts/uvicorn.exe"
        VENV_PYTHON="$venv/Scripts/python.exe"
    else
        UVICORN_BIN=""
        VENV_PYTHON=""
    fi
}

# 依赖未装时补一次 uv sync，与 Dockerfile 用的命令保持一致（--frozen 保证按 uv.lock 装）
ensure_runtime() {
    resolve_runtime
    [ -n "$UVICORN_BIN" ] && return 0
    command -v uv >/dev/null 2>&1 || die \
        "未找到 $SERVER_DIR/.venv 下的 uvicorn，且 PATH 里没有 uv。请先执行：cd '$SERVER_DIR' && uv sync --frozen --no-dev"
    log_info "依赖未安装，执行 uv sync --frozen --no-dev …"
    ( cd "$SERVER_DIR" && uv sync --frozen --no-dev ) || die "uv sync 失败"
    resolve_runtime
    [ -n "$UVICORN_BIN" ] || die "uv sync 完成仍未找到 uvicorn，请检查 $SERVER_DIR/.venv"
}

# --------------------------------------------------------------------------- #
# 进程状态
# --------------------------------------------------------------------------- #
read_pid() {
    if [ -f "$PID_FILE" ]; then
        tr -d ' \t\r\n' < "$PID_FILE"
    fi
}

is_alive() {
    local pid="${1:-}"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

# PID 文件残留（上次 kill -9、机器重启）会让 start 误判为「已在运行」，先清掉
cleanup_stale_pid() {
    local pid
    pid="$(read_pid)"
    if [ -n "$pid" ] && ! is_alive "$pid"; then
        log_warn "清理残留 PID 文件（进程 $pid 已不存在）：$PID_FILE"
        rm -f "$PID_FILE"
    fi
}

# 端口是否已被占用。bash 的 /dev/tcp 需要编译时开启；未开启时这里恒为「未占用」，
# 方向是安全的——最多漏掉一次提前告警，不会误拦正常启动。
port_in_use() {
    ( exec 3<>"/dev/tcp/$(probe_host)/$PORT" ) >/dev/null 2>&1
}

# --------------------------------------------------------------------------- #
# 进程树查询
#
# 只用 `ps -ef` 这一种最通用的输出（UID PID PPID ... CMD），不依赖 `ps -o`：
# Windows 下 Git Bash 自带的是精简版 ps，不认 -o 选项，本地验证时会全部落空。
# 代价是没有进程组信息，改由「递归抓子进程」（PPID 关系）替代，两个平台都对。
# --------------------------------------------------------------------------- #

# 直接子进程的 PID 列表
child_pids() {
    local parent="${1:-}"
    [ -n "$parent" ] || return 0
    ps -ef 2>/dev/null | awk -v p="$parent" '$3 == p { print $2 }'
}

# 进程的完整命令行（含参数）。
# 优先读 /proc/<pid>/cmdline——Linux 和 Git Bash(MSYS) 都有，参数完整且不受 ps 版本
# 影响；退回到 `ps -ef` 时按原始间距拼后半段（Git Bash 的精简 ps 会把参数吞掉，
# 所以这条只是兜底，用于容器里没有 /proc 的极端情形）。
proc_cmd() {
    local pid="${1:-}" line
    [ -n "$pid" ] || return 0
    if [ -r "/proc/$pid/cmdline" ]; then
        tr '\0' ' ' < "/proc/$pid/cmdline"
        return 0
    fi
    line="$(ps -ef 2>/dev/null | awk -v p="$pid" '$2 == p {
        out = "";
        for (i = 8; i <= NF; i++) out = out (i > 8 ? " " : "") $i;
        print out; exit
    }')"
    printf '%s' "$line"
}

# 递归收集进程 $1 及其全部子孙（先父后子）。用来整棵树一起发信号——只杀父进程的话，
# 孙进程会被 init 收养然后继续占着端口。
collect_tree() {
    local pid="$1" child
    printf '%s\n' "$pid"
    for child in $(child_pids "$pid"); do
        collect_tree "$child"
    done
}

# 对进程 $1 及其全部子孙发信号 $2
kill_tree() {
    local pid="$1" sig="$2" p
    for p in $(collect_tree "$pid"); do
        kill "-$sig" "$p" 2>/dev/null || true
    done
}

# 当前是否跑在 Windows 的 Git Bash / MSYS 下（用于补一刀 taskkill，见 stop.sh）
is_windows_shell() {
    case "$(uname -s 2>/dev/null || echo unknown)" in
        MINGW*|MSYS*|CYGWIN*) return 0 ;;
        *) return 1 ;;
    esac
}

# MSYS PID → Windows PID。Git Bash 里 kill 打的是「信号」，Windows 上的 python.exe
# 未必理会，需要 taskkill /T /F 才能连子孙一起收干净。
win_pid_of() {
    local pid="${1:-}"
    [ -n "$pid" ] || return 0
    ps -W 2>/dev/null | awk -v p="$pid" '$1 == p { print $4; exit }'
}

# 监听本服务端口的 Windows 原生进程 PID。
# Git Bash 下的 ps 看不到 uvicorn 用 multiprocessing 拉起的原生 python.exe worker，
# 它们在 Windows 上会脱离 MSYS 的进程树、继续占着端口——只能靠 netstat 反查回来。
# LISTENING 状态名不随系统语言本地化，因此这个匹配在中文 Windows 上同样成立。
windows_port_pid() {
    netstat -ano 2>/dev/null | awk -v p=":$PORT" '
        $0 ~ p && $4 == "LISTENING" { print $5; exit }'
}

# Windows 进程的镜像名（python3.11.exe 之类），用于确认「端口占用者确实是本服务」，
# 避免误杀同机器上恰好占用该端口的无关程序。
windows_image_name() {
    local win_pid="${1:-}"
    [ -n "$win_pid" ] || return 0
    tasklist //FI "PID eq $win_pid" //NH //FO CSV 2>/dev/null \
        | head -n 1 | sed 's/^"\([^"]*\)".*/\1/'
}

# 端口占用者的镜像名看起来像不像 Python 服务（python*.exe / uvicorn*.exe）
is_python_image() {
    case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in
        python*|uvicorn*) return 0 ;;
        *) return 1 ;;
    esac
}

# 按记录的 PID 反查它名下的 uvicorn 主进程。守护循环万一不在了（机器重启后残留
# PID 文件、进程被手工 kill），只剩这条路能把服务进程找回来，否则端口会一直被占。
#
# 两条都要命中才动手：父进程正是 PID 文件里记的那个，且命令行里带本次的端口。
# 避免误杀同一台机器上别的 uvicorn 进程。
find_uvicorn_children() {
    local parent="$1" child cmd
    [ -n "$parent" ] || return 0
    for child in $(child_pids "$parent"); do
        cmd="$(proc_cmd "$child")"
        case "$cmd" in
            *"app.main:app"*) ;;
            *) continue ;;
        esac
        case "$cmd" in
            *"--port $PORT"*|*"--port=$PORT"*) printf '%s\n' "$child" ;;
        esac
    done
}

# 把 PID 加进要终止的目标列表（空格分隔，自动去重）
KILL_TARGETS=""
add_target() {
    case " $KILL_TARGETS " in
        *" $1 "*) ;;
        *) KILL_TARGETS="${KILL_TARGETS:+$KILL_TARGETS }$1" ;;
    esac
}

# 对目标列表里的每个进程连同其子孙发信号
signal_targets() {
    local sig="$1" target
    for target in $KILL_TARGETS; do
        kill_tree "$target" "$sig"
    done
}

# 按命令行特征找本服务进程：命令行里同时出现 `app.main:app` 和本次端口。
# 这是唯一能覆盖「进程树已断」的手段——守护循环被 kill -9 之后，uvicorn 会被 init
# 收养，父子关系没了，find_uvicorn_children 就找不着它了，端口却还被占着。
#
# 有 /proc 时直接扫一遍（Linux、Git Bash 都有），比逐个进程 fork 一次 ps 快得多；
# 没有 /proc 才退回 ps 列表逐条查。
pids_matching_service() {
    local entry pid cmd
    if [ -d /proc ]; then
        for entry in /proc/[0-9]*/cmdline; do
            [ -r "$entry" ] || continue
            pid="${entry#/proc/}"
            pid="${pid%/cmdline}"
            [ "$pid" = "$$" ] && continue
            cmd="$(tr '\0' ' ' < "$entry" 2>/dev/null)"
            cmd_matches_service "$cmd" && printf '%s\n' "$pid"
        done
        return 0
    fi
    for pid in $(ps -ef 2>/dev/null | awk '$2 ~ /^[0-9]+$/ { print $2 }'); do
        [ "$pid" = "$$" ] && continue
        cmd_matches_service "$(proc_cmd "$pid")" && printf '%s\n' "$pid"
    done
}

# 命令行是否属于本服务（同时带 app.main:app 与本次端口）
cmd_matches_service() {
    case "$1" in
        *"app.main:app"*) ;;
        *) return 1 ;;
    esac
    case "$1" in
        *"--port $PORT"*|*"--port=$PORT"*) return 0 ;;
        *) return 1 ;;
    esac
}

# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
# 优先 curl，其次用 venv 里的解释器，两者都没有则返回 127（调用方自行降级）
http_get() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time "${HTTP_TIMEOUT:-5}" "$url"
    elif [ -n "$VENV_PYTHON" ] && [ -x "$VENV_PYTHON" ]; then
        "$VENV_PYTHON" - "$url" <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as resp:
        sys.stdout.write(resp.read().decode("utf-8", "replace"))
except Exception:
    sys.exit(1)
PY
    else
        return 127
    fi
}
