#!/usr/bin/env bash
#
# 办公设备易采买 —— 云端服务：停止后台服务
#
#   ./scripts/stop.sh           # 优雅停止：SIGTERM 后最多等 STOP_TIMEOUT 秒，仍在则自动 SIGKILL
#   ./scripts/stop.sh --force   # 跳过优雅等待，直接 SIGKILL
#
# 停止分三步，顺序不能反：
#   1. 落一个停止标记 run/office-buyer.stop —— 看门狗看到标记后不再拉起新进程；
#   2. 对守护循环及其整棵子进程树发 SIGTERM —— 让当前这轮 uvicorn 走 graceful shutdown；
#   3. 等到确认全部退出，超时再 SIGKILL。
# 少了第 1 步，看门狗会把你刚停掉的服务立刻拉回来；只杀守护循环而不杀子进程树，
# uvicorn 会被 init 收养、继续占着端口（Windows 上杀掉 bash 也不会带走 python.exe）。

set -euo pipefail

# shellcheck source=scripts/_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

FORCE=0
for arg in "$@"; do
    case "$arg" in
        -f|--force) FORCE=1 ;;
        -h|--help) print_usage "$0"; exit 0 ;;
        *) die "未知参数：$arg（可用：--force）" ;;
    esac
done

cleanup_stale_pid
# 端口/地址以启动时记下的为准：本次可能没带 PORT，而被停的那个实例用的是别的端口
load_state
pid="$(read_pid)"

# --------------------------------------------------------------------------- #
# 收集要终止的目标
#   1) 由 PID 文件反查它名下的 uvicorn（父子关系还在时最准）
#   2) 按「命令行含 app.main:app 且含本次端口」全量扫描（覆盖进程树已断的情形）
#   3) 守护循环本身
# --------------------------------------------------------------------------- #
KILL_TARGETS=""
if [ -n "$pid" ]; then
    for uvicorn_pid in $(find_uvicorn_children "$pid"); do
        add_target "$uvicorn_pid"
    done
    add_target "$pid"
fi
for uvicorn_pid in $(pids_matching_service); do
    add_target "$uvicorn_pid"
done

if [ -z "$pid" ]; then
    if [ -z "$KILL_TARGETS" ]; then
        log_warn "未找到 PID 文件（$PID_FILE），服务未通过本脚本启动。"
        if port_in_use; then
            log_warn "端口 $PORT 仍在监听，可能由 Docker 或手工命令启动，请自行确认后再处理。"
        fi
        exit 0
    fi
    # 没有 PID 文件却扫到了本服务的进程（上次 start 被中断、PID 文件被手工删掉等）
    log_warn "未找到 PID 文件，但发现本服务的残留进程：$KILL_TARGETS（端口 $PORT），一并结束。"
fi

# --------------------------------------------------------------------------- #
# 退出判定与升级手段
# --------------------------------------------------------------------------- #
# 目标列表里的进程全部消失，且端口真的不再监听。
# 只看 PID 是不够的——Windows 上 uvicorn 的 worker 是原生进程，可能已经脱离 MSYS
# 进程树，PID 全没了端口却还在，下一次 start 会因为端口被占而失败。
all_gone() {
    local target
    for target in $KILL_TARGETS; do
        is_alive "$target" && return 1
    done
    port_in_use && return 1
    return 0
}

# 先按 PID 树杀；MSYS 的 kill 带不走原生 python.exe 时，再按 Windows 进程树补一刀。
# Linux 上 is_windows_shell 为假，这一段完全不会执行，优雅停机的行为不受影响。
kill_all_forcefully() {
    local target win_pid
    if is_windows_shell; then
        # 顺序很关键：Windows PID 必须在发信号「之前」查、taskkill /T 也必须在父进程
        # 还活着时打。一旦 MSYS 的 kill 先把 uvicorn.exe 收走，ps -W 里查不到它，
        # taskkill /T 就无从下手，而它名下的原生 python.exe worker 会继续占着端口。
        local win_targets=""
        for target in $KILL_TARGETS; do
            win_pid="$(win_pid_of "$target")"
            [ -n "$win_pid" ] || continue
            win_targets="${win_targets:+$win_targets }$win_pid"
        done
        for win_pid in $win_targets; do
            taskkill //T //F //PID "$win_pid" >/dev/null 2>&1 || true
        done
    fi
    signal_targets KILL
    if is_windows_shell; then
        # 端口占用者仍活着时再补一次：进程树关系已断的残留 worker 只能这样收
        windows_kill_port_squatter
    fi
}

# 端口被 Python/uvicorn 镜像的进程占着就收掉（否则下次 start 必然因端口被占而失败）；
# 镜像名对不上（nginx、别的服务）只告警，不动手。
windows_kill_port_squatter() {
    is_windows_shell || return 0
    local win_pid image
    win_pid="$(windows_port_pid)"
    [ -n "$win_pid" ] || return 0
    image="$(windows_image_name "$win_pid")"
    if is_python_image "$image"; then
        taskkill //T //F //PID "$win_pid" >/dev/null 2>&1 || true
    else
        log_warn "端口 $PORT 被 $image（Windows PID $win_pid）占用，它不是本服务，未做处理。"
    fi
}

# 等目标全部退出；超时返回 1
wait_until_gone() {
    local timeout="$1" deadline
    deadline=$(( $(date +%s) + timeout ))
    while :; do
        all_gone && return 0
        [ "$(date +%s)" -ge "$deadline" ] && return 1
        sleep 0.2
    done
}

# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
# 标记必须先落盘：看门狗每轮循环开始前都会检查它
mkdir -p "$RUNTIME_DIR"
: > "$STOP_FILE"

log_info "正在停止服务${pid:+（守护循环 PID $pid）}，目标进程：$KILL_TARGETS"

if [ "$FORCE" = "1" ]; then
    log_warn "--force：跳过优雅等待，直接强制结束"
    kill_all_forcefully
    wait_until_gone 5 || true
else
    signal_targets TERM
    if ! wait_until_gone "$STOP_TIMEOUT"; then
        # 超时不再让用户手动再跑一次 --force：等满 STOP_TIMEOUT 还没退出，说明进程
        # 已经卡住（或平台不吃 SIGTERM），此时升级到 SIGKILL 是标准做法。
        log_warn "等待 ${STOP_TIMEOUT}s 后仍在运行，升级为强制结束"
        kill_all_forcefully
        wait_until_gone 5 || true
    fi
fi

if ! all_gone; then
    # 强制手段也无效时，把端口占用者是谁一并报出来，便于人工判断
    if is_windows_shell; then
        squatter="$(windows_port_pid)"
        if [ -n "$squatter" ]; then
            image="$(windows_image_name "$squatter")"
            log_error "端口 $PORT 仍被 $image（Windows PID $squatter）占用。"
        fi
    fi
    log_error "以下进程仍然存活，请人工排查：$KILL_TARGETS"
    log_error "提示：ps -ef | grep -E 'app.main:app|start.sh'；Windows 上用 tasklist /FI \"PID eq <WINPID>\""
    exit 1
fi

rm -f "$PID_FILE" "$STATE_FILE"
log_info "${C_OK}服务已停止${C_OFF}。"
log_info "说明：run/office-buyer.stop 保留在运行目录，下次 ./scripts/start.sh 会自动清除。"
