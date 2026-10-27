#!/usr/bin/env bash
#
# 办公设备易采买 —— 云端服务：重启（stop 后 start）
#
#   ./scripts/restart.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/stop.sh" "$@"
"$SCRIPT_DIR/start.sh"
