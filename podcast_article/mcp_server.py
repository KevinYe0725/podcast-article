"""MCP 服务器：把 podcast-article 的能力暴露给任意 MCP 客户端（Claude Desktop、DSH 等）。

运行：uv run podcast-article-mcp   （stdio 传输）
客户端配置示例（Claude Desktop / 通用 mcpServers JSON）：
{
  "mcpServers": {
    "podcast-article": {
      "command": "uv",
      "args": ["--directory", "/Users/kevinye/Projects/podcast-analysis", "run", "podcast-article-mcp"]
    }
  }
}
"""
from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import notion
from .config import PROJECT_ROOT
from .pipeline import Pipeline

OUTPUT_ROOT = PROJECT_ROOT / "output"

mcp = FastMCP(
    "podcast-article",
    instructions="把播客/视频链接变成深度文章，并可写入 Notion。分析一集需要几分钟，请耐心等待工具返回。",
)


def _collect_logs() -> tuple[list, callable]:
    logs: list[str] = []

    def log(msg: str) -> None:
        logs.append(msg)

    return logs, log


@mcp.tool()
def analyze_podcast(
    url: str,
    language: str = "auto",
    no_subs: bool = False,
    force_transcript: bool = False,
    force_article: bool = False,
    pick: int = 1,
) -> str:
    """分析一期播客/视频并生成深度文章（可能需要 1-15 分钟）。

    Args:
        url: 小宇宙单集 / YouTube / Bilibili / Apple Podcasts / RSS 链接，或本地音视频路径
        language: 转写语言 auto/zh/en/...
        no_subs: 强制语音转写，不使用平台字幕
        force_transcript: 忽略缓存重新转写
        force_article: 忽略缓存重新生成文章
        pick: 输入为 RSS/节目 feed 时取第 N 新的单集
    """
    logs, log = _collect_logs()
    try:
        pipe = Pipeline(
            url=url,
            output_dir=OUTPUT_ROOT,
            language=language,
            no_subs=no_subs,
            force_transcript=force_transcript,
            force_article=force_article,
            pick=pick,
            log=log,
        )
        article_path = pipe.run()
    except Exception as exc:
        return "任务失败：" + str(exc) + "\n\n已执行步骤的日志：\n" + "\n".join(logs[-10:])

    article = article_path.read_text(encoding="utf-8")
    return (
        f"完成。输出目录：output/{article_path.parent.name}/\n"
        f"产物：article.md / transcript.txt / meta.json\n\n"
        f"--- 文章全文 ---\n\n{article}"
    )


@mcp.tool()
def list_episodes() -> str:
    """列出所有已生成的单集（目录名、标题、播客、是否已有文章）。"""
    items = []
    if OUTPUT_ROOT.exists():
        for d in sorted(OUTPUT_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            meta_file = d / "meta.json"
            if not d.is_dir() or not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            items.append(
                {
                    "dir": d.name,
                    "title": meta.get("title"),
                    "podcast": meta.get("podcast"),
                    "has_article": (d / "article.md").exists(),
                }
            )
    return json.dumps(items, ensure_ascii=False, indent=1)


@mcp.tool()
def get_article(episode_dir: str) -> str:
    """读取某一集已生成的文章 markdown 全文。用 list_episodes 先查目录名。"""
    path = OUTPUT_ROOT / episode_dir / "article.md"
    if not path.is_file():
        return f"不存在：{episode_dir}/article.md（用 list_episodes 查可用目录）"
    return path.read_text(encoding="utf-8")


@mcp.tool()
def push_to_notion(episode_dir: str) -> str:
    """把某一集的文章写入 Notion（页面标题 = 单集标题，正文 = 文章全文 + 原文链接）。

    需要在 .env 配置 NOTION_TOKEN，以及 NOTION_DATABASE_ID（写入数据库）或
    NOTION_PARENT_PAGE_ID（作为子页面）。返回创建的 Notion 页面 URL。
    """
    base = OUTPUT_ROOT / episode_dir
    article = base / "article.md"
    meta_file = base / "meta.json"
    if not article.is_file() or not meta_file.is_file():
        return f"目录 {episode_dir} 里没有 article.md / meta.json（用 list_episodes 查可用目录）"
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    title = f"{meta.get('podcast', '')}｜{meta.get('title', '')}".strip("｜")
    url = notion.push_article(
        title=title,
        markdown_text=article.read_text(encoding="utf-8"),
        source_url=meta.get("url"),
        podcast=meta.get("podcast"),
        pub_date=meta.get("pub_date"),
        duration=meta.get("duration"),
    )
    return f"已写入 Notion：{url}"


def main() -> None:
    OUTPUT_ROOT.mkdir(exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()
