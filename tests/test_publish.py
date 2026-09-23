"""发布模板渲染与链接提取（MCP 发布的核心逻辑）。"""
import pytest

from podcast_article import publish
from podcast_article import notion


CTX = {
    "title": "标题｜单集", "podcast": "测试台", "date": "2024-01-01T00:00:00Z",
    "duration": 3661, "url": "https://example.com/e1",
    "content": "# 标题\n\n正文", "blocks": [{"object": "block", "type": "paragraph"}],
}


def test_placeholder_string_interpolation():
    out = publish.render_args(
        {"properties": {"标题": {"title": [{"text": {"content": "{{title}}"}}]}}}, CTX
    )
    assert out["properties"]["标题"]["title"][0]["text"]["content"] == "标题｜单集"


def test_inline_placeholders_inside_text():
    out = publish.render_args({"text": "来自 {{podcast}} 的《{{title}}》"}, CTX)
    assert out["text"] == "来自 测试台 的《标题｜单集》"


def test_blocks_placeholder_injects_json_array():
    out = publish.render_args({"children": "{{blocks}}"}, CTX)
    assert out["children"] == CTX["blocks"]


def test_blocks_placeholder_accepts_json_string():
    out = publish.render_args({"children": "{{blocks}}"}, {**CTX, "blocks": '[{"type":"x"}]'})
    assert out["children"] == [{"type": "x"}]


def test_nested_structures_are_walked():
    tpl = {"a": [{"b": {"c": "{{url}}"}}], "d": ["{{duration}}", "常量"]}
    out = publish.render_args(tpl, CTX)
    assert out["a"][0]["b"]["c"] == "https://example.com/e1"
    assert out["d"] == ["3661", "常量"]


def test_missing_placeholder_becomes_empty_string():
    out = publish.render_args({"x": "{{nope}}"}, CTX)
    assert out["x"] == ""


def test_extract_url_from_notion_result():
    text = '{"object":"page","url":"https://app.notion.com/p/x-3db8c3cda3c681288994d32075f52713"}'
    assert publish._extract_url(text).endswith("75f52713")
    assert publish._extract_url("没有链接") is None


def test_publish_unknown_target_raises():
    with pytest.raises(ValueError, match="未知的发布目标"):
        publish.publish(CTX, target="slack:channel")


def test_publish_mcp_target_format_error():
    with pytest.raises(ValueError, match="mcp:<服务器名>:<工具名>"):
        publish.publish(CTX, target="mcp:只有两段")


def test_publish_mcp_unknown_server():
    with pytest.raises(ValueError, match="未找到 MCP 服务器"):
        publish.publish(CTX, target="mcp:不存在:tool")


def test_notion_headers_accept_explicit_account_token_without_environment_lookup():
    headers = notion._headers("account-specific-token")
    assert headers["Authorization"] == "Bearer account-specific-token"


def test_notion_rejects_empty_explicit_account_token():
    from podcast_article.notion import NotionError

    with pytest.raises(NotionError, match="当前账号"):
        notion._headers("")
