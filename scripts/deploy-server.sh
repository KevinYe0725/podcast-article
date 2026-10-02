#!/usr/bin/env bash
# 更新播客服务代码。账号数据与服务商密钥保留在服务器。
#
#   bash scripts/deploy-server.sh                    # 用默认主机
#   SERVER=root@1.2.3.4 bash scripts/deploy-server.sh
#
# 设计取舍：
#   · 服务器密钥只放 /etc/podcast-article/server.env，这里**不传** .env / settings.json / queue.json / mcp_servers.json
#   · 阅读状态、分类、文章、队列属于服务器上的各账号工作区，不从本机覆盖
#   · macOS 自带的 rsync 没有 --chown，属主在服务器上用一条 chown 修正
set -euo pipefail

SERVER="${SERVER:-root@116.62.168.32}"
KEY="${KEY:-$HOME/.ssh/kevinye-portfolio-aliyun}"
ROOT=/srv/podcast-article
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SSH=(ssh -i "$KEY" -o BatchMode=yes)
RSYNC_SSH="ssh -i $KEY -o BatchMode=yes"

cd "$HERE"

echo "▸ 1/3 推代码 → $SERVER:$ROOT/"
rsync -az --delete \
  --exclude .git --exclude .venv --exclude output --exclude node_modules \
  --exclude __pycache__ --exclude .env --exclude settings.json \
  --exclude library.json --exclude queue.json --exclude feeds.json \
  --exclude mcp_servers.json --exclude data \
  -e "$RSYNC_SSH" ./ "$SERVER:$ROOT/"

echo "▸ 2/3 更新服务配置与代码权限"
scp -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes deploy/podcast-article.service "$SERVER:/etc/systemd/system/podcast-article.service"
"${SSH[@]}" "$SERVER" "find '$ROOT' -path '$ROOT/data' -prune -o -path '$ROOT/.venv' -prune -o -exec chown root:root {} +; find '$ROOT' -path '$ROOT/data' -prune -o -path '$ROOT/.venv' -prune -o -exec chmod u+rwX,go+rX {} +; chown -R podcast:podcast '$ROOT/data'"

echo "▸ 3/3 重启应用并等待就绪"
"${SSH[@]}" "$SERVER" 'systemctl daemon-reload && systemctl restart podcast-article && systemctl is-active podcast-article'
"${SSH[@]}" "$SERVER" 'curl -fsS --retry 30 --retry-connrefused --retry-delay 1 --retry-max-time 35 --max-time 5 http://172.17.0.1:8788/api/auth/me >/dev/null'

echo
echo "✓ 播客服务已更新：https://podcast.squareconf.cn"
