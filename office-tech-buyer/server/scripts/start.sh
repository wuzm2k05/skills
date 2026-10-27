#!/usr/bin/env bash
#
# 办公设备易采买 —— 云端服务：后台常驻启动（带看门狗）
#
#   ./scripts/start.sh              # 后台启动，等健康检查通过后返回
#   ./scripts/start.sh --foreground # 前台运行看门狗（交给 systemd/docker 托管时用）
#   PORT=9000 ./scripts/start.sh    # 覆盖端口
#
# 进程结构：
#
#   start.sh（拉起后即退出）
#     └── 守护循环  ← PID 记在 run/office-buyer.pid，常驻
#           └── uvicorn ← 崩了由循环按退避策略重新拉起
#
# 停止请用 ./scripts/stop.sh，不要直接 kill uvicorn——看门狗会立刻把它拉回来。

set -euo pipefail

# shellcheck source=scripts/_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

FOREGROUND=0
for arg in "$@"; do
    case "$arg" in
        -f|--foreground) FOREGROUND=1 ;;
        -h|--help) print_usage "$0"; exit 0 ;;
        *) die "未知参数：$arg（可用：--foreground）" ;;
    esac
done

# --------------------------------------------------------------------------- #
# 看门狗主体
#
# 以 `bash start.sh --foreground` 的形式重新执行自己，所以这段既可以在后台被
# nohup 拉起，也可以直接前台跑。
# --------------------------------------------------------------------------- #
if [ "$FOREGROUND" = "1" ]; then
    # 已经有实例在跑时，前台模式同样要挡住：两个实例抢同一个端口，后来者只会反复
    # 崩溃重启，日志里刷满 "address already in use"。要跑第二个实例请显式换端口。
    # 这里刻意不读 state 文件——前台启动以「本次参数」为准，读 state 反而会把上一次
    # 用过的端口带进来。OFFICE_BUYER_NO_GUARD=1 可跳过（供脚本编排使用）。
    if [ -z "${OFFICE_BUYER_NO_GUARD:-}" ]; then
        running_pid="$(read_pid)"
        if is_alive "$running_pid"; then
            die "已有实例在运行（PID $running_pid），停止后再试：./scripts/stop.sh"
        fi
    fi
    ensure_runtime
    mkdir -p "$RUNTIME_DIR" "$LOG_DIR" "$SERVER_DIR/app/data"
    cd "$SERVER_DIR"

    export HOST PORT WORKERS LOG_LEVEL
    export OFFICE_BUYER_LOG_DIR="$LOG_DIR"
    export OFFICE_BUYER_TRUST_FORWARDED_FOR="$TRUST_PROXY"
    # 无缓冲输出：日志重定向到文件时，不设这个会攒在缓冲区里看不到实时进度
    export PYTHONUNBUFFERED=1
    export PYTHONDONTWRITEBYTECODE=1

    # 停止标记是「人工停机」的唯一凭据。循环每轮开始前清一次，避免上次 kill -9
    # 留下的标记让本次启动立刻自杀。
    rm -f "$STOP_FILE"

    # PID 文件在这里、也只在这里写：它的内容就是当前这个守护循环的 PID（Windows 下
    # 的 bash 没有 $$，用 $BASHPID 取真实子 shell PID）。stop/status 都以它为准。
    printf '%s\n' "$BASHPID" > "$PID_FILE"

    # 优雅停机的收尾：bash 的 wait 会被信号打断，重新 wait 一次拿到真实退出码，
    # 但此时 uvicorn 可能还没退干净（Linux 上 SIGTERM 后还会停留几秒），所以要轮询
    # 等它真正结束。否则 stop.sh 会误判成「循环死了但 uvicorn 还占着端口」。
    wait_child_exit() {
        local rc="$1" waited=0
        while [ -n "$child" ] && kill -0 "$child" 2>/dev/null; do
            [ "$waited" -ge "${CHILD_EXIT_TIMEOUT:-20}" ] && break
            sleep 0.2
            waited=$((waited + 1))
        done
        if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
            echo "[$(ts)] [daemon] 子进程 $child 未在 ${CHILD_EXIT_TIMEOUT:-20}s 内退出，补发 SIGKILL" >&2
            kill_tree "$child" KILL
        fi
        child=""
        return "$rc"
    }

    # 正常退出（收到 SIGTERM/SIGINT）时让当前这轮 uvicorn 优雅停机，然后退出循环
    child=""
    stopping=0
    on_stop() {
        stopping=1
        if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
            kill -TERM "$child" 2>/dev/null || true
        fi
    }
    trap on_stop TERM INT

    ts() { date '+%Y-%m-%d %H:%M:%S'; }

    fail_streak=0
    while :; do
        if [ -f "$STOP_FILE" ]; then
            echo "[$(ts)] [daemon] 检测到停止标记，退出守护循环" >&2
            break
        fi

        echo "[$(ts)] [daemon] 启动 uvicorn：$UVICORN_BIN app.main:app --host $HOST --port $PORT --workers $WORKERS" >&2
        "$UVICORN_BIN" app.main:app \
            --host "$HOST" --port "$PORT" --workers "$WORKERS" \
            --log-level "$LOG_LEVEL" --proxy-headers --forwarded-allow-ips='*' &
        child=$!

        set +e
        wait "$child"
        status=$?
        # 被信号打断时 wait 返回 128+信号，uvicorn 其实还在优雅停机中，这里等它退干净
        if [ "$stopping" = "1" ] || [ -f "$STOP_FILE" ]; then
            wait_child_exit "$status"
            status=$?
        fi
        set -e
        child=""

        if [ "$stopping" = "1" ] || [ -f "$STOP_FILE" ]; then
            echo "[$(ts)] [daemon] 收到停止信号，不再重启（uvicorn 退出码 $status）" >&2
            break
        fi

        # 退出码 0 是「uvicorn 自己正常收工」（例如收到外部 SIGTERM 后 graceful 退出），
        # 不是崩溃：立刻重启，不进指数退避，也不累加失败计数。
        if [ "$status" = "0" ]; then
            echo "[$(ts)] [daemon] uvicorn 正常退出，立即重启" >&2
            fail_streak=0
            continue
        fi

        # 指数退避：启动即崩（端口占用、配置写错）时不至于把 CPU 和日志刷爆；
        # 连续崩溃封顶在 60 秒，恢复后立刻回到 RESTART_DELAY。
        fail_streak=$((fail_streak + 1))
        sleep_for="$RESTART_DELAY"
        i=1
        while [ "$i" -lt "$fail_streak" ] && [ "$sleep_for" -lt 60 ]; do
            sleep_for=$((sleep_for * 2))
            i=$((i + 1))
        done
        [ "$sleep_for" -gt 60 ] && sleep_for=60

        echo "[$(ts)] [daemon] uvicorn 异常退出（退出码 $status），${sleep_for}s 后重启（连续失败 ${fail_streak} 次）" >&2
        sleep "$sleep_for"
    done

    echo "[$(ts)] [daemon] 守护循环结束" >&2
    rm -f "$PID_FILE" "$STATE_FILE"
    exit 0
fi

# --------------------------------------------------------------------------- #
# 后台启动分支
# --------------------------------------------------------------------------- #
cleanup_stale_pid
existing="$(read_pid)"
if is_alive "$existing"; then
    # 已有一个实例在跑时，把它的端口读回来，避免用另一套端口去提示、去探测
    load_state
    log_warn "服务已在运行（PID $existing，端口 $PORT），无需重复启动。"
    log_warn "如需重启请用：./scripts/restart.sh"
    exit 0
fi

if port_in_use; then
    die "端口 $PORT 已被占用，但没有本脚本的 PID 文件。请先腾出端口，或用 PORT=<其他端口> ./scripts/start.sh"
fi

ensure_runtime
mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

# 记下本次实际使用的参数，供 stop.sh / status.sh 找到这个实例。
# 放在依赖检查之后：依赖没装好时不该留下一个「有状态文件、没实例」的空壳。
save_state

# setsid 让守护循环脱离当前终端，关掉 SSH 会话也不受影响；没有 setsid（如 Git Bash）
# 时退回 nohup——两者都靠 start.sh 退出来脱离，配合重定向后的 stdio 足够稳。
launcher="nohup"
if command -v setsid >/dev/null 2>&1; then
    launcher="setsid"
fi

# </dev/null：不继承 stdin，避免守护进程被后台终端的输入干扰
"$launcher" bash "$0" --foreground >>"$DAEMON_LOG" 2>&1 </dev/null &
daemon_pid=$!

# 守护循环刚拉起时可能还没写 PID 文件，等它落盘
tries=0
while [ "$tries" -lt 50 ]; do
    is_alive "$(read_pid)" && break
    is_alive "$daemon_pid" || die "守护进程启动失败，请查看：$DAEMON_LOG"
    sleep 0.1
    tries=$((tries + 1))
done
[ -n "$(read_pid)" ] || die "守护进程未写出 PID 文件，请查看：$DAEMON_LOG"

log_info "守护循环已启动（PID $(read_pid)），等待服务就绪 …"
log_info "守护日志：$DAEMON_LOG"

# 健康检查通过才算启动成功：只看进程活着，无法区分「起来了」和「起来后端口没绑上」
deadline=$(( $(date +%s) + START_TIMEOUT ))
health_last_err=""
while :; do
    if ! is_alive "$(read_pid)"; then
        log_error "守护循环已退出，最后 20 行日志："
        tail -n 20 "$DAEMON_LOG" >&2 || true
        exit 1
    fi
    # 只捕获 stderr（curl 的错误说明），stdout 丢弃；|| 形式是为了在 set -e 下不中断
    health_rc=0
    health_last_err="$(http_get "$(health_url)" 2>&1 >/dev/null)" || health_rc=$?
    if [ "$health_rc" = "0" ]; then
        break
    fi
    # http_get 返回 127 表示 curl 与 venv 解释器都不可用，重试也不会有结果，直接说明
    if [ "$health_rc" = "127" ]; then
        log_warn "本机没有 curl，也找不到 $VENV_PYTHON，跳过健康检查。"
        log_warn "服务仍在后台运行（PID $(read_pid)），请从外部确认：$(health_url)"
        exit 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        log_error "启动超时（${START_TIMEOUT}s 内未通过健康检查）：$(health_url)"
        [ -n "$health_last_err" ] && log_error "最后错误：$health_last_err"
        log_error "最后 20 行日志："
        tail -n 20 "$DAEMON_LOG" >&2 || true
        log_warn "服务可能仍在启动中；确认状态：./scripts/status.sh"
        exit 1
    fi
    sleep 1
done

log_info "${C_OK}服务已就绪${C_OFF}：http://$(probe_host):$PORT/   （文档 /docs，健康检查 /health）"
log_info "查看状态：./scripts/status.sh    停止：./scripts/stop.sh"
