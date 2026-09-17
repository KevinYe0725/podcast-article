#!/usr/bin/env bash
# 本地开发：重启 Web 服务（杀掉占着端口的旧进程，再用当前代码起一个新的）
#
#   bash scripts/dev.sh            # 默认 8787
#   bash scripts/dev.sh 8899       # 换端口
#   bash scripts/dev.sh --stop     # 只停，不起
#
# 为什么需要它：这个项目的后端（webapp.py）改了必须重启，前端（web/ 下的
# 静态文件）却是每次请求都从磁盘读 —— 于是很容易出现「代码是新的、跑着的
# 进程是旧的」：页面看起来正常，但新加的接口 404、时间戳点不动。
# 实测就踩过一次（跑了 4 小时前的进程，以为改的没生效）。
set -euo pipefail

PORT="${1:-8787}"
case "$PORT" in --stop) PORT="${2:-8787}"; STOP_ONLY=1;; *) STOP_ONLY=0;; esac
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="/tmp/pa-local-$PORT.log"
cd "$ROOT"

# ---------------------------------------------------------------- 停旧
if PIDS="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)" && [ -n "$PIDS" ]; then
  for pid in $PIDS; do
    cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    started="$(ps -o lstart= -p "$pid" 2>/dev/null | xargs || true)"
    case "$cmd" in
      *webapp.py*)
        echo "▸ 停掉旧进程 pid=$pid（启动于 $started）"
        kill "$pid" 2>/dev/null || true
        ;;
      *)
        echo "✗ 端口 $PORT 被别的进程占着，不是本项目的 webapp.py，我不动它：" >&2
        echo "    pid=$pid  $cmd" >&2
        exit 2
        ;;
    esac
  done
  for _ in $(seq 1 20); do
    lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.3
  done
  lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && { echo "✗ 端口 $PORT 还占着" >&2; exit 1; }
  echo "  ✓ 端口已释放"
else
  echo "▸ 端口 $PORT 空闲"
fi
[ "${STOP_ONLY:-0}" = 1 ] && exit 0

# ---------------------------------------------------------------- 起新
echo "▸ 用当前代码启动（日志：$LOG）"
nohup uv run python webapp.py --port "$PORT" >"$LOG" 2>&1 &
for i in $(seq 1 40); do
  if curl -sf -m 2 "http://127.0.0.1:$PORT/api/config" >/dev/null 2>&1; then
    echo "  ✓ 已就绪"
    break
  fi
  sleep 0.5
  if [ "$i" = 40 ]; then
    echo "✗ 20 秒还没起来，日志最后几行：" >&2
    tail -20 "$LOG" >&2
    exit 1
  fi
done

echo
echo "  打开：http://127.0.0.1:$PORT"
echo "  模式：$(curl -s "http://127.0.0.1:$PORT/api/config")"
echo "  看日志：tail -f $LOG    停止：bash scripts/dev.sh --stop"
