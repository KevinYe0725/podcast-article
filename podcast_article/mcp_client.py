"""以 MCP 客户端身份临时启动一个 stdio MCP 服务器并与之交互。

Web 界面用它来「测试连接 / 列出工具 / 试调用」，发布时也走这里。
"""
from __future__ import annotations

import asyncio
import tempfile
from typing import Any, Awaitable, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPClientError(RuntimeError):
    pass


async def _with_session(
    action: Callable[[ClientSession, Any], Awaitable[Any]],
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    init_timeout: float = 30.0,
) -> Any:
    # errlog 必须是真实文件（SDK 会取 fileno 作为子进程 stderr）
    errfile = tempfile.TemporaryFile(mode="w+", encoding="utf-8")

    def err_tail() -> str:
        try:
            errfile.seek(0)
            return errfile.read().strip()[-300:].replace("\n", " ")
        except Exception:
            return ""

    params = StdioServerParameters(
        command=command, args=list(args or []), env=env or None, cwd=cwd
    )
    try:
        async with stdio_client(params, errlog=errfile) as (read, write):
            async with ClientSession(read, write) as session:
                init = await asyncio.wait_for(session.initialize(), init_timeout)
                return await action(session, init)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise MCPClientError(
            f"启动超时（{init_timeout:.0f}s）：{command} —— 命令是否存在、能否联网？"
        ) from exc
    except Exception as exc:
        detail = err_tail()
        raise MCPClientError(
            f"{type(exc).__name__}: {exc}" + (f" ｜ 服务器输出: {detail}" if detail else "")
        ) from exc
    finally:
        errfile.close()


def list_tools(
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    timeout: float = 60.0,
) -> dict:
    """启动服务器并列出工具。返回 {server, tools}。"""
    async def action(session: ClientSession, init: Any) -> dict:
        info = getattr(init, "serverInfo", None)
        name = getattr(info, "name", "") or ""
        version = getattr(info, "version", "") or ""
        tools = await asyncio.wait_for(session.list_tools(), timeout)
        return {
            "server": f"{name} {version}".strip(),
            "tools": [
                {
                    "name": t.name,
                    "description": (t.description or "").strip()[:400],
                    "schema": t.inputSchema or {},
                }
                for t in tools.tools
            ],
        }

    return asyncio.run(_with_session(action, command, args, env, cwd))


def call_tool(
    command: str,
    tool: str,
    arguments: dict | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    timeout: float = 180.0,
) -> dict:
    """调用某个工具，返回 {ok, text}。"""
    async def action(session: ClientSession, init: Any) -> dict:
        result = await asyncio.wait_for(
            session.call_tool(tool, arguments=arguments or {}), timeout
        )
        parts: list[str] = []
        for block in result.content or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
            elif getattr(block, "type", "") == "image":
                parts.append("[图片结果]")
        text = "\n".join(parts).strip() or "(无文本返回)"
        return {"ok": not result.isError, "text": text}

    return asyncio.run(_with_session(action, command, args, env, cwd))
