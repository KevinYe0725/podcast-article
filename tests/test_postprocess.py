"""确定性排版收尾的单元测试（这些正则是启发式的，最需要测试兜底）。"""
from podcast_article import postprocess as pp


def test_split_long_paragraph():
    text = "第一句。" + "内容。" * 80
    out, n = pp.split_long_paragraphs(text)
    assert n == 1
    assert all(len(b) <= 200 for b in out.split("\n\n"))


def test_split_skips_markdown_blocks():
    text = "# 标题\n\n- 一条\n- 两条\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    out, n = pp.split_long_paragraphs(text)
    assert n == 0 and out == text


def test_fix_orphan_timestamp_merges_into_quote():
    out, n = pp.fix_orphan_timestamps("> 他说了一句话。\n\n[00:10:15]\n\n下一段")
    assert n == 1
    assert out.splitlines()[0] == "> 他说了一句话。 [00:10:15]"


def test_normalize_punctuation_and_number_spacing():
    out, _ = pp.normalize_punctuation("他出生于1906年,那是一个数字:7.9级地震")
    assert "1906 年" in out and "，" in out and "：" in out


def test_strip_filler():
    out, n = pp.strip_filler("值得注意的是，他来了。总的来说，这很好。")
    assert n == 2 and "值得注意的是" not in out and "总的来说" not in out


def test_trim_dangling_last_paragraph():
    text = "第一段完整。\n\n第二段写到一半就断"
    out, n = pp.trim_dangling(text)
    assert n == 1 and out.strip() == "第一段完整。"


def test_trim_dangling_drops_tiny_fragment():
    out, n = pp.trim_dangling("正文完整。\n\n他")
    assert n == 1 and "他" not in out.split("\n\n")[-1]


def test_trim_dangling_keeps_lists():
    text = "正文。\n\n- 一条\n- 两条"
    out, n = pp.trim_dangling(text)
    assert n == 0 and out == text


def test_dedupe_quotes_keeps_longest():
    text = ("> 同一句话 [00:10:07]\n\n正文。\n\n"
            "> 同一句话，但更完整一些 [00:10:07]")
    out, n = pp.dedupe_quotes(text)
    assert n == 1
    assert "更完整一些" in out and out.count("[00:10:07]") == 1


def test_trim_to_budget_protects_fixed_sections():
    # 必须超过「上限 × 1.6」才会触发裁剪（concise 上限 4200 → 6720 字）
    body = "\n\n".join(f"## 第{i}节\n" + "内容。" * 400 for i in range(1, 10))
    text = f"# 标题\n\n{body}\n\n## 编辑点评\n" + "点评。" * 60
    assert len(text) > pp.MODE_TARGETS["concise"][1] * 1.6
    out, dropped = pp.trim_to_budget(text, "concise")
    assert dropped, "超长时应当裁掉部分章节"
    assert "编辑点评" in out, "固定栏目不能被裁掉"
    assert len(out) < len(text)


def test_finalize_reports_and_shrinks():
    text = "# 标题\n\n" + "值得注意的是，" + "内容。" * 60 + "\n\n[00:01:02]\n"
    out, report = pp.finalize(text, "concise")
    assert report["最终字数"] == len(out)
    assert "值得注意的是" not in out
