"""本地 Web 界面：填链接 → 实时日志 → 展示文章。

启动：uv run python webapp.py  （默认 http://127.0.0.1:8787）
"""
from __future__ import annotations

import json
import os
import shlex
import threading
import time
import uuid
from pathlib import Path

import markdown
from flask import Flask, Response, jsonify, request, send_from_directory

from podcast_article import mcp_client, mcp_config, notion
from podcast_article import publish as publish_mod
from podcast_article import settings as settings_mod
from podcast_article.config import PROJECT_ROOT
from podcast_article.pipeline import Pipeline

OUTPUT_ROOT = PROJECT_ROOT / "output"
WEB_DIR = PROJECT_ROOT / "web"

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")

_JOBS: dict[str, dict] = {}          # job_id -> 状态字典
_JOBS_LOCK = threading.Lock()
_ALLOWED_FILES = {"article.md", "transcript.txt", "transcript.json", "meta.json"}


def _new_job(url: str, opts: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "url": url,
        "status": "running",       # running / done / error
        "logs": [],
        "progress": None,          # {"stage": download/asr/llm, ...}
        "article_html": None,
        "meta": None,
        "workdir": None,
        "error": None,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def log(msg: str) -> None:
        job["logs"].append(msg)

    def progress(stage: str, data: dict) -> None:
        job["progress"] = {**data, "stage": stage}

    def worker() -> None:
        try:
            # 请求没有显式指定时，落到设置页里的「生成默认值」
            defaults = settings_mod.load()["generation"]
            lang = opts.get("lang") or defaults.get("language") or "auto"
            pipe = Pipeline(
                url=url,
                output_dir=OUTPUT_ROOT,
                language=lang,
                backend=opts.get("backend") or defaults.get("backend") or "auto",
                asr_model=(opts.get("model") or defaults.get("asr_model") or None),
                no_subs=bool(opts.get("no_subs", defaults.get("no_subs"))),
                force_transcript=bool(opts.get("force_transcript")),
                force_article=bool(opts.get("force_article")),
                pick=int(opts.get("pick", 1)),
                max_chars=int(opts.get("max_chars") or defaults.get("max_chars") or 75_000),
                mode=opts.get("mode") or defaults.get("length_mode") or "standard",
                polish=bool(opts.get("polish", defaults.get("auto_polish", True))),
                log=log,
                progress=progress,
            )
            article_path = pipe.run()
            workdir = article_path.parent
            job["workdir"] = workdir.name
            job["article_html"] = _md_to_html(
                (workdir / "article.md").read_text(encoding="utf-8")
            )
            meta_file = workdir / "meta.json"
            if meta_file.exists():
                job["meta"] = json.loads(meta_file.read_text(encoding="utf-8"))
            job["status"] = "done"
            log("[done] 完成 ✔")
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            job["logs"].append(f"[error] {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def _md_to_html(text: str) -> str:
    return markdown.markdown(
        text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"]
    )


def _running_job_id() -> str | None:
    with _JOBS_LOCK:
        for job_id, job in _JOBS.items():
            if job["status"] == "running":
                return job_id
    return None


# ---------------------------------------------------------------- API

@app.post("/api/run")
def api_run():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "请填写链接"}), 400
    if _running_job_id():
        return jsonify({"error": "已有任务在运行，请等它完成"}), 409
    job_id = _new_job(url, data)
    return jsonify({"job_id": job_id})


@app.get("/api/job/<job_id>")
def api_job(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(
        {
            "id": job["id"],
            "status": job["status"],
            "logs": job["logs"],
            "progress": job["progress"],
            "article_html": job["article_html"],
            "meta": job["meta"],
            "workdir": job["workdir"],
            "error": job["error"],
        }
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/stream/<job_id>")
def api_stream(job_id: str):
    """SSE 实时推送：log / progress / status 三类事件。"""
    def gen():
        last_log = 0
        last_prog = None
        while True:
            job = _JOBS.get(job_id)
            if not job:
                yield _sse("status", {"status": "error", "error": "任务不存在"})
                return
            logs = job["logs"]
            while last_log < len(logs):
                yield _sse("log", {"line": logs[last_log]})
                last_log += 1
            prog = job["progress"]
            if prog and prog != last_prog:
                last_prog = prog
                yield _sse("progress", prog)
            if job["status"] != "running":
                yield _sse(
                    "status",
                    {
                        "status": job["status"],
                        "error": job["error"],
                        "workdir": job["workdir"],
                        "meta": job["meta"],
                        "article_html": job["article_html"],
                    },
                )
                return
            time.sleep(0.3)

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/notion")
def api_notion():
    """把某一集的文章写入内置 Notion 集成。body: {"dir": "<输出目录名>"}"""
    return _publish_with({"target": "builtin"})


def _episode_ctx(dir_name: str):
    """读取一集的文章与元信息，组装成发布上下文。返回 (ctx, error_response)。"""
    base = (OUTPUT_ROOT / (dir_name or "")).resolve()
    if not dir_name or not base.is_dir() or OUTPUT_ROOT.resolve() not in base.parents:
        return None, (jsonify({"error": "目录不存在"}), 404)
    article = base / "article.md"
    if not article.is_file():
        return None, (jsonify({"error": "该单集还没有生成文章"}), 400)
    try:
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        meta = {}
    content = article.read_text(encoding="utf-8")
    ctx = {
        "title": f"{meta.get('podcast', '')}｜{meta.get('title', '')}".strip("｜") or dir_name,
        "podcast": meta.get("podcast") or "",
        "date": meta.get("pub_date") or "",
        "duration": meta.get("duration"),
        "url": meta.get("url") or "",
        "content": content,
        "blocks": notion.markdown_to_blocks(content),
    }
    return ctx, None


def _publish_with(extra: dict | None = None):
    """把文章发布到指定目标：内置 Notion，或 mcp:<服务器>:<工具>。"""
    data = {**(request.get_json(force=True, silent=True) or {}), **(extra or {})}
    ctx, err = _episode_ctx((data.get("dir") or "").strip())
    if err:
        return err
    target = (data.get("target") or "builtin").strip()
    template = data.get("template")
    if isinstance(template, str):
        template = json.loads(template) if template.strip() else None
    try:
        result = publish_mod.publish(ctx, target=target, template=template)
    except json.JSONDecodeError as exc:
        return jsonify({"error": f"参数模板不是合法 JSON：{exc}"}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/publish")
def api_publish():
    """发布文章。body: {"dir", "target": "builtin"|"mcp:<server>:<tool>", "template": {...}}"""
    return _publish_with()


@app.get("/api/settings")
def api_settings_get():
    """设置页数据：个人资料、生成默认值、密钥状态（打码）、存储信息。"""
    return jsonify({
        "profile": settings_mod.load()["profile"],
        "generation": settings_mod.load()["generation"],
        "secrets": settings_mod.secret_status(),
        "storage": settings_mod.storage_info(),
    })


@app.post("/api/settings")
def api_settings_post():
    """保存设置。body: {profile, generation, secrets}

    secrets 里空字符串表示保持原值（密钥不会被误清空），非空则写入 .env。
    """
    data = request.get_json(force=True, silent=True) or {}
    saved = settings_mod.save(profile=data.get("profile"), generation=data.get("generation"))
    changed = settings_mod.update_env(data.get("secrets") or {})
    if changed:
        # 让当前进程立即用上新值
        for key in changed:
            os.environ[key] = settings_mod.read_env().get(key, os.environ.get(key, ""))
    return jsonify({
        "profile": saved["profile"],
        "generation": saved["generation"],
        "secrets": settings_mod.secret_status(),
        "env_changed": changed,
    })


@app.post("/api/settings/verify")
def api_settings_verify():
    """实测当前配置是否可用。body: {"what": "deepseek"|"notion"}"""
    what = (request.get_json(force=True, silent=True) or {}).get("what", "")
    if what == "deepseek":
        from openai import OpenAI

        from podcast_article import config

        try:
            client = OpenAI(
                api_key=config.deepseek_api_key(), base_url=config.DEEPSEEK_BASE_URL
            )
            models = [m.id for m in client.models.list().data]
        except Exception as exc:
            return jsonify({"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:300]})
        return jsonify({"ok": True, "detail": "可用模型：" + "、".join(models[:8])})

    if what == "notion":
        try:
            resp = notion._session().get(
                f"{notion.API}/users/me", headers=notion._headers(), timeout=20
            )
        except Exception as exc:
            return jsonify({"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:300]})
        if resp.status_code != 200:
            return jsonify({"ok": False, "detail": f"HTTP {resp.status_code}：{resp.text[:200]}"})
        body = resp.json()
        ws = (body.get("bot") or {}).get("workspace_name", "")
        return jsonify({"ok": True, "detail": f"已连接 {ws}（integration：{body.get('name')}）"})

    return jsonify({"error": f"未知的检查项：{what}"}), 400


@app.get("/api/mcp/servers")
def api_mcp_servers():
    """已配置的 MCP 服务器（密钥打码）+ 一键添加的预设 + 前端默认值。"""
    from podcast_article import config

    return jsonify({
        "servers": [mcp_config.mask_entry(s) for s in mcp_config.load_servers()],
        "presets": mcp_config.preset_catalog(),
        "defaults": {
            "notion_database_id": config.notion_database_id(),
            "notion_parent_page_id": config.notion_parent_page_id(),
        },
    })


@app.post("/api/mcp/servers/preset")
def api_mcp_preset():
    """按预设一键添加。body: {"preset": "notion", "secret": "ntn_…"（可选）}"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        entry = mcp_config.build_from_preset(data.get("preset", ""), data.get("secret"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"server": mcp_config.mask_entry(entry)})


@app.post("/api/mcp/servers")
def api_mcp_save():
    """新增或更新一台 MCP 服务器。命令可整行传 command_line，env 支持 dict 或 "K=V\\nK2=V2"。"""
    data = request.get_json(force=True, silent=True) or {}
    args = data.get("args") or []
    if isinstance(args, str):
        args = shlex.split(args)
    env = data.get("env") or {}
    if isinstance(env, str):
        env = {}
        for line in data.get("env", "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    elif isinstance(env, dict):
        # 前端回显的密钥是打码值（含 …），别用它覆盖真实密钥
        env = {k: v for k, v in env.items() if "…" not in str(v)}
    try:
        entry = mcp_config.upsert_server({
            "name": data.get("name"), "command_line": data.get("command_line"),
            "command": data.get("command"), "args": args, "env": env,
            "note": data.get("note", ""),
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"server": mcp_config.mask_entry(entry)})


@app.delete("/api/mcp/servers/<name>")
def api_mcp_delete(name: str):
    if not mcp_config.delete_server(name):
        return jsonify({"error": f"未找到服务器：{name}"}), 404
    return jsonify({"ok": True})


def _resolve_entry(data: dict) -> dict | None:
    """优先用已保存的服务器；否则用请求里直接给的 command/args/env（用于「先测再存」）。"""
    name = (data.get("name") or "").strip()
    if name and not data.get("command"):
        entry = mcp_config.get_server(name)
        if not entry:
            raise ValueError(f"未找到服务器：{name}")
        return entry
    if data.get("command"):
        args = data.get("args") or []
        if isinstance(args, str):
            args = shlex.split(args)
        env = data.get("env") or {}
        if isinstance(env, str):
            env = dict(
                (line.split("=", 1)[0].strip(), line.split("=", 1)[1].strip())
                for line in env.splitlines() if "=" in line
            )
        return {"name": name or "(未保存)", "command": data["command"], "args": args, "env": env}
    raise ValueError("请提供服务器名称或启动命令")


@app.post("/api/mcp/test")
def api_mcp_test():
    """启动服务器并列出工具。body: {"name": "..."} 或 {"command","args","env"}"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        entry = _resolve_entry(data)
        result = mcp_client.list_tools(
            command=entry["command"], args=entry.get("args"),
            env=mcp_config.resolved_env(entry),
        )
    except (ValueError, mcp_client.MCPClientError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/mcp/call")
def api_mcp_call():
    """试调用某个工具。body: {"name", "tool", "arguments"}"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        entry = _resolve_entry(data)
        tool = (data.get("tool") or "").strip()
        if not tool:
            raise ValueError("缺少工具名")
        result = mcp_client.call_tool(
            command=entry["command"], args=entry.get("args"),
            env=mcp_config.resolved_env(entry), tool=tool,
            arguments=data.get("arguments") or {},
        )
    except (ValueError, mcp_client.MCPClientError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.get("/api/library")
def api_library():
    """列出 output/ 下所有已生成的单集（按时间倒序）。"""
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
                    "title": meta.get("title") or d.name,
                    "podcast": meta.get("podcast") or "",
                    "duration": meta.get("duration"),
                    "has_article": (d / "article.md").exists(),
                }
            )
    return jsonify({"items": items})


@app.get("/api/file/<job_dir>/<name>")
def api_file(job_dir: str, name: str):
    """安全地取输出目录里的文件（白名单）。"""
    if name not in _ALLOWED_FILES:
        return jsonify({"error": "不支持的文件"}), 400
    base = (OUTPUT_ROOT / job_dir).resolve()
    if not base.is_dir() or OUTPUT_ROOT.resolve() not in base.parents:
        return jsonify({"error": "目录不存在"}), 404
    target = base / name
    if not target.exists():
        return jsonify({"error": "文件不存在"}), 404
    text = target.read_text(encoding="utf-8")
    if name == "article.md":
        return jsonify({"html": _md_to_html(text)})
    if name.endswith(".json"):
        return jsonify(json.loads(text))
    return app.response_class(text, mimetype="text/plain")


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


if __name__ == "__main__":
    OUTPUT_ROOT.mkdir(exist_ok=True)
    print("► podcast-article web: http://127.0.0.1:8787")
    app.run(host="127.0.0.1", port=8787, threaded=True)
