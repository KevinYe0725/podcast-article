# 播客工作台邀请制多用户登录与数据隔离设计

**状态：** 已批准，按计划实施中
**日期：** 2026-09-23
**范围：** `podcast-article` Flask 服务与 Portfolio Hub Caddy 部署配置。本文是设计文档，不包含实现。

## 1. 已确认的目标与决策

Kevin 希望邀请朋友使用线上播客工作台。朋友需要各自登录，彼此不能查看或修改对方的文章、队列、订阅、设置、知识库、记忆或用量。转写和 AI 生成继续使用服务器上的 DashScope 与 DeepSeek 凭据，费用由 Kevin 的服务账户承担，并按账号设置额度。

已确认的产品选择：

- 注册仅通过一次性邀请，不开放公开注册。
- 每个账号拥有独立工作空间。
- Core ASR 与 LLM 使用服务器密钥；朋友不接触密钥，管理员为每个邀请设置月度额度。
- Kevin 的账号是管理员，当前的旧数据与旧 OSS 前缀归属 Kevin。
- 保留现有 OSS 对象，不因账号迁移或单集删除而清空 OSS。

设计假设，供本稿审阅：

- 账号使用用户名和密码，不要求邮箱；线上没有邮件发送配置。
- 忘记密码通过管理员命令重置，重置后撤销该账号的全部会话。
- 管理员账号通过一次性 CLI 引导创建；管理员密码由隐藏交互输入，不复用现有共享 Basic Auth 密码。
- 邀请创建时必须填写 ASR 月额度与 LLM 月度金额额度；没有默认无限额。管理员可配置为不受限。
- Notion 与 TTS 等个人集成密钥属于各自账号，并在服务器加密保存。MCP 命令配置只允许管理员使用，因为 MCP 配置能在服务器上启动进程。
- 本次只准备本地代码和验证。上线仍需独立确认。

## 2. 当前状态与已验证的问题

目前 `podcast.squareconf.cn` 由 Caddy 的单个 Basic Auth 保护。Flask 没有用户身份或授权层；多个浏览器使用同一凭据时，服务将其视为同一个用户。Caddy 配置实际位于 Portfolio Hub 仓库的 `Caddyfile`、`compose.yaml` 和 `.github/workflows/deploy.yml`；播客仓库的 `deploy/Caddyfile` 是另一份部署模板。登录切换必须同时修改两个仓库。

线上只读检查发现：

- 页面、静态 JS、文章库、分类、当前任务、设置读取、队列、订阅、全文搜索和用量接口返回 200。
- `/api/kb/status`、`/api/kb/search`、`/api/kb/entities` 和 `/api/memory` 返回 500，错误为 `OperationalError: unable to open database file`。
- Flask 服务以 `podcast` 用户运行，代码目录 `/srv/podcast-article` 是 `root:root`、权限 755；服务只有 `/srv/podcast-article/data` 可写。服务未设置 `PA_KB_FILE`，所以 `podcast_article/kb.py` 回退到 `PROJECT_ROOT/kb.sqlite`。
- 设置层把 `settings.json` 和 `.env` 放在 `PROJECT_ROOT`。代码目录不可写；服务器真实服务变量来自 `/etc/podcast-article/server.env`。因此设置写入路径和服务配置路径不一致。只读检查显示设置读取为 200；没有对线上发起设置写入请求。
- 服务器本地 `data/output` 当前没有单集目录，`data` 约 8 KB。OSS 内容没有清点；迁移必须保留现有对象并将旧前缀归给 Kevin。

当前测试结果：后端为 713 通过、2 失败、1 跳过；两个失败用例打桩了 `_chat`，却没有打桩 `summarize._client()`，所以本机缺少 `DEEPSEEK_API_KEY` 时测试失败。用测试专用假密钥重跑这两个用例均通过，没有调用外部模型。UI 套件全部通过，但 `tests/ui/run.sh` 漏传 `PA_KB_FILE`，测试启动时在仓库根目录生成了空的 `kb.sqlite`。该测试产物已清理；UI runner 需增加独立临时 KB 路径。

## 3. 用户与邀请

角色：

- `admin`：Kevin；可管理邀请、账号状态与额度，并查看服务级健康状态。
- `member`：被邀请的朋友；只能访问自己的工作空间和自己的用量。

账号生命周期：

1. `podcast-admin bootstrap` 交互式创建首个管理员 `kevin`。命令检查数据库不存在或明确处于初始化状态，密码只在终端隐藏输入，不作为命令行参数、日志或输出内容。
2. 管理员用 `podcast-admin invite create` 创建一次性邀请，并为该账号设置 ASR 月秒数额度与 LLM 月度 CNY 额度。邀请记录只保存令牌哈希、创建者、过期时间和使用时间。
3. 朋友访问邀请页，设置用户名与自己的密码。令牌只能消费一次；过期、撤销或重复消费均拒绝。
4. 用户可以登录、退出和修改自己的密码。管理员可以禁用/启用账号、撤销邀请和重置密码；被管理员重置后，用户下次登录必须设置新密码。
5. 禁用账号或重置密码时，立即撤销该账号所有会话，并阻止未开始的任务。

不提供公开注册、邮箱验证、邮件找回、SSO 或 MFA。忘记密码由管理员在服务器运行 CLI 重置；账号数据不会因此删除。

## 4. 身份验证与会话

密码使用 Argon2id 哈希存储，使用经过调优的安全参数；数据库泄露时不会暴露明文密码。OWASP 推荐新系统优先使用 Argon2id，并强调密码应使用自适应单向哈希保存。[Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)

会话采用服务端会话表：

- 登录成功后生成高熵随机不透明会话令牌；数据库只保存令牌哈希、账号 ID、创建时间、最近活动时间、空闲过期时间、绝对过期时间和撤销状态。
- 浏览器 Cookie 只包含令牌，设置 `Secure`、`HttpOnly`、`SameSite=Lax`，只作用于 `podcast.squareconf.cn` 主机。
- 登录、退出、改密、禁用账号和过期处理均能立即使服务端会话失效。服务端执行空闲与绝对超时。
- 登录失败使用统一提示；按账号与 IP 限速，连续失败返回 429。密码、会话令牌、邀请令牌和 API 密钥不得进入应用日志。
- 写请求使用 CSRF token；所有认证流量继续经 Caddy TLS。

OWASP 建议会话 ID 为不可预测的不透明值，使用 HTTPS 与安全 Cookie，并在服务端执行超时和撤销。[Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) 登录接口采用按账号和来源限制尝试的限速策略。[Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)

路由边界：

- 公开：登录页、邀请注册页、登录/注册/退出 API、静态登录资源，以及健康探测所需的最小端点。
- 其余页面和 `/api/*` 默认要求有效会话。未认证页面请求跳转到登录页；未认证 API 返回 JSON 401；无权限资源返回 404 或 403，不泄露其他账号资源是否存在。
- 管理 API 与管理 CLI 操作均检查 `admin` 角色。

## 5. 工作空间与数据隔离

服务器布局建议：

```text
/srv/podcast-article/data/
  platform.sqlite              # accounts、invites、sessions、quota ledger、jobs
  users/<account-id>/
    output/                     # 该账号的文章、稿件、封面和本地音频缓存
    library.json                # 分类、阅读状态
    settings.json               # 个人资料与生成偏好
    feeds.json                  # 个人订阅
    kb.sqlite                   # 知识库和记忆
    integrations.enc            # 该账号的 Notion/TTS 凭据密文
```

新增 `WorkspacePaths`（或等价的显式上下文），根据已认证账号构造上述路径，并传入存储层和后台线程。请求处理过程中不通过改写模块全局常量或进程环境变量切换账号，因为 Flask 请求、SSE 和后台线程会并发运行。

账号 ID 只从服务端会话读取。客户端传入的目录名、任务 ID 或 URL 先经过所属账号校验，再解析到该账号工作空间内。覆盖范围包括文章列表与删除、文件下载、音频流、封面、导出、搜索、知识库与记忆、设置、分类、队列、订阅、TTS、问答、发布与 SSE。

OSS 使用同一个私有 bucket 和服务器凭据。新对象按稳定账号 ID 放入独立前缀；创建签名 URL 前校验对象归属。现有 OSS 前缀保留为 Kevin 的命名空间，不批量移动或删除对象。用户删除文章仍沿用当前保留策略，不自动删除 OSS 对象。

这是应用层逻辑隔离，所有请求仍由同一个 `podcast` 服务进程处理。每条数据访问都必须从会话推导账号并执行归属检查；不能把“路径难以猜到”当成授权边界。

## 6. 任务、费用和资源

服务继续限制一个全局 ASR/文章流水线同时运行，以保护 2 vCPU ECS。每个提交先进入共享任务表，记录 `owner_id`；用户的队列 API 只返回该账号任务。调度器逐账号轮转，从各账号队列中取下一项，避免一个人的长队列长期占满服务。任务状态、进度查询和 SSE 均验证任务所有者；用户看不到其他人的 URL、日志、标题或错误详情。

额度账本按月、按账号记录：

- ASR：累计已处理音频秒数。
- LLM：累计 CNY 用量，包含文章生成、阅读助手、书库问答及其他计费模型调用。
- 每个邀请必须明确设置 ASR 月额度、LLM 月度 CNY 额度、本地缓存空间与待处理任务数；管理员可显式设置不受限。
- 月度账期按 Asia/Shanghai 自然月切换；额度不触发自动删除。OSS 对象继续按既有保留规则保存。
- 开始计费步骤前原子预留额度；任务结束按实际返回用量结算并释放未使用部分。失败任务保留已经发生的供应商费用。不能获取实际 usage 时按预先设置的最大调用预算计费，或拒绝该调用，不能绕过限额。
- 达到额度后拒绝新的计费操作并返回可理解的 429；页面显示本月用量、限额和下次重置时间。
- 每账号本地缓存空间、待处理任务数和单次上传大小也设置上限；具体数值由管理员在邀请时配置，本文不替用户虚构额度。

核心 DashScope、DeepSeek 与 OSS 凭据继续只放 `/etc/podcast-article/server.env`。它们不返回给成员 API，也不能被成员的设置表单覆盖。用户个人 Notion/TTS 凭据按账号加密保存，接口仅返回“已配置”和打码状态。MCP 命令配置与调用均限管理员，成员不能提交或触发会在服务器上运行的任意命令。

## 7. Portfolio Hub 反向代理切换

当前 live Caddy 对 `podcast.squareconf.cn` 的 Basic Auth 定义在 Portfolio Hub 仓库：

- `Caddyfile` 含共享 `basic_auth`。
- `compose.yaml` 要求 `PODCAST_BASIC_AUTH_HASH` 环境变量。
- `.github/workflows/deploy.yml` 从 GitHub Secret 生成部署 `.env`。

Flask 的账号认证和所有路由保护完成后，Portfolio Hub 配置需移除 Caddy Basic Auth、删除不再使用的环境变量引用及 GitHub Secret，同时保留 HTTPS、反向代理、SSE flush 和请求大小限制。播客仓库 `deploy/Caddyfile` 同步更新为无共享认证的代理模板。

切换顺序：先部署并检查 Flask 认证边界，再部署移除 Basic Auth 的 Caddy 配置。切换前后都必须保证未登录请求无法访问任何用户数据；不能先移除 Basic Auth 再补 Flask 保护。

## 8. 现有数据和问题修复

- 将现有文件型数据（若部署时存在）映射到 Kevin 的工作空间。当前服务器本地 `output` 为空；OSS 对象保持原位并继续归 Kevin 读取。
- 将知识库数据库放到用户工作空间；替代当前默认 `PROJECT_ROOT/kb.sqlite` 路径。恢复 KB status/search/entities 和 memory 接口，并修复 UI 测试 runner 为 `PA_KB_FILE` 使用临时目录。
- 设置 API 从登录会话取得用户工作空间；服务级密钥状态只对管理员显示，不能从个人设置写入 `/etc/podcast-article/server.env`。
- 修复两个问答测试的 mock：同时替代模型客户端构造和模型请求，使测试不依赖环境密钥，也不可能误连真实服务。
- 保持现有队列、订阅、文章阅读、音频回听、导出、搜索、TTS 和问答 UI 行为，并为每个路由增加归属检查。

## 9. 验收标准

1. 无会话请求不能读取页面数据或任何受保护 API；登录后用户只能访问自己的空间。
2. 公开注册关闭；有效邀请只能使用一次；过期、撤销或重复令牌被拒绝。
3. 登录/退出、改密、管理员重置、账号禁用和会话过期按预期工作；密码和令牌不出现在日志或 API 响应。
4. 两个成员互相不能通过目录名、文章 ID、音频 URL、导出、任务 ID、SSE、问答、知识库、分类、队列、订阅或 OSS 签名 URL 读取对方数据。
5. ASR 与 LLM 额度在所有计费入口服务端强制执行，队列、并发请求和失败重试都不能超额扣费或绕过限额。
6. MCP 命令配置与调用只对管理员开放；服务器核心密钥不对成员暴露；用户个人集成密钥加密保存。
7. `/api/kb/status`、KB 搜索、实体和记忆接口返回成功；设置修改写入正确工作空间。
8. `uv run pytest tests/ -q`、`bash tests/ui/run.sh` 全部通过；UI 测试不在工作树创建 `kb.sqlite` 或其他真实数据文件。
9. 所有本地验证通过后，在单独获准的发布窗口备份服务器数据与配置，再做两仓库切换和线上路由验收。任何上线操作保留 Caddy 与应用回滚方案，不删除 OSS 对象。

## 10. 暂不包含

- 公开注册、邮件验证和邮件找回。
- SSO、MFA、团队共享空间和成员间共享文章。
- 多个并行 ASR 流水线、按次收款或自动退款。
- 成员自定义服务器端 MCP 命令。
- 自动清理或迁移现有 OSS 对象。

## 11. 设计依据

- OWASP Password Storage Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- OWASP Session Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- OWASP Authentication Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html
