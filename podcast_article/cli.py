"""CLI 入口：podcast-article <链接>。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from .pipeline import Pipeline

console = Console(highlight=False)

BANNER = """[bold cyan]podcast-article[/] — 播客/视频链接 → 深度文章"""


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


def main(argv: list[str] | None = None) -> int:
    console.print(BANNER)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
