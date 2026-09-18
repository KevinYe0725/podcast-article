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

from . import kb, notion
from .config import PROJECT_ROOT
from .pipeline import Pipeline

OUTPUT_ROOT = PROJECT_ROOT / "output"

mcp = FastMCP(
    "podcast-article",
    instructions=(
        "把播客/视频链接变成深度文章，并可写入 Notion。分析一集需要几分钟，请耐心等待工具返回。"
        "另外还带一个私人知识库与记忆：用 search_library 跨集检索原文、用 ask_library 跨集问答；"
        "当用户表达出偏好、结论、决定或值得长期记住的事实，用 remember 存下来，"
        "之后用 recall 取回。记忆是用户自己的东西 —— 不要替他编造，也不要因为一次闲聊就乱存。"
    ),
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


# ---------------------------------------------------------------- 知识库与记忆

@mcp.tool()
def search_library(query: str, k: int = 6) -> str:
    """在他的私人播客书库里跨集检索原文，返回带出处的片段（哪一集、小标题、时间戳）。

    回答「我以前在哪一期听过关于 X 的内容」这类问题时先搜这里，不要凭印象回答：
    书库是他自己收集的材料，命中就能点回原音频位置。
    """
    res = kb.search(query, k=max(1, min(20, k)))
    if not res["hits"]:
        return json.dumps({"hits": [], "note": "没有命中。可以换个说法，或先让用户在界面上重建索引。"},
                          ensure_ascii=False)
    hits = []
    for h in res["hits"]:
        pub = kb._hit_public(h, limit=800)
        pub["从哪一期"] = h.get("title") or h.get("dir")
        hits.append(pub)
    return json.dumps({"mode": res["mode"], "hits": hits}, ensure_ascii=False, indent=1)


@mcp.tool()
def ask_library(question: str) -> str:
    """就整个书库提问，得到一句带出处的回答（**会调用模型，产生费用**）。

    适合「我这一年在这堆播客里学到的东西，关于 X 有哪些说法」这类跨集问题。
    只依据书库里的原文回答；书库里没有的会说没有。
    """
    res = kb.search(question, k=6)
    hits = [kb._hit_public(h, limit=700) for h in res["hits"]]
    if not hits:
        return "书库里没有检索到相关内容（可以先在界面上重建索引，或换个说法）。"
    from . import library_ask
    mem = kb.memory_for_prompt(question)
    answer = library_ask.answer(question, hits, [m["text"] for m in mem])
    src = "\n".join(f"[{i}] {h.get('title') or h.get('dir')}"
                    + (f" · {h['heading']}" if h.get("heading") else "") for i, h in enumerate(hits, 1))
    return f"{answer.strip()}\n\n---\n依据：\n{src}"


@mcp.tool()
def remember(text: str, kind: str = "fact", tags: str = "", episode_dir: str = "") -> str:
    """把一条关于用户的信息长期记住（他说的偏好、结论、决定、值得记的事实）。

    kind：preference（偏好）/ fact（事实）/ entity（人、机构、作品）/ decision（决定）/ insight（洞察）。
    **只在用户明确表达或认可时调用**，不要从闲聊里替他总结；episode_dir 填来源那一集的目录名，
    以后才追得回「这条是哪来的」。用户说要忘记某条时用 forget。
    """
    try:
        item = kb.memory_add(text, kind=kind, tags=tags, source_dir=episode_dir,
                             source_kind="mcp")
    except ValueError as exc:
        return f"没存：{exc}"
    return f"已记住（id={item['id']}，kind={item['kind']}）：{item['text']}"


@mcp.tool()
def recall(query: str = "", limit: int = 10) -> str:
    """取回他之前让你记住的东西（不带 query 就按最近更新列出）。

    返回里带 use_count / last_used_at：次数为 0 的是从没用上过的记忆 ——
    他问「我记过什么、有没有没用的」时，如实把这些说出来。
    """
    items = kb.memory_list(query=query, limit=max(1, min(50, limit)))
    if not items:
        return json.dumps({"items": [], "note": "还没有存过记忆。"}, ensure_ascii=False)
    return json.dumps(
        [{"id": m["id"], "kind": m["kind"], "text": m["text"], "tags": m.get("tags") or "",
          "来源": m.get("source_dir") or "", "置顶": bool(m["pinned"]),
          "用过次数": m.get("use_count", 0)} for m in items],
        ensure_ascii=False, indent=1)


@mcp.tool()
def forget(memory_id: int) -> str:
    """删掉一条记忆（用户说「忘掉这个」时用）。id 从 recall 的结果里取。"""
    return "已删除。" if kb.memory_delete(memory_id) else f"没有 id={memory_id} 这条记忆。"


def main() -> None:
    OUTPUT_ROOT.mkdir(exist_ok=True)
    mcp.run()

if __name__ == "__main__":
    main()
