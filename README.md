# podcast-article

给一个播客/视频链接，产出一篇「读完能记住」的深度文章。

解决一个真实痛点：好播客很多，但全量去听信息密度太低，听完就忘。本工具把 1-4 小时的音频压缩成结构化文章——核心论点、推理链条、金句（带时间戳可回跳）、书影音清单、编辑点评。

## 支持的输入

| 来源 | 形式 | 说明 |
|---|---|---|
| 小宇宙 | 单集分享链接 | 免登录抓取 |
| YouTube | 视频/播客链接 | 优先用平台字幕，免转写 |
| Bilibili | 视频链接 | 同上 |
| Apple Podcasts | 节目页链接 | 自动换取 RSS |
| RSS | feed 地址 | 默认取最新一集，`--pick N` 选第 N 新 |
| 本地文件 | mp3/m4a/mp4 路径 | 跳过下载 |

## 安装

```bash
uv sync          # 自动创建 .venv 并安装依赖（含 mlx-whisper）
brew install ffmpeg   # 如尚未安装
cp .env.example .env  # 填入 DEEPSEEK_API_KEY（https://platform.deepseek.com）
```

要求：Apple Silicon Mac（转写用 mlx-whisper 走 GPU）。非 Apple Silicon 或想用 CPU 通用后端：

```bash
uv sync --extra faster   # 安装 faster-whisper 后端
```

## 使用

```bash
uv run podcast-article "https://www.xiaoyuzhoufm.com/episode/xxxx"
```

### Web 界面

```bash
uv run python webapp.py     # 打开 http://127.0.0.1:8787
```

单页界面（OpenAI 风格，自托管 Inter 字体，零构建）：填链接 → 四阶段时间线**实时**推进（SSE 推送：下载百分比/速度、ASR 百分比与剩余时间、LLM 已生成字数）→ 文章阅读视图。支持一键写入 Notion、查看文字稿、重写文章；底部是历史文章库。

## MCP：接入 Claude Desktop 等客户端

```bash
uv run podcast-article-mcp     # stdio 传输
```

客户端配置（Claude Desktop / 其他支持 mcpServers 的客户端）：

```json
{
  "mcpServers": {
    "podcast-article": {
      "command": "uv",
      "args": ["--directory", "/Users/kevinye/Projects/podcast-analysis", "run", "podcast-article-mcp"]
    }
  }
}
```

暴露四个工具：`analyze_podcast`（跑完整流水线，返回文章全文）、`list_episodes`、`get_article`、`push_to_notion`。

## 写入 Notion

1. 在 [Notion Integrations](https://www.notion.so/profile/integrations) 创建 Internal Integration，复制 `Secret`，填入 `.env` 的 `NOTION_TOKEN`
2. 在 Notion 打开目标数据库或页面 → 右上角「…」→ 连接 → 添加该 integration
3. `.env` 配置目标位置（二选一，都填优先数据库）：
   - `NOTION_DATABASE_ID=…` → 每篇文章作为数据库一行
   - `NOTION_PARENT_PAGE_ID=…` → 作为某个页面的子页面

写入入口：Web 界面文章区的「✦ 写入 Notion」按钮，或 MCP 工具 `push_to_notion`。markdown 会转换为 Notion 原生块（标题/引用/列表/表格/行内样式），文末自动附原文链接。

一条命令跑完 **抓取 → 音频 → 文字稿（无字幕时本地转写）→ DeepSeek 精读成文**，产物在 `output/<日期-节目-标题>/`：

- `article.md` — 最终文章
- `transcript.txt` — 带时间戳的完整文字稿
- `transcript.json` — 结构化片段（程序可读）
- `audio.*` — 原始音频
- `meta.json` — 单集元信息

### 常用参数

```bash
--lang zh            # 指定转写语言（默认 auto 自动检测）
--no-subs            # 强制语音转写，不用平台字幕
--backend faster     # 指定转写后端（auto/mlx/faster）
--force-transcript   # 重新转写
--force-article      # 同一文字稿重新生成文章（换风格/换模型时用）
--refresh            # 全部重来
--pick 2             # RSS 输入时取第 2 新的单集
```

### 缓存机制

每一步的产物落盘后，重跑自动跳过已完成步骤：转写一次要几分钟到十几分钟，但**重新生成文章不用再转写**；中断后重跑自动续上。

## 文章结构

模型按固定骨架输出（`podcast_article/summarize.py` 里可调）：

1. **自拟标题 + 一句话总结** —— 30 秒抓住全貌
2. **内容速览** —— 3-6 条要点
3. **核心内容** —— 按逻辑组织的章节，含论点/论据/案例/数据与原文引用（带时间戳）
4. **金句摘录** —— 原话直引 + 时间戳
5. **提及的书影音/人物/概念** —— 表格
6. **编辑点评** —— 模型对该期内容的批判性评估：哪里论证薄弱、哪里值得深挖

超长文字稿（约 >5 万字）自动切换「分段精读 → 汇总成文」模式，时间戳全程保留。

## 网络问题（国内环境常见）

首次运行转写模型会从 HuggingFace 下载约 1-2 GB。如果直连缓慢或报 SSL 错误（`huggingface_hub` 的下载器在部分网络下会被重置），用手动方式把模型放到本地目录，工具会自动识别：

```bash
# 1. 找到模型 commit（以 4bit 版为例，约 550MB，中文场景推荐）
#    https://huggingface.co/mlx-community/whisper-large-v3-turbo-4bit/tree/main
SHA=$(curl -sL "https://hf-mirror.com/api/models/mlx-community/whisper-large-v3-turbo-4bit" | python3 -c "import json,sys;print(json.load(sys.stdin)['sha'])")

# 2. 下载到约定目录（文件名必须是 weights.safetensors）
D=~/.cache/podcast-article/models/whisper-large-v3-turbo-4bit
mkdir -p $D
curl -L -C - -o $D/weights.safetensors "https://huggingface.co/mlx-community/whisper-large-v3-turbo-4bit/resolve/$SHA/model.safetensors"
curl -L -o $D/multilingual.tiktoken "https://huggingface.co/mlx-community/whisper-large-v3-turbo-4bit/resolve/$SHA/multilingual.tiktoken"

# 3. 运行时指定 --model 4bit（也支持 8bit，或任意本地目录 / HF repo id）
uv run podcast-article <链接> --model 4bit
```

镜像站 `hf-mirror.com` 可用于 API 请求（如第 1 步）；直连 `huggingface.co` 的 `curl` 下载通常可用。若两者都慢，把命令里的域名互换试试。

## 成本参考

- 转写：本地 mlx-whisper，免费；1 小时音频约 3-8 分钟（M 系列芯片，large-v3-turbo）
- 总结：DeepSeek API，2 小时播客单次总结约几分钱人民币

## 项目结构

```
podcast_article/
├── cli.py            # 命令行入口
├── pipeline.py       # 全流程编排 + 目录级缓存
├── sources/          # 链接解析：小宇宙 / RSS / Apple / yt-dlp(YouTube·B站)
├── subtitles.py      # 平台字幕下载与 VTT/SRT 解析
├── transcribe.py     # 本地转写（mlx-whisper 优先，faster-whisper 回退，含进度解析）
├── summarize.py      # DeepSeek 精读与文章生成（直读 / 分段精读两种模式）
├── notion.py         # markdown → Notion blocks，写入 Notion
├── mcp_server.py     # MCP 服务器（stdio）：analyze/list/get/push_to_notion
├── config.py         # API key 与模型配置（.env）
└── util.py           # slug / 时间戳 / shownotes 清洗

webapp.py             # Web 界面服务（Flask，端口 8787，SSE 实时推送）
web/index.html        # 前端单页（OpenAI 风格，零构建，Inter 字体自托管）
```
