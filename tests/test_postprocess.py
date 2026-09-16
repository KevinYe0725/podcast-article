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


def test_fix_orphan_range_timestamp_merges_into_quote():
    """区间形态的孤儿时间戳也要并回引文。

    模型引用一段跨十几秒的话时写的是 `[00:06:36-00:06:49]`；只认单个时间点的那个版本
    会把它当成普通文本，于是成稿里留下一行孤零零的时间戳。
    """
    out, n = pp.fix_orphan_timestamps("> 他说了一句话。\n\n[00:06:36-00:06:49]\n\n下一段")
    assert n == 1, f"应修掉 1 处，实际 {n}"
    assert out.splitlines()[0] == "> 他说了一句话。 [00:06:36-00:06:49]", f"没并上去：{out.splitlines()[0]!r}"


def test_fix_orphan_timestamp_drops_when_line_already_has_one():
    """上一行已经有时间戳（含区间）时，孤儿行直接丢掉，不要叠两个。"""
    out, n = pp.fix_orphan_timestamps("> 引文 [00:06:36-00:06:49]\n\n[00:20:00]\n\n尾")
    assert n == 1 and out.count("[") == 1, f"孤儿时间戳应被丢掉：{out!r}"


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


# ---------------------------------------------------------------- 开篇闸门
#
# 这些用例的第二组是**真实文章的原文开头**（含用户库里 6 篇好文章）：
# 闸门必须一个都不误判 —— 误判会把好开头逼着重写，比漏判更糟。


def test_opening_gate_flags_report_style():
    from podcast_article.outline import opening_problems

    # 简报式：某年某月 + 机构职位 + 发言动词（用户库里那篇平开头的真实样子）
    got = opening_problems(
        "2025 年 11 月初，OpenAI 首席财务官 Sarah Fryer 在一场公开会议上说，"
        "希望美国政府提供担保，她用的词是 backstop。几周后，资金又回来了。"
    )
    assert got, "简报式开头必须被判不合格"
    assert any("简报" in p or "机构" in p for p in got), f"问题描述要说清是什么毛病：{got}"


def test_opening_gate_flags_weak_openers():
    from podcast_article.outline import opening_problems

    for text in ("随着大模型能力的提升，推理成本成为行业关注的核心问题。",
                 "近年来，越来越多的人开始关注播客这种媒介。",
                 "作为一家成立十年的公司，他们的路径很有代表性。"):
        assert opening_problems(text), f"以铺垫起句应当被判不合格：{text}"


def test_opening_gate_flags_question_and_long_first_sentence():
    from podcast_article.outline import opening_problems

    assert opening_problems("一家亏损百亿的公司为什么要向国家要兜底？"), "疑问句起句应被判不合格"
    # 阈值 70：实测真实的长首句是 58-60 字，压在阈值下不会被误判
    long_first = ("他站在那片被地震和大火烧过的废墟里，看着自己毕生命名的几百条鱼重新回到混沌当中，"
                  "然后慢慢地弯下腰去，从瓦砾里捡起了那根缝衣针，又把它放回了口袋里，转身走开了，"
                  "没有再回头看一眼那片被烧成灰的东西。")
    assert opening_problems(long_first), "第一句过长应被判不合格"


def test_opening_gate_flags_empty_vagueness():
    from podcast_article.outline import opening_problems

    got = opening_problems("这是一次很有意义的对话，值得每个人认真思考。")
    assert got, "既无原话、又无数字、又无动作的空泛开头应被判不合格"


def test_opening_gate_never_flags_real_good_openings():
    """**关键**：用户库里那些真实的好开头，一个都不许被判不合格。

    曾经误判过「凌晨两点，前台电话响了」（中文数字不算数字、画面里也没有引号），
    所以判定收成了「三样全无才判」。
    """
    from podcast_article.outline import opening_problems

    good = [
        "凌晨两点，前台电话响了。夜班经理被告知，门外停着无限辆巴士。",
        "1906 年 4 月 18 日清晨，旧金山在地震中抖了 47 秒，随后的大火夺走了三千多人的性命。",
        "台上那个人说，他没写一行代码。他手里只有一个从后台顺来的 Xbox 手柄。",
        "他 22 岁，山东人，高考 400 分出头，因为侏儒症一直待在家里，靠打零工维持生活。",
        "周二尾盘，比特币跌 4.4%，Coinbase 盘中一度跌 11.25%。",
        "他卷起袖子一阵翻找，最终找到了一根缝衣针。",
        "「我没写一行代码。」他说这话时手里只有一个手柄。",
    ]
    for text in good:
        got = opening_problems(text)
        assert not got, f"好开头被误判了：{text}\n  问题：{got}"


def test_opening_gate_handles_empty():
    from podcast_article.outline import opening_problems

    assert opening_problems("") == ["开篇是空的"]
    assert opening_problems("   \n\n  ") == ["开篇是空的"]
