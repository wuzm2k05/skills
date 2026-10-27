#!/usr/bin/env bash
#
# 办公设备易采买 —— 云端服务：查看运行状态
#
#   ./scripts/status.sh            # 摘要：进程、端口、健康检查、日志位置
#   ./scripts/status.sh --json     # 只输出 /health 的原始 JSON（给脚本/监控用）
#   ./scripts/status.sh --logs 50  # 顺带打印守护日志最后 50 行
#
# 退出码：0 运行中且健康；1 未运行或健康检查失败。可直接用于监控探测。

set -euo pipefail

# shellcheck source=scripts/_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

JSON_ONLY=0
LOG_TAIL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --json) JSON_ONLY=1 ;;
        # 行数可省略；只给了 --logs 时不能再 shift，否则末尾那次 shift 会因参数耗尽而失败
        --logs) LOG_TAIL="${2:-50}"; [ $# -ge 2 ] && shift ;;
        -h|--help) print_usage "$0"; exit 0 ;;
        *) die "未知参数：$1（可用：--json / --logs [行数]）" ;;
    esac
    shift
done

resolve_runtime
cleanup_stale_pid
# 端口/地址以启动时记下的为准：查看状态时通常不带 PORT，而被查的实例可能用的是别的端口
load_state
pid="$(read_pid)"

# --------------------------------------------------------------------------- #
# 健康检查（进程退出后端口可能还在 TIME_WAIT，因此状态以「进程 + 接口」共同判定）
# --------------------------------------------------------------------------- #
health_body=""
health_ok=0
if health_body="$(http_get "$(health_url)" 2>/dev/null)"; then
    health_ok=1
fi

if [ "$JSON_ONLY" = "1" ]; then
    [ "$health_ok" = "1" ] || die "健康检查失败：$(health_url)"
    printf '%s\n' "$health_body"
    exit 0
fi

# --------------------------------------------------------------------------- #
# 摘要
# --------------------------------------------------------------------------- #
printf '服务      : office-tech-buyer-server\n'
printf '地址      : http://%s:%s/  （文档 /docs，健康检查 /health）\n' "$(probe_host)" "$PORT"
printf '工作目录  : %s\n' "$SERVER_DIR"

if [ -n "$pid" ] && is_alive "$pid"; then
    # 守护循环下面通常挂着一个 uvicorn，把它连同命令行列出来，便于确认 worker 数
    printf '进程      : 运行中（守护循环 PID %s）\n' "$pid"
    children="$(child_pids "$pid")"
    if [ -n "$children" ]; then
        printf '子进程    :\n'
        for child in $children; do
            printf '            %s  %s\n' "$child" "$(proc_cmd "$child")"
        done
    else
        printf '子进程    : （暂未拉起 uvicorn，可能正处于重启退避等待中）\n'
    fi
elif [ -n "$pid" ]; then
    printf '进程      : %s未运行%s（PID 文件记录 %s，进程不存在）\n' "$C_ERR" "$C_OFF" "$pid"
else
    printf '进程      : %s未运行%s（无 PID 文件，未通过本脚本启动）\n' "$C_WARN" "$C_OFF"
fi

if [ "$health_ok" = "1" ]; then
    printf '健康检查  : %s正常%s\n' "$C_OK" "$C_OFF"
    # 映射表条数与未命中日志滚动状态都在 /health 里，用解释器格式化输出；
    # 没有解释器时退回原样打印，信息不丢。
    if [ -n "$VENV_PYTHON" ] && [ -x "$VENV_PYTHON" ]; then
        printf '%s' "$health_body" | "$VENV_PYTHON" - <<'PY' || true
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print("版本      :", data.get("version", "-"))
m = data.get("mappings", {})
print("映射表    : 耗材 %s 条 / 服务器配件 %s 条 / 纸品文具 %s 条"
      % (m.get("consumables", "-"), m.get("server_hardware", "-"), m.get("paper_spec", "-")))
rot = data.get("unmatched_log_rotation", {})
print("未命中日志: %s（存在=%s，上限 %s 字节，保留 %s 个分片，现有分片 %d 个）"
      % (data.get("unmatched_log", "-"), data.get("unmatched_log_exists", "-"),
         rot.get("max_bytes", "-"), rot.get("backup_count", "-"), len(rot.get("slices", []))))
PY
    else
        printf '健康详情  : %s\n' "$health_body"
    fi
else
    printf '健康检查  : %s失败%s（%s）\n' "$C_ERR" "$C_OFF" "$(health_url)"
fi

printf '上游日志  : %s\n' "$LOG_DIR"
printf '守护日志  : %s\n' "$DAEMON_LOG"

if [ "$LOG_TAIL" != "0" ]; then
    printf '\n---- %s 最后 %s 行 ----\n' "$DAEMON_LOG" "$LOG_TAIL"
    if [ -f "$DAEMON_LOG" ]; then
        tail -n "$LOG_TAIL" "$DAEMON_LOG" || true
    else
        printf '（尚无守护日志）\n'
    fi
fi

[ "$health_ok" = "1" ] || exit 1
