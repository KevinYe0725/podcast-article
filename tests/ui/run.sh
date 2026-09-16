#!/usr/bin/env bash
# UI 测试：起一个指向临时目录的服务，用 jsdom 跑真实交互测试，最后关掉服务。
set -uo pipefail
cd "$(dirname "$0")"

REPO="$(cd ../.. && pwd)"
PORT="${PA_TEST_PORT:-8791}"
TMP="$(mktemp -d)"
OUT="$TMP/output"
mkdir -p "$OUT"
export PA_OUTPUT_DIR="$OUT"
export PA_LIBRARY_FILE="$TMP/library.json"   # 分类数据也要隔离，别碰真实 library.json
export PA_QUEUE_FILE="$TMP/queue.json"       # 批量队列（后台调度会读写它）
export PA_FEEDS_FILE="$TMP/feeds.json"       # 订阅
export PA_SCHEDULER=0                        # 关掉后台自动跑队列，让测试可控（手动触发用 /api/queue/run）
export PA_BASE="http://127.0.0.1:$PORT"

# 端口必须空闲：否则会静默连到上一次残留的服务上，测出莫名其妙的结果
if curl -sf "$PA_BASE/" >/dev/null 2>&1; then
  echo "✕ 端口 $PORT 已被占用（可能是上次没退干净的服务），请先释放或换 PA_TEST_PORT"
  exit 2
fi

[ -d node_modules ] || npm install --silent --no-fund --no-audit

node seed.js

( cd "$REPO" && PA_OUTPUT_DIR="$OUT" PA_LIBRARY_FILE="$PA_LIBRARY_FILE" \
    PA_QUEUE_FILE="$PA_QUEUE_FILE" PA_FEEDS_FILE="$PA_FEEDS_FILE" PA_SCHEDULER=0 \
    uv run python webapp.py --port "$PORT" >/tmp/pa-ui-test-server.log 2>&1 ) &
SERVER_PID=$!

# 彻底回收：只 kill 子 shell 会留下 uv/python 子进程，CI 上会让后续的 uv 缓存清理失败
cleanup() {
  kill "$SERVER_PID" 2>/dev/null
  pkill -f "webapp.py --port $PORT" 2>/dev/null
  wait "$SERVER_PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

for i in $(seq 1 40); do
  curl -sf "$PA_BASE/" >/dev/null && break
  sleep 0.5
done
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  echo "✕ 服务启动失败，日志："; tail -5 /tmp/pa-ui-test-server.log; exit 2
fi

# 注：runview.test.js 需要真实下载/转写（依赖网络），不进 CI，需要时手动跑
# 每个测试都套 timeout：jsdom 里某个 promise 挂住时，宁可红掉也不要让 CI 卡死
# 调试时可以只跑其中一个：PA_UI_TESTS=assistant.test.js bash run.sh
DEFAULT_TESTS="close.test.js reader.test.js modal.test.js sidebar.test.js delete.test.js audio.test.js
search.test.js status.test.js queue.test.js feeds.test.js export.test.js nav.test.js
assistant.test.js"
FAILED=0
for t in ${PA_UI_TESTS:-$DEFAULT_TESTS}; do
  echo "── $t"
  if command -v timeout >/dev/null 2>&1; then
    timeout 120 node "$t" || FAILED=1
  else
    node "$t" || FAILED=1          # macOS 没有 timeout，run.sh 自身受 CI 超时保护
  fi
done
[ "$FAILED" = 0 ] && echo "✓ UI 测试全部通过" || echo "✕ 存在失败的 UI 测试"
exit "$FAILED"
