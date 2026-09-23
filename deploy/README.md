# 部署到服务器（云端转写 + OSS 长期归档）

服务器负责下载、OSS 归档、云端转写和 DeepSeek 成文；不在 2 vCPU 主机上安装本地 Whisper。

工作台域名为 `podcast.squareconf.cn`。将该域名的 A 记录指向服务器 IP。Flask 负责登录、会话与账号权限，Caddy 只提供 HTTPS 和反向代理。

```
手机 / 电脑浏览器 ──HTTPS──▶ Caddy ──▶ Flask（登录、账号权限、云端 ASR）──▶ /srv/podcast-article/data/ + OSS
```

实际部署目标：阿里云 ECS Ubuntu 24.04，2 vCPU / 约 1.7G / 40G，工作台域名 `podcast.squareconf.cn`。

## 为什么使用云端 ASR

- 2 vCPU 主机不适合运行本地 Whisper，服务器改用阿里云百炼 `paraformer-v2`。
- 音频上传到私有 OSS 并长期保留，转写完成后不删除对象。
- ASR、OSS、DeepSeek 凭据只放 `/etc/podcast-article/server.env`，权限为 `0600`。
- 工作台采用邀请注册和 Flask 账号登录；服务端按账号隔离数据并执行用量限制。

## 服务器上的最终形态（宿主安装，不用 Docker）

```
/srv/podcast-article/                 代码（root 属主 + 全局可读，服务用户改不了）
  .venv/                              Python 3.11 虚拟环境
  deploy/                             Dockerfile / compose / Caddyfile（容器方案备用）
  data/                               持久数据根目录（podcast 用户可写）
    platform.sqlite                  账号、会话、队列与用量
    users/<账号 UUID>/                各账号独立的数据工作区
/usr/local/bin/caddy                  Caddy 静态二进制（v2.11.4）
/etc/caddy/Caddyfile                  站点配置（HTTPS + 反向代理）
/etc/systemd/system/podcast-article.service
/etc/systemd/system/caddy.service
```

两个服务都 `systemctl enable --now`，开机自启。

## 首次部署的实际步骤

```bash
# ---------- 在 Mac 上 ----------
# 0) 专用部署密钥（不动其他项目的密钥）
ssh-keygen -t ed25519 -N "" -f ~/.ssh/podcast_server -C podcast-article-deploy
# 把 ~/.ssh/podcast_server.pub 贴到服务器 /root/.ssh/authorized_keys

# 1) 传代码（排除个人数据与密钥；注意 macOS 自带 rsync 没有 --chown）
rsync -az --delete \
  --exclude .git --exclude .venv --exclude output --exclude node_modules \
  --exclude __pycache__ --exclude .env --exclude settings.json \
  --exclude library.json --exclude queue.json --exclude feeds.json \
  --exclude mcp_servers.json --exclude data \
  -e "ssh -i ~/.ssh/podcast_server" ./ root@SERVER:/srv/podcast-article/

# 2) 如果要导入旧版个人数据，先完成服务安装和账号初始化，再执行下方“旧版数据迁移”。

# 3) Caddy：服务器直连 GitHub 只有 25KB/s（17MB 要 11 分钟），所以在 Mac 上下好再传
curl -sL -o /tmp/caddy-linux.tar.gz \
  https://github.com/caddyserver/caddy/releases/download/v2.11.4/caddy_2.11.4_linux_amd64.tar.gz
scp -i ~/.ssh/podcast_server /tmp/caddy-linux.tar.gz root@SERVER:/tmp/

# ---------- 在服务器上 ----------
cd /tmp && tar xzf caddy-linux.tar.gz caddy && install -m 0755 caddy /usr/local/bin/caddy

# 4) Python 依赖（走阿里云 PyPI 镜像，PyPI 直连很慢）
python3 -m venv /srv/podcast-article/.venv
/srv/podcast-article/.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ /srv/podcast-article/

# 5) 权限：rsync 保留了 macOS 上 600 的权限，服务用户会读不到（实测踩过）
chown -R root:root /srv/podcast-article && chmod -R u+rwX,go+rX /srv/podcast-article
chown -R podcast:podcast /srv/podcast-article/data

# 6) Caddy 站点配置（不要配 email，填 example.com 会被 Let's Encrypt 拒）
cp /srv/podcast-article/deploy/Caddyfile /etc/caddy/Caddyfile

# 7) systemd（见下），然后
systemctl enable --now podcast-article caddy
```

`podcast-article.service` 的要点：`User=podcast`、`PA_READONLY=0`、`PA_ASR_BACKEND=cloud`、`PA_SCHEDULER=0`、
`PA_DATA_ROOT=/srv/podcast-article/data`、`PA_COOKIE_SECURE=1`、`PA_COOKIE_DOMAIN=podcast.squareconf.cn`、
`PA_TRUSTED_PROXY=1`、`ProtectSystem=full` + `ReadWritePaths=/srv/podcast-article/data`。各账号工作区由该根目录派生。
可信代理设置用于让登录限流从 `X-Real-IP` 读取访客地址；仅在请求始终经过 Portfolio Hub 边缘 Caddy 时启用，并确保代理传递真实远端地址。
公开访问后由 Flask 登录页处理账号身份；管理员先执行 `podcast-admin bootstrap`，再通过 `podcast-admin invite create` 为朋友生成一次性邀请链接。不要在 Caddy 配置或浏览器存储中放置共享密码。

在 `/etc/podcast-article/server.env` 配置 `DASHSCOPE_API_KEY`、`OSS_ACCESS_KEY_ID`、
`OSS_ACCESS_KEY_SECRET`、`OSS_BUCKET`、`OSS_ENDPOINT`、`OSS_PREFIX` 和 `DEEPSEEK_API_KEY`。
ASR 提交用的 OSS 签名 URL 默认有效 24 小时（`PA_ASR_URL_EXPIRES=86400`），播放接口仍使用短时签名。
不要把此文件上传到仓库。

首次初始化账号和用户密钥加密：

```bash
install -d -m 0700 /etc/podcast-article
touch /etc/podcast-article/server.env
chmod 0600 /etc/podcast-article/server.env
cd /srv/podcast-article
PA_DATA_ROOT=/srv/podcast-article/data \
  .venv/bin/podcast-admin secrets-key --env-file /etc/podcast-article/server.env
sudo -u podcast env PA_DATA_ROOT=/srv/podcast-article/data \
  .venv/bin/podcast-admin bootstrap --username kevin
```

`secrets-key` 由 root 执行，只把新密钥写入权限为 `0600` 的环境文件，不会把密钥打印到终端。确保数据目录归 `podcast` 用户后，由该服务用户创建管理员账号。核心服务商密钥也只保存在该文件；不要将这些变量放进 GitHub Actions。

### 旧版数据迁移

先对旧数据目录和项目目录做独立备份。迁移先运行只读清单：

```bash
sudo -u podcast env PA_DATA_ROOT=/srv/podcast-article/data \
  .venv/bin/podcast-admin migrate-legacy \
  --source-root /srv/podcast-article/legacy-data \
  --legacy-project-root /srv/podcast-article/legacy-project \
  --owner kevin --dry-run
```

核对集数、文件数和来源路径后，再将 `--dry-run` 改为 `--apply`。命令会把文章、分类、队列、订阅、设置和知识库导入 Kevin 的账号工作区，并在 `platform.sqlite` 记录完成状态；相同数据可安全重跑，目标文件内容不同会拒绝覆盖。旧文件保持原样，OSS 音频对象不复制、不删除也不改写；服务商密钥不会进入账号工作区。

## 日常更新

```bash
# 代码 + 数据一起推，然后重启应用（脚本见 scripts/deploy-server.sh）
bash scripts/deploy-server.sh             # 默认 root@120.27.128.11
```

只改前端也要重启（前端是静态文件，但改的是仓库里的副本）：

```bash
ssh root@SERVER 'systemctl restart podcast-article'
```

## 音频保留策略

OSS 音频长期保留。服务器本地音频只是上传和失败重试缓存；第一版不自动清理本地缓存，也不自动删除 OSS 对象。

```bash
cd /srv/podcast-article/data/users/<Kevin 的账号 UUID>/output
ls -1dt */ | tail -n +21 | while read d; do rm -f "$d"/audio.*; done   # 可选：只清本地缓存，OSS 不受影响
```

建议进 crontab 每周跑一次。删掉音频后文章照常阅读，只是时间戳点不开播放。

## GitHub Actions 自动部署

`.github/workflows/deploy.yml` 会在这个仓库的 `main` 推送后，通过 SSH 同步代码、更新虚拟环境并重启 `podcast-article.service`。在 GitHub 仓库设置：

- `PODCAST_SERVER_HOST`：ECS 公网 IP
- `PODCAST_SERVER_USER`：通常为 `root`
- `PODCAST_SERVER_SSH_PRIVATE_KEY_B64`：部署私钥的 Base64 内容

工作流不会同步 `data/`、本地配置或密钥文件，也不会覆盖服务器的 `/etc/podcast-article/server.env`。第一次部署前仍需在服务器准备 `python3.12-venv`、`podcast` 用户、systemd 服务和环境变量。

## 多账号发布前备份与回滚

这部分是批准发布后的操作清单；本地开发和验收阶段不执行。发布前先确认有足够备份空间，并把备份复制到独立的安全位置。`server.env` 含服务商密钥和用户密钥加密密钥，只能在服务器上以 `0600` 保存，不要放进 GitHub Actions 或发到聊天中。

在服务器 root shell 中设置 `PORTFOLIO_DEPLOY_PATH` 为 Portfolio Hub Actions 当前使用的 `DEPLOY_PATH`，然后备份数据、代码、密钥文件、正在运行的 Portfolio Hub 边缘 Caddy 配置、Compose 配置和当前镜像 tag。当前 Flask 直接绑定 Docker 网桥，边缘 Caddy 直连 Flask；宿主 `caddy` systemd 服务不在这条链路中。停止 Podcast Article 服务后再打包，确保 SQLite 数据完整：

```bash
set -euo pipefail
PORTFOLIO_DEPLOY_PATH=/替换为当前的Portfolio部署目录
BACKUP_ROOT="/srv/backups/podcast-article/$(date -u +%Y%m%dT%H%M%SZ)"
df -h /srv
install -d -m 0700 "$BACKUP_ROOT"

systemctl stop podcast-article
trap 'systemctl start podcast-article' EXIT
tar --acls --xattrs -cpf "$BACKUP_ROOT/data.tar" -C /srv/podcast-article data
tar --acls --xattrs \
  --exclude=.git --exclude=.venv --exclude=data --exclude=.env \
  --exclude=mcp_servers.json --exclude=output --exclude=library.json \
  --exclude=queue.json --exclude=feeds.json --exclude=settings.json \
  -cpf "$BACKUP_ROOT/code.tar" -C /srv/podcast-article .
install -m 0600 /etc/podcast-article/server.env "$BACKUP_ROOT/server.env"
cp -p "$PORTFOLIO_DEPLOY_PATH/Caddyfile" "$BACKUP_ROOT/portfolio-Caddyfile"
cp -p "$PORTFOLIO_DEPLOY_PATH/compose.yaml" "$BACKUP_ROOT/portfolio-compose.yaml"
cp -p "$PORTFOLIO_DEPLOY_PATH/.env" "$BACKUP_ROOT/portfolio.env"
grep '^APP_IMAGE=' "$PORTFOLIO_DEPLOY_PATH/.env" > "$BACKUP_ROOT/portfolio-image-tag.txt"
APP_IMAGE="$(sed -n 's/^APP_IMAGE=//p' "$PORTFOLIO_DEPLOY_PATH/.env")"
docker image save "$APP_IMAGE" | gzip -1 > "$BACKUP_ROOT/portfolio-image.tar.gz"
systemctl start podcast-article
trap - EXIT
```

核对 `portfolio-image-tag.txt` 和镜像归档已写入备份，再运行 `migrate-legacy --dry-run`。先检查清单和来源路径；仅在你确认后才运行 `--apply`。给朋友创建邀请前，按 DeepSeek 官方 USD 价目与 `usage.py` 中保守的 CNY 换算重新核对每月 LLM 上限；转写上限按秒设置，OSS 与上传上限也要按你愿意承担的额度填写。`podcast-admin invite create` 会要求五项上限都显式提供。

回滚时先设置 `BACKUP_ROOT` 为选定的备份目录、`PORTFOLIO_DEPLOY_PATH` 为 Actions 使用的部署目录，然后在服务器执行。旧数据先解压到临时目录，再用 `rsync --delete` 精确还原代码；排除项保护工作区、虚拟环境和本机密钥文件。失败版本的数据目录会改名留存，不会递归删除。

```bash
set -euo pipefail
RESTORE_CODE="$BACKUP_ROOT/restore-code"
mkdir -p "$RESTORE_CODE"
systemctl stop podcast-article
if [ -e /srv/podcast-article/data ]; then
  mv /srv/podcast-article/data "/srv/podcast-article/data.failed-$(date -u +%Y%m%dT%H%M%SZ)"
fi
tar --acls --xattrs -xpf "$BACKUP_ROOT/data.tar" -C /srv/podcast-article
tar --acls --xattrs -xpf "$BACKUP_ROOT/code.tar" -C "$RESTORE_CODE"
rsync -a --delete \
  --exclude=.venv/ --exclude=data/ --exclude=.env --exclude=mcp_servers.json \
  --exclude=output/ --exclude=library.json --exclude=queue.json \
  --exclude=feeds.json --exclude=settings.json \
  "$RESTORE_CODE/" /srv/podcast-article/
install -m 0600 "$BACKUP_ROOT/server.env" /etc/podcast-article/server.env
cp -p "$BACKUP_ROOT/portfolio-Caddyfile" "$PORTFOLIO_DEPLOY_PATH/Caddyfile"
cp -p "$BACKUP_ROOT/portfolio-compose.yaml" "$PORTFOLIO_DEPLOY_PATH/compose.yaml"
cp -p "$BACKUP_ROOT/portfolio.env" "$PORTFOLIO_DEPLOY_PATH/.env"
docker load --input "$BACKUP_ROOT/portfolio-image.tar.gz"
(cd "$PORTFOLIO_DEPLOY_PATH" && docker compose up -d --pull never --wait app && docker compose restart caddy)
systemctl start podcast-article
```

检查 `podcast-article` 的 systemd 状态、Portfolio Hub 的 `docker compose ps` 和全部公开路由。保留旧镜像 tag 直到新版本验收完成。回滚流程不需要、也不得删除 OSS 对象。

当前实现用 SSH 传送 Portfolio Hub 的压缩镜像归档并在服务器执行 `docker load`，不从服务器拉取 GHCR 应用镜像；这条传输路径尚未在生产服务器上测速或执行。

## 排障（这几条都是实测踩到的）

| 现象 | 原因与处理 |
| --- | --- |
| `certificate obtained successfully` 之后就通 | 443 必须是通的（Caddy 会用 TLS-ALPN-01）；80 只用于跳转 |
| Let's Encrypt 报 `invalidContact` | Caddyfile 里配了 `email`，且域名是 example.com 之类的保留域；删掉 email 即可 |
| 502 Bad Gateway | 上游地址不对：宿主安装服务监听 Docker 网桥 `172.17.0.1:8788`，容器方案才使用 `app:8787`（用 `{$PA_APP_UPSTREAM}` 占位） |
| 应用起不来、日志 `Permission denied` | rsync 过来的文件是 600；`chmod -R u+rwX,go+rX /srv/podcast-article` |
| `docker pull` 卡住 / timeout | 这台服务器的 Docker Hub 不通（`registry-1.docker.io` 超时），所以用宿主安装 |
| 页面能开但进度条不动 | 反代没关 SSE 缓冲：确认 `flush_interval -1` |
| 生成按钮点了报 401 | 登录会话已过期时应用会返回登录页；重新登录后继续 |
| 音频 404 | 音频还没同步，或被保留策略清理 |
