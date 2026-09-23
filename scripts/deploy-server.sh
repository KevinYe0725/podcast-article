#!/usr/bin/env bash
# 把代码与文章数据推到云端转写服务，然后重启应用。
#
#   bash scripts/deploy-server.sh                    # 用默认主机
#   SERVER=root@1.2.3.4 bash scripts/deploy-server.sh
#   WITH_AUDIO=1 bash scripts/deploy-server.sh       # 连音频一起推（每集约 40MB）
#
# 设计取舍：
#   · 服务器密钥只放 /etc/podcast-article/server.env，这里**不传** .env / settings.json / queue.json / mcp_servers.json
#   · 阅读状态与分类由 Mac 这边说了算（服务器上相关接口是 503），所以 library.json 单向覆盖
#   · macOS 自带的 rsync 没有 --chown，属主在服务器上用一条 chown 修正
set -euo pipefail

SERVER="${SERVER:-root@116.62.168.32}"
KEY="${KEY:-$HOME/.ssh/kevinye-portfolio-aliyun}"
ROOT=/srv/podcast-article
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SSH=(ssh -i "$KEY" -o BatchMode=yes)
RSYNC_SSH="ssh -i $KEY -o BatchMode=yes"

cd "$HERE"

echo "▸ 1/4 推代码 → $SERVER:$ROOT/"
rsync -az --delete \
  --exclude .git --exclude .venv --exclude output --exclude node_modules \
  --exclude __pycache__ --exclude .env --exclude settings.json \
  --exclude library.json --exclude queue.json --exclude feeds.json \
  --exclude mcp_servers.json --exclude data \
  -e "$RSYNC_SSH" ./ "$SERVER:$ROOT/"

echo "▸ 2/4 推文章数据（音频$( [ "${WITH_AUDIO:-0}" = "1" ] && echo "要" || echo "不要" )传）"
if [ "${WITH_AUDIO:-0}" = "1" ]; then
  rsync -az -e "$RSYNC_SSH" output/ "$SERVER:$ROOT/data/output/"
else
  rsync -az --exclude 'audio.*' -e "$RSYNC_SSH" output/ "$SERVER:$ROOT/data/output/"
fi
[ -f library.json ] && rsync -az -e "$RSYNC_SSH" library.json "$SERVER:$ROOT/data/library.json"

echo "▸ 3/4 修权限（rsync 会把 Mac 上的 600 带过去，服务用户读不到）"
"${SSH[@]}" "$SERVER" "chown -R root:root $ROOT && chmod -R u+rwX,go+rX $ROOT && chown -R podcast:podcast $ROOT/data"

echo "▸ 4/4 重启应用并自检"
"${SSH[@]}" "$SERVER" 'systemctl restart podcast-article && sleep 3 && systemctl is-active podcast-article'
"${SSH[@]}" "$SERVER" 'curl -s -m 5 http://127.0.0.1:8788/api/config'

echo
echo "✓ 完成。播客服务已更新，宿主监听 http://127.0.0.1:8788；公网入口需由 Portfolio Hub Caddy 配置 podcast.squareconf.cn。"
