"""导出模块：front matter、文件名安全化、独立 HTML、zip 打包、下载入口。

全部跑在 tmp_output / episode fixture 造的临时目录上，不碰真实 output/。

关于 YAML 校验：这里**刻意不引入 pyyaml**（它不在依赖清单里，只为一个断言加依赖不划算），
改成手写断言 —— front matter 行数固定、引号成对、字段名齐全 + 转义序列精确匹配。
这几条足够抓住「标题里带引号/换行会毁掉 YAML」这类回归，而不用真的去解析它。
"""
import json
import re
import zipfile
from html import escape
from pathlib import Path
from urllib.parse import unquote

import pytest

from podcast_article import export

FRONT_MATTER_KEYS = ["title", "podcast", "author", "date", "duration", "url", "source"]


# ---------------------------------------------------------------- 测试内的小工具


def _make_episode(root: Path, dir_name: str, *, article="""# 正文标题

正文 [00:10:07] 和一段解释。

> 这是原话 [00:10:07]

| 项目 | 值 |
| --- | --- |
| 时长 | 61 分钟 |

```python
x = 1  # 代码块里的 [00:10:07] 不该被包成 span
```
""", meta=None, transcript=None) -> Path:
    """在临时输出目录里手搓一集（article/meta/transcript 都可选）。"""
    d = root / dir_name
    d.mkdir(parents=True, exist_ok=True)
    if article is not None:
        (d / "article.md").write_text(article, encoding="utf-8")
    if meta is not None:
        raw = meta if isinstance(meta, str) else json.dumps(meta, ensure_ascii=False)
        (d / "meta.json").write_text(raw, encoding="utf-8")
    if transcript is not None:
        (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    return d


def _front_matter(text: str) -> list[str]:
    """切出 front matter 的字段行（不含两行 ---）；顺便验证 --- 包裹正确。"""
    lines = text.splitlines()
    assert lines[0] == "---", "front matter 必须以 --- 开头"
    assert lines[-1] != "---", "--- 之后应该还有正文"
    end = lines.index("---", 1)
    return lines[1:end]


def _fields(lines: list[str]) -> dict[str, str]:
    """字段行 -> {名: 原始值字符串}（不做 YAML 解析，只按 "名: 值" 切）。"""
    out: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition(": ")
        assert sep, f"字段行格式不是 `名: 值`：{line!r}"
        assert key.isascii() and key == key.strip(), f"字段名不规范：{key!r}"
        out[key] = value
    return out


def _quotes_balanced(value: str) -> bool:
    """数一遍没被反斜杠转义的双引号，要求出现偶数次（配对成功）。"""
    count = 0
    i = 0
    while i < len(value):
        if value[i] == "\\":
            i += 2
            continue
        if value[i] == '"':
            count += 1
        i += 1
    return count % 2 == 0


ILLEGAL = set('/\\:*?"<>|')


# ---------------------------------------------------------------- article_markdown


def test_article_markdown_front_matter_complete(episode, tmp_output):
    name, text = export.article_markdown(tmp_output, episode.name)

    assert name == "测试台-测试单集.md"
    fields = _fields(_front_matter(text))
    assert list(fields) == FRONT_MATTER_KEYS
    assert fields["title"] == '"测试单集"'
    assert fields["podcast"] == '"测试台"'
    assert fields["author"] == '"某人"'
    assert fields["date"] == '"2024-01-01"'          # pub_date 归一化成日期
    assert fields["duration"] == "3661"              # 纯秒数不加引号
    assert fields["url"] == '"https://example.com/ep1"'
    assert fields["source"] == '"example.com"'       # 没有 source 就退回域名

    # front matter 之后紧跟原文，正文一个字符都没丢
    body = text.split("\n---\n", 1)[1]
    assert body.lstrip("\n").startswith("# 测试标题")
    assert episode.joinpath("article.md").read_text(encoding="utf-8").strip() in text


def test_front_matter_escapes_quotes_and_newlines(tmp_output):
    meta = {
        "title": '他说"你好"\n第二行',
        "podcast": "读书:电台",
        "author": 'A"B',
        "pub_date": "不是日期",
        "duration": "未知",
        "url": "",
    }
    _make_episode(tmp_output, "dir-yaml", article="# 标题\n", meta=meta)
    _, text = export.article_markdown(tmp_output, "dir-yaml")

    lines = _front_matter(text)
    # 标题里的换行必须转义成 \n，否则一条字段会被撑成两行
    assert len(lines) == len(FRONT_MATTER_KEYS)
    assert len([l for l in lines if l.startswith("title: ")]) == 1

    fields = _fields(lines)
    for value in fields.values():
        assert _quotes_balanced(value), f"引号没配对：{value!r}"
    assert fields["title"] == '"他说\\"你好\\"\\n第二行"'
    assert '"' not in fields["podcast"] or fields["podcast"] == '"读书:电台"'
    assert fields["date"] == '"不是日期"'            # 解析不出来就原样带引号
    assert fields["duration"] == '"未知"'
    assert fields["url"] == '""'
    assert fields["source"] == '""'


def test_front_matter_tolerates_missing_and_broken_meta(tmp_output):
    _make_episode(tmp_output, "dir-nometa", article="# 只有正文\n", meta=None)
    name, text = export.article_markdown(tmp_output, "dir-nometa")
    fields = _fields(_front_matter(text))
    assert name == "dir-nometa.md"                    # 名字回退成目录名
    assert fields["title"] == '"dir-nometa"'
    assert fields["podcast"] == '""'
    assert fields["duration"] == '""'

    _make_episode(tmp_output, "dir-badjson", article="# 正文\n", meta="{ 这不是 json")
    name2, text2 = export.article_markdown(tmp_output, "dir-badjson")
    assert name2 == "dir-badjson.md"                  # 坏 meta 不抛异常
    assert _fields(_front_matter(text2))["title"] == '"dir-badjson"'

    _make_episode(tmp_output, "dir-metamissing", article="# 正文\n")   # 连 meta.json 都没有
    assert export.article_markdown(tmp_output, "dir-metamissing")[0] == "dir-metamissing.md"


def test_filename_strips_illegal_chars(tmp_output):
    _make_episode(
        tmp_output, "dir-illegal",
        meta={"title": 'a/b:c*d?e"f<g>h|i\\j\n换行', "podcast": "P/Q"},
    )
    name, _ = export.article_markdown(tmp_output, "dir-illegal")
    assert not (set(name) & ILLEGAL), f"文件名里还有非法字符：{name!r}"
    assert "\n" not in name and "\t" not in name
    assert name == name.strip(" .")                   # 首尾空格/点已清掉
    assert name.endswith(".md") and name != ".md"


def test_filename_trims_leading_trailing_spaces_and_dots(tmp_output):
    _make_episode(tmp_output, "dir-space", meta={"title": "  .标题.  ", "podcast": "测试台"})
    name, _ = export.article_markdown(tmp_output, "dir-space")
    assert name == "测试台-标题.md"


def test_filename_length_capped_at_80_chars(tmp_output):
    _make_episode(tmp_output, "dir-long", meta={"title": "很长" * 60, "podcast": "台"})
    name, _ = export.article_markdown(tmp_output, "dir-long")
    assert len(name) == export.MAX_FILENAME == 80     # 中文按 1 个字符算
    assert name.endswith(".md") and not (set(name) & ILLEGAL)

    # 后缀也算进上限：文字稿名的后缀更长
    _make_episode(tmp_output, "dir-long2", meta={"title": "很长" * 60, "podcast": "台"},
                  transcript="[00:00:01] 开头\n")
    txt_name = export.export_episode(tmp_output, "dir-long2", "txt")[0]
    assert len(txt_name) <= 80 and txt_name.endswith("-文字稿.txt")


def test_filename_falls_back_to_dir_name_when_title_is_all_illegal(tmp_output):
    # 标题清理后只剩空串（全是空格/点/非法字符）→ 回退成目录名
    _make_episode(tmp_output, "20240101-测试台-测试单集",
                  meta={"title": " .:. \n .", "podcast": ""})
    name, _ = export.article_markdown(tmp_output, "20240101-测试台-测试单集")
    assert name == "20240101-测试台-测试单集.md"


def test_article_markdown_missing_article_raises(tmp_output):
    empty = tmp_output / "dir-empty"
    empty.mkdir()                                     # 有目录、没有 article.md
    (empty / "meta.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="不存在"):
        export.article_markdown(tmp_output, "dir-empty")
    with pytest.raises(ValueError, match="不存在"):
        export.article_markdown(tmp_output, "根本没有这个目录")


def test_article_markdown_rejects_path_traversal(tmp_output):
    for bad in ("../外面的目录", "a/b", "..", ".", ""):
        with pytest.raises(ValueError, match="不存在"):
            export.article_markdown(tmp_output, bad)


# ---------------------------------------------------------------- article_html


def test_article_html_is_standalone_document(episode, tmp_output):
    title = '我的"标题" & 说明'
    doc = export.article_html(tmp_output, episode.name, title=title)

    assert doc.startswith("<!DOCTYPE html>")
    assert doc.rstrip().endswith("</html>")
    assert "<style>" in doc and "</style>" in doc
    assert f"<title>{escape(title)}</title>" in doc       # 页面标题做 HTML 转义
    assert "max-width: 720px" in doc and "line-height: 1.75" in doc
    assert '"PingFang SC"' in doc and "sans-serif" in doc  # 系统中文字体栈
    assert "<h1>" in doc

    # 独立文件：不许引用外部资源 / 站点静态目录
    assert "/static/" not in doc
    assert "<link" not in doc
    assert not re.search(r'src="https?://', doc)


def test_article_html_renders_timestamps_quotes_tables_code(tmp_output):
    _make_episode(tmp_output, "dir-render")
    doc = export.article_html(tmp_output, "dir-render", title="渲染测试")

    assert '<span class="ts">[00:10:07]</span>' in doc
    assert '[00:10:07]' in doc                            # 文本内容保留
    # .ts 有独立样式（弱化）
    assert re.search(r"\.ts\s*\{[^}]*color:", doc)
    assert "<blockquote>" in doc
    assert "<table>" in doc and "<th>" in doc
    assert re.search(r"<pre[ >]", doc) and "<code" in doc
    # 代码块里的时间戳不动它
    code_block = doc.split("<pre", 1)[1]
    assert '<span class="ts">' not in code_block and "[00:10:07]" in code_block


def test_article_html_adds_h1_when_missing(tmp_output):
    _make_episode(tmp_output, "dir-noh1", article="只有一段正文 [00:00:05]。\n")
    doc = export.article_html(tmp_output, "dir-noh1", title="补标题")
    assert "<h1>补标题</h1>" in doc


def test_article_html_missing_article_raises(tmp_output):
    with pytest.raises(ValueError, match="不存在"):
        export.article_html(tmp_output, "没有这一集", title="x")


# ---------------------------------------------------------------- bundle


def _zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def test_bundle_zip_roundtrip_with_utf8_flags(episode, tmp_output):
    _make_episode(tmp_output, "20240102-读书电台-第二集",
                  meta={"title": "第二集", "podcast": "读书电台", "pub_date": "2024-01-02T10:00:00Z",
                        "duration": 300},
                  transcript="[00:00:01] 第二集开头\n")
    dest = tmp_output.parent / "bundle.zip"
    result = export.bundle(tmp_output, dest)

    assert result["path"] == str(dest)
    assert result["episodes"] == 2
    assert result["bytes"] == dest.stat().st_size > 0

    with zipfile.ZipFile(dest) as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        assert "README.md" in names
        assert "测试台-测试单集.md" in names
        assert "测试台-测试单集-文字稿.txt" in names
        assert "读书电台-第二集.md" in names

        # 中文条目要带 UTF-8 文件名标志（general purpose bit 11），否则 Windows 解压乱码
        utf8_names = [n for n in names if not n.isascii()]
        assert utf8_names, "测试数据里应该有中文条目"
        for info in zf.infolist():
            if not info.filename.isascii():
                assert info.flag_bits & 0x800, f"缺少 UTF-8 flag：{info.filename!r}"

        # 文章是 front matter 版本，文字稿是原文
        md = zf.read("测试台-测试单集.md").decode("utf-8")
        assert md.startswith("---\n") and '"测试单集"' in md
        assert zf.read("测试台-测试单集-文字稿.txt").decode("utf-8").startswith("[00:00:01]")

        readme = zf.read("README.md").decode("utf-8")
        assert "测试单集" in readme and "第二集" in readme
        # 目录页里的链接（百分号编码）能还原成 zip 里的真实条目
        targets = {unquote(t) for t in re.findall(r"\]\(([^)]+)\)", readme)}
        assert targets == set(names) - {"README.md"}


def test_bundle_dirs_limits_packing(episode, tmp_output):
    _make_episode(tmp_output, "20240102-读书电台-第二集",
                  meta={"title": "第二集", "podcast": "读书电台"}, transcript="[00:00:01] x\n")
    dest = tmp_output.parent / "one.zip"
    result = export.bundle(tmp_output, dest, dirs=["20240102-读书电台-第二集"])

    assert result["episodes"] == 1
    names = _zip_names(dest)
    assert "读书电台-第二集.md" in names
    assert "读书电台-第二集-文字稿.txt" in names
    assert all("测试台" not in n for n in names)
    assert "README.md" in names


def test_bundle_include_transcript_false(episode, tmp_output):
    dest = tmp_output.parent / "notranscript.zip"
    result = export.bundle(tmp_output, dest, include_transcript=False)
    names = _zip_names(dest)
    assert result["episodes"] == 1
    assert names == ["README.md", "测试台-测试单集.md"]
    assert not any("文字稿" in n for n in names)
    readme = zipfile.ZipFile(dest).read("README.md").decode("utf-8")
    assert "文字稿" not in readme


def test_bundle_skips_missing_and_articleless_dirs(tmp_output):
    _make_episode(tmp_output, "有文章", meta={"title": "有文章"}, transcript="[00:00:01] x\n")
    (tmp_output / "没文章").mkdir()                    # 只有目录
    dest = tmp_output.parent / "mixed.zip"
    result = export.bundle(tmp_output, dest, dirs=["有文章", "没文章", "根本不存在", "有文章"])
    assert result["episodes"] == 1
    assert _zip_names(dest) == ["README.md", "有文章.md", "有文章-文字稿.txt"]


def test_bundle_of_empty_root_still_writes_readme(tmp_output):
    dest = tmp_output.parent / "empty.zip"
    result = export.bundle(tmp_output, dest)
    assert result["episodes"] == 0
    assert _zip_names(dest) == ["README.md"]


# ---------------------------------------------------------------- export_episode


def test_export_episode_md(episode, tmp_output):
    name, data, mime = export.export_episode(tmp_output, episode.name, "md")
    assert name == "测试台-测试单集.md"
    assert data.decode("utf-8").startswith("---\n")
    assert mime == "text/markdown; charset=utf-8"


def test_export_episode_html(episode, tmp_output):
    name, data, mime = export.export_episode(tmp_output, episode.name, "HTML")   # 大小写不敏感
    text = data.decode("utf-8")
    assert name == "测试台-测试单集.html"
    assert text.startswith("<!DOCTYPE html>")
    assert "<title>测试单集</title>" in text           # 标题取自 meta
    assert "/static/" not in text
    assert mime == "text/html; charset=utf-8"


def test_export_episode_txt(episode, tmp_output):
    name, data, mime = export.export_episode(tmp_output, episode.name, "txt")
    assert name == "测试台-测试单集-文字稿.txt"
    assert data == (episode / "transcript.txt").read_bytes()
    assert mime == "text/plain; charset=utf-8"


def test_export_episode_txt_without_transcript_raises(tmp_output):
    _make_episode(tmp_output, "dir-notxt", meta={"title": "没有文字稿"})
    with pytest.raises(ValueError, match="文字稿"):
        export.export_episode(tmp_output, "dir-notxt", "txt")


def test_export_episode_unknown_format_and_missing_dir_raise(tmp_output):
    _make_episode(tmp_output, "dir-fmt", meta={"title": "任意"}, transcript="[00:00:01] x\n")
    with pytest.raises(ValueError, match="不支持的导出格式"):
        export.export_episode(tmp_output, "dir-fmt", "pdf")
    with pytest.raises(ValueError, match="不存在"):
        export.export_episode(tmp_output, "没有这一集", "md")
