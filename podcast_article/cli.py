"""CLI 入口：podcast-article <链接>，以及书库/记忆的子命令。

两种用法都保留：`podcast-article <链接>` 是最常用的主路径，所以它继续走位置参数；
`search / ask / remember / recall / forget / index / status` 是子命令。
判据只有一个：第一个参数是不是已知子命令名 —— 不是，就当链接处理（老命令不会失效）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from . import kb
from .config import PROJECT_ROOT
from .pipeline import Pipeline

console = Console(highlight=False)

BANNER = """[bold cyan]podcast-article[/] — 播客/视频链接 → 深度文章"""

SUBCOMMANDS = ("search", "ask", "remember", "recall", "forget", "index", "status")

KIND_CHOICES = list(kb.MEMORY_KINDS)


def _fmt_ts(sec) -> str:
    if sec is None:
        return ""
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="podcast-article",
        description="给一个播客/视频链接，产出一篇有价值的深度文章（抓取 → 转写 → DeepSeek 精读）",
    )
    parser.add_argument("url", help="小宇宙 / YouTube / Bilibili / Apple Podcasts / RSS 链接，或本地音视频路径")
    parser.add_argument("-o", "--output", default="output", help="输出根目录（默认 ./output）")
    parser.add_argument("--lang", default="auto", help="转写语言：auto/zh/en/...（默认 auto）")
    parser.add_argument("--backend", default="auto", choices=["auto", "mlx", "faster"], help="转写后端（默认自动选择）")
    parser.add_argument("--model", default=None, help="转写模型名（默认 large-v3-turbo）")
    parser.add_argument("--llm-model", default=None, help="DeepSeek 模型名（默认 deepseek-chat）")
    parser.add_argument("--no-subs", action="store_true", help="有平台字幕也不用，强制语音转写")
    parser.add_argument("--force-transcript", action="store_true", help="忽略已有文字稿，重新生成")
    parser.add_argument("--force-article", action="store_true", help="忽略已有文章，重新生成")
    parser.add_argument("--refresh", action="store_true", help="音频/文字稿/文章全部重新生成")
    parser.add_argument("--pick", type=int, default=1, help="输入是 RSS/节目主页时选第 N 新的单集（默认 1=最新）")
    parser.add_argument("--max-chars", type=int, default=75_000, help="单次直读文字稿上限，超过走分段精读")
    return parser


def _build_sub_parser(sub: argparse.ArgumentParser) -> None:
    acts = sub.add_subparsers(dest="cmd", required=True)

    p_search = acts.add_parser("search", help="在书库里跨集检索原文（带出处）")
    p_search.add_argument("query")
    p_search.add_argument("-k", type=int, default=8, help="返回条数（默认 8）")

    p_ask = acts.add_parser("ask", help="就整个书库提问（会调用模型，产生费用）")
    p_ask.add_argument("question")

    p_rem = acts.add_parser("remember", help="记住一条（偏好/事实/决定/洞察）")
    p_rem.add_argument("text")
    p_rem.add_argument("--kind", default="fact", choices=KIND_CHOICES)
    p_rem.add_argument("--tags", default="")
    p_rem.add_argument("--dir", default="", help="来源那一集的目录名（以后追得回去）")
    p_rem.add_argument("--pin", action="store_true", help="置顶：每次提问都带上它")

    p_rec = acts.add_parser("recall", help="看看记住了什么（带「用过几次」）")
    p_rec.add_argument("query", nargs="?", default="")
    p_rec.add_argument("--never-used", action="store_true", help="只看从没用上过的（清理用）")

    p_for = acts.add_parser("forget", help="删掉一条记忆")
    p_for.add_argument("id", type=int)

    p_idx = acts.add_parser("index", help="重建知识库索引")
    p_idx.add_argument("--force", action="store_true", help="连没变的也重建（慢）")
    p_idx.add_argument("--no-embed", action="store_true", help="不生成向量（语义检索会关掉）")

    acts.add_parser("status", help="书库与记忆的规模")


def _run_kb_command(args) -> int:
    if args.cmd == "search":
        res = kb.search(args.query, k=max(1, args.k))
        if not res["hits"]:
            console.print("[yellow]没有命中。[/]换个说法，或先 `podcast-article index`。")
            return 1
        console.print(f"[dim]模式 {res['mode']}，{len(res['hits'])} 条[/]")
        for h in res["hits"]:
            where = h.get("title") or h.get("dir") or ""
            tail = " · ".join(x for x in (h.get("heading"), _fmt_ts(h.get("start_sec")),
                                          "文字稿" if h.get("doc_kind") == "transcript" else "") if x)
            console.print(f"\n[bold]{where}[/]" + (f" [dim]· {tail}[/]" if tail else ""))
            console.print(f"  {h.get('text', '')[:300]}")
        return 0

    if args.cmd == "ask":
        from . import summarize
        res = kb.search(args.question, k=6)
        hits = [kb._hit_public(h, limit=700) for h in res["hits"]]
        if not hits:
            console.print("[yellow]书库里没有检索到相关内容。[/]")
            return 1
        lines = []
        for i, h in enumerate(hits, 1):
            where = (h.get("title") or h.get("dir") or "")
            if h.get("heading"):
                where += f" · {h['heading']}"
            lines.append(f"[{i}] {where}\n{h['text']}")
        mem = kb.memory_for_prompt(args.question)
        mem_text = "\n".join(f"- {m['text']}" for m in mem) or "（没有）"
        answer = summarize.ask_once(
            "你是这位读者私人播客书库的研究助手。只依据资料片段回答，"
            "资料里没有的就说「资料里没有」。先结论后依据，依据标编号（如 [2]）。",
            f"读者的问题：{args.question}\n\n【关于这位读者的已知信息】\n{mem_text}\n\n"
            f"【资料片段】\n" + "\n\n".join(lines))
        console.print(answer.strip())
        console.print("\n[dim]依据：[/]")
        for i, h in enumerate(hits, 1):
            console.print(f"[dim][{i}] {h.get('title') or h.get('dir')}"
                          + (f" · {h['heading']}" if h.get("heading") else "") + "[/]")
        return 0

    if args.cmd == "remember":
        try:
            item = kb.memory_add(args.text, kind=args.kind, tags=args.tags,
                                 source_dir=args.dir, source_kind="cli", pinned=args.pin)
        except ValueError as exc:
            console.print(f"[red]没存：[/]{exc}")
            return 1
        console.print(f"[green]已记住[/] id={item['id']} kind={item['kind']}"
                      + (" [dim](置顶)[/]" if item["pinned"] else ""))
        console.print(f"  {item['text']}")
        return 0

    if args.cmd == "recall":
        items = kb.memory_never_used() if args.never_used else kb.memory_list(
            query=args.query, limit=50)
        if not items:
            console.print("[yellow]还没有记忆。[/]用 `podcast-article remember \"...\"` 存一条。")
            return 0
        for m in items:
            used = m.get("use_count") or 0
            mark = "[green]★[/]" if m["pinned"] else " "
            usage = f"用过 {used} 次" if used else "[yellow]从未用过[/]"
            console.print(f"{mark} [bold]#{m['id']}[/] [dim]{m['kind']} · {usage}[/] {m['text']}")
        return 0

    if args.cmd == "forget":
        if kb.memory_delete(args.id):
            console.print(f"[green]已删除[/] #{args.id}")
            return 0
        console.print(f"[yellow]没有 #{args.id} 这条记忆。[/]用 `recall` 看 id。")
        return 1

    if args.cmd == "index":
        res = kb.index_all(PROJECT_ROOT / "output", force=args.force, embed=not args.no_embed,
                           log=lambda msg: console.print(f"[dim]{msg}[/]", markup=False))
        console.print(f"[green]✔ 索引完成[/] {res['episodes']} 集 / {res['passages']} 个新切片"
                      f" / 补向量 {res.get('vectors', 0)} 条")
        return 0

    if args.cmd == "status":
        s = kb.stats()
        console.print(f"书库：{s.get('episodes_on_disk', 0)} 集 / {s.get('passages', 0)} 切片"
                      f" / {s.get('entities', 0)} 实体")
        console.print(f"检索：语义 {'开' if s.get('semantic') else '关'}"
                      f"（向量 {s.get('vectors', 0)} 条，模型 {s.get('model', '')}）")
        if s.get("embed_error"):
            console.print(f"[yellow]语义检索不可用：{s['embed_error']}[/]")
        never = kb.memory_never_used()
        console.print(f"记忆：{s.get('memory', 0)} 条，其中 {len(never)} 条从未用过")
        console.print(f"数据库：{s.get('db')}")
        return 0

    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    console.print(BANNER)

    if argv and argv[0] in SUBCOMMANDS:
        sub = argparse.ArgumentParser(prog="podcast-article")
        _build_sub_parser(sub)
        args = sub.parse_args(argv)
        try:
            return _run_kb_command(args)
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            console.print(f"[bold red]出错：[/]{exc}")
            return 1

    args = build_parser().parse_args(argv)

    pipe = Pipeline(
        url=args.url,
        output_dir=Path(args.output),
        language=args.lang,
        backend=args.backend,
        asr_model=args.model,
        llm_model=args.llm_model,
        no_subs=args.no_subs,
        force_transcript=args.force_transcript or args.refresh,
        force_article=args.force_article or args.refresh,
        pick=args.pick,
        max_chars=args.max_chars,
        log=lambda msg: console.print(msg, markup=False, highlight=False),
    )

    try:
        article = pipe.run()
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。已完成的步骤已缓存，重新运行会自动续上。[/]")
        return 130
    except Exception as exc:
        console.print(f"[bold red]出错：[/]{exc}")
        return 1

    console.print(f"\n[bold green]✔ 完成[/] 文章：[underline]{article}[/]")
    console.print(f"   同目录下还有 transcript.txt（文字稿）、meta.json（元信息）")
    _index_quietly(article.parent)
    return 0


def _index_quietly(ep_dir: Path) -> None:
    """把刚生成的文章存进知识库（失败只提示一行，绝不影响「文章已生成」这个事实）。

    网页端也有同样的动作（webapp._after_done）。命令行这边漏了它的话，
    「收集库」就会按你用什么方式生成而时有时无 —— 用户不该记得这件事。
    """
    try:
        conn = kb.ensure_schema(kb.connect())
        try:
            r = kb.index_dir(conn, ep_dir, log=lambda *_: None)
        finally:
            conn.close()
        console.print(f"   已存入知识库：{r.get('passages', 0)} 个切片")
    except Exception as exc:
        console.print(f"   [yellow]入库失败（不影响文章）：{exc}[/]")


if __name__ == "__main__":
    sys.exit(main())
