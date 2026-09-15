"""本地 Web 界面：填链接 → 实时日志 → 展示文章。

启动：uv run python webapp.py  （默认 http://127.0.0.1:8787）
"""
from __future__ import annotations

import json
import os
import shlex
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import markdown
from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from podcast_article import library as library_mod
from podcast_article import mcp_client, mcp_config, notion
from podcast_article import publish as publish_mod
from podcast_article import settings as settings_mod
from podcast_article import usage as usage_mod
from podcast_article import export as export_mod
from podcast_article import feeds as feeds_mod
from podcast_article import queue as queue_mod
from podcast_article import search as search_mod
from podcast_article.config import PROJECT_ROOT
from podcast_article.pipeline import Pipeline

# 输出根目录，可用 PA_OUTPUT_DIR 覆盖（测试用独立目录，避免碰真实数据）
OUTPUT_ROOT = Path(os.environ.get("PA_OUTPUT_DIR") or (PROJECT_ROOT / "output"))
WEB_DIR = PROJECT_ROOT / "web"

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")

_JOBS: dict[str, dict] = {}          # job_id -> 状态字典
_JOBS_LOCK = threading.Lock()
_ALLOWED_FILES = {"article.md", "transcript.txt", "transcript.json", "meta.json",
                  "usage.json", "outline.json"}


def _new_job(url: str, opts: dict, *, source: str = "manual",
             queue_id: str | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "url": url,
        "source": source,          # manual（首页直接提交）/ queue / feed:<id> / lib（重新生成）
        "queue_id": queue_id,
        "status": "running",       # running / done / error
        "logs": [],
        "progress": None,          # {"stage": download/asr/llm, ...}
        "usage": None,             # 实时累计的 token / 费用
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

    def on_usage(snapshot: dict) -> None:
        job["usage"] = snapshot

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
                llm_model=(opts.get("llm_model") or defaults.get("llm_model") or None),
                no_subs=bool(opts.get("no_subs", defaults.get("no_subs"))),
                force_transcript=bool(opts.get("force_transcript")),
                force_article=bool(opts.get("force_article")),
                pick=int(opts.get("pick", 1)),
                max_chars=int(opts.get("max_chars") or defaults.get("max_chars") or 75_000),
                mode=opts.get("mode") or defaults.get("length_mode") or "standard",
                polish=bool(opts.get("polish", defaults.get("auto_polish", False))),
                outlined=bool(opts.get("outlined", defaults.get("outline_mode", True))),
                log=log,
                progress=progress,
                on_usage=on_usage,
                episode=opts.get("episode") or None,
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
            _after_done(workdir.name, opts, log)
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            job["logs"].append(f"[error] {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def _after_done(dir_name: str, opts: dict, log) -> None:
    """任务成功后的小动作：订阅自动归类。失败不影响主流程。"""
    try:
        dest = (opts or {}).get("auto_dest")
        if dest:
            library_mod.assign(dir_name, dest)
            log(f"[done] 已归入分类：{dest}")
    except Exception as exc:                 # 归类失败不该让「文章已生成」变成失败
        log(f"[done] 自动归类失败：{exc}")


def _article_preview(path: Path, limit: int = 3) -> dict:
    """从成稿里抽「一句话引语 + 前几条带走清单」，做历史库预览卡。

    不额外调用模型：这些都是文章里已有的内容，直接解析即可。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    deck = ""
    for line in text.splitlines():
        if line.startswith("> "):
            deck = line[2:].strip()
            break
    takeaways: list[str] = []
    in_take = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            in_take = "带走" in s
            continue
        if in_take and s.startswith(("- ", "* ")):
            takeaways.append(s[2:].strip())
    headings = [l.strip("# ").strip() for l in text.splitlines() if l.startswith("## ")]
    return {
        "deck": deck,
        "takeaways": takeaways[:limit],
        "takeaway_total": len(takeaways),
        "sections": [h for h in headings if "带走" not in h and "点评" not in h][:6],
        "chars": len(text),
    }


_TS_RE = re.compile(r"\[(\d{1,2}):(\d{2}):(\d{2})\]")
_AUDIO_NAMES = ("audio.m4a", "audio.mp3", "audio.webm", "audio.wav", "audio.m4a")


def _audio_path(base: Path) -> Path | None:
    for name in _AUDIO_NAMES:
        p = base / name
        if p.is_file():
            return p
    return None


def _linkify_timestamps(html: str) -> str:
    """把正文里的 [时:分:秒] 变成可点元素，点了跳到音频对应位置。"""

    def repl(m: re.Match) -> str:
        sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        return (f'<a class="ts" data-sec="{sec}" title="跳到音频此处" '
                f'href="#">{m.group(0)}</a>')

    return _TS_RE.sub(repl, html)


def _md_to_html(text: str) -> str:
    html = markdown.markdown(
        text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"]
    )
    return _linkify_timestamps(html)


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
    busy = _running_job_id()
    if busy:
        # 明确告诉前端「在跑什么」，否则用户只会觉得按钮坏了
        with _JOBS_LOCK:
            current = dict(_JOBS.get(busy) or {})
        return jsonify({
            "error": f"已有任务在运行：{current.get('url', '')}",
            "busy": True, "job_id": busy, "url": current.get("url", ""),
            "source": current.get("source", ""),
        }), 409
    job_id = _new_job(url, data)
    return jsonify({"job_id": job_id})


@app.get("/api/jobs/current")
def api_jobs_current():
    """当前正在运行的任务（供界面提示与自动化测试等待）。"""
    with _JOBS_LOCK:
        for job in _JOBS.values():
            if job["status"] == "running":
                return jsonify({"job_id": job["id"], "url": job["url"], "status": "running"})
    return jsonify({"job_id": None, "status": "idle"})


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
            "usage": job["usage"],
            "article_html": job["article_html"],
            "meta": job["meta"],
            "workdir": job["workdir"],
            "source": job["source"],
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
        last_usage = None
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
            if job["usage"] and job["usage"] != last_usage:
                last_usage = job["usage"]
                yield _sse("usage", job["usage"])
            if job["status"] != "running":
                yield _sse(
                    "status",
                    {
                        "status": job["status"],
                        "error": job["error"],
                        "workdir": job["workdir"],
                        "meta": job["meta"],
                        "usage": job["usage"],
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
    """设置页数据：个人资料、生成默认值、订阅调度、密钥状态（打码）、存储信息。"""
    current = settings_mod.load()
    return jsonify({
        "profile": current["profile"],
        "generation": current["generation"],
        "subscriptions": current["subscriptions"],
        "secrets": settings_mod.secret_status(),
        "storage": settings_mod.storage_info(),
    })


@app.post("/api/settings")
def api_settings_post():
    """保存设置。body: {profile, generation, subscriptions, secrets}

    secrets 里空字符串表示保持原值（密钥不会被误清空），非空则写入 .env。
    """
    data = request.get_json(force=True, silent=True) or {}
    saved = settings_mod.save(profile=data.get("profile"),
                              generation=data.get("generation"),
                              subscriptions=data.get("subscriptions"))
    changed = settings_mod.update_env(data.get("secrets") or {})
    if changed:
        # 让当前进程立即用上新值
        for key in changed:
            os.environ[key] = settings_mod.read_env().get(key, os.environ.get(key, ""))
    return jsonify({
        "profile": saved["profile"],
        "generation": saved["generation"],
        "subscriptions": saved["subscriptions"],
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
    """列出 output/ 下所有已生成的单集（按时间倒序）+ 分类归属 + 阅读状态 + 花费。"""
    items = _library_items()
    cat_state = library_mod.snapshot()
    totals = usage_mod.summary_over(OUTPUT_ROOT)
    return jsonify({
        "items": items,
        "categories": cat_state["categories"],
        "assignments": cat_state["assignments"],
        "status": cat_state["status"],
        "status_counts": cat_state["status_counts"],
        "status_labels": cat_state["status_labels"],
        "usage_total": totals,
        "search_stats": search_mod.stats(OUTPUT_ROOT),
        "queue_active": queue_mod.snapshot()["active"],
    })


@app.get("/api/categories")
def api_categories():
    """分类列表（含各自文章数）与文章归属。"""
    return jsonify(library_mod.snapshot())


@app.post("/api/categories")
def api_category_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        cat = library_mod.create(data.get("name", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"category": cat})


@app.patch("/api/categories/<cid>")
def api_category_rename(cid: str):
    data = request.get_json(force=True, silent=True) or {}
    try:
        cat = library_mod.rename(cid, data.get("name", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"category": cat})


@app.delete("/api/categories/<cid>")
def api_category_delete(cid: str):
    try:
        library_mod.delete(cid)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"ok": True})


@app.post("/api/assign")
def api_assign():
    """把文章放进分类（category_id 为空 → 移出分类）。body: {"dir", "category_id"}"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        library_mod.assign(data.get("dir", ""), (data.get("category_id") or "").strip() or None)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(library_mod.snapshot())


@app.delete("/api/episode/<job_dir>")
def api_episode_delete(job_dir: str):
    """删除一条历史记录。

    body: {"scope": "all"}     → 删整个输出目录（文章 + 音频 + 文字稿）
          {"scope": "article"} → 只删 article.md，保留音频与文字稿以便重新生成
    """
    data = request.get_json(force=True, silent=True) or {}
    scope = data.get("scope", "all")
    base = (OUTPUT_ROOT / job_dir).resolve()
    if not job_dir or not base.is_dir() or OUTPUT_ROOT.resolve() not in base.parents:
        return jsonify({"error": "目录不存在"}), 404

    freed = 0
    try:
        if scope == "article":
            target = base / "article.md"
            if not target.is_file():
                return jsonify({"error": "这条记录没有文章"}), 400
            freed = target.stat().st_size
            target.unlink()
        else:
            freed = sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
            shutil.rmtree(base)
            library_mod.forget(job_dir)
    except OSError as exc:
        return jsonify({"error": f"删除失败：{exc}"}), 500
    return jsonify({"ok": True, "scope": scope, "freed": freed, "dir": job_dir})


@app.get("/api/audio/<job_dir>")
def api_audio(job_dir: str):
    """本地音频流。conditional=True 让 werkzeug 处理 Range 请求，200MB 的文件也能拖动进度条。"""
    base = (OUTPUT_ROOT / job_dir).resolve()
    if not job_dir or not base.is_dir() or OUTPUT_ROOT.resolve() not in base.parents:
        return jsonify({"error": "目录不存在"}), 404
    path = _audio_path(base)
    if not path:
        return jsonify({"error": "这一集没有本地音频"}), 404
    return send_file(path, conditional=True, mimetype="audio/mp4")


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


def _library_items() -> list[dict]:
    """扫一遍输出目录，拼出历史库列表（按修改时间倒序）。"""
    items: list[dict] = []
    if not OUTPUT_ROOT.exists():
        return items
    for d in sorted(OUTPUT_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        meta_file = d / "meta.json"
        if not d.is_dir() or not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        item = {
            "dir": d.name,
            "title": meta.get("title") or d.name,
            "podcast": meta.get("podcast") or "",
            "duration": meta.get("duration"),
            "pub_date": meta.get("pub_date"),
            "url": meta.get("url") or "",
            "has_article": (d / "article.md").exists(),
            "has_transcript": (d / "transcript.txt").exists(),
            "has_audio": _audio_path(d) is not None,
        }
        try:
            item["size"] = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        except OSError:
            item["size"] = 0
        if item["has_article"]:
            item["preview"] = _article_preview(d / "article.md")
        episode_usage = usage_mod.load(d)
        if episode_usage:
            item["usage"] = usage_mod.describe(episode_usage)
        items.append(item)
    return items


# ---------------------------------------------------------------- 全文检索

@app.get("/api/search")
def api_search():
    """跨文章与文字稿检索。GET /api/search?q=关键词&limit=30"""
    query = (request.args.get("q") or "").strip()
    try:
        limit = min(100, max(1, int(request.args.get("limit") or 30)))
    except ValueError:
        limit = 30
    if not query:
        return jsonify({"query": "", "count": 0, "items": [],
                        "stats": search_mod.stats(OUTPUT_ROOT)})
    items = search_mod.search(OUTPUT_ROOT, query, limit=limit)
    state = library_mod.snapshot()
    for it in items:
        it["category"] = state["assignments"].get(it["dir"])
        it["status"] = state["status"].get(it["dir"], library_mod.DEFAULT_STATUS)
    return jsonify({"query": query, "count": len(items), "items": items,
                    "stats": search_mod.stats(OUTPUT_ROOT)})


# ---------------------------------------------------------------- 阅读状态

@app.post("/api/status")
def api_status():
    """设置阅读状态。body: {"dir": "...", "status": "unread|reading|read|later"}（空 = 清除）"""
    data = request.get_json(force=True, silent=True) or {}
    dir_name = (data.get("dir") or "").strip()
    try:
        value = library_mod.set_status(dir_name, (data.get("status") or "").strip() or None)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"dir": dir_name, "value": value, "state": library_mod.snapshot()})


# ---------------------------------------------------------------- 用量与费用

@app.get("/api/usage")
def api_usage():
    """累计 token 与费用（扫一遍所有 usage.json）+ 当前任务的实时用量。"""
    live = usage_mod.current()
    return jsonify({
        "total": usage_mod.summary_over(OUTPUT_ROOT),
        "live": usage_mod.describe(live.usage) if live else None,
        "prices": usage_mod.MODEL_PRICES,
        "busy": _running_job_id() is not None,
    })


# ---------------------------------------------------------------- 导出

def _safe_dir(dir_name: str) -> Path | None:
    """把目录名解析成输出根目录下的真实目录；越界或不存在返回 None。"""
    base = (OUTPUT_ROOT / (dir_name or "")).resolve()
    if not dir_name or not base.is_dir() or OUTPUT_ROOT.resolve() not in base.parents:
        return None
    return base


def _attachment(name: str) -> dict:
    """中文文件名要用 RFC 5987 编码，否则部分浏览器会存成乱码。"""
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}


@app.get("/api/export/<job_dir>")
def api_export_episode(job_dir: str):
    """导出单篇。GET /api/export/<dir>?fmt=md|html|txt"""
    fmt = (request.args.get("fmt") or "md").lower()
    if fmt not in ("md", "html", "txt"):
        return jsonify({"error": "只支持 md / html / txt"}), 400
    if not _safe_dir(job_dir):
        return jsonify({"error": "目录不存在"}), 404
    try:
        name, payload, mime = export_mod.export_episode(OUTPUT_ROOT, job_dir, fmt)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return Response(payload, mimetype=mime, headers=_attachment(name))


@app.get("/api/export")
def api_export_all():
    """整库导出 zip。GET /api/export?category=<id>&transcript=1"""
    category = (request.args.get("category") or "").strip()
    include_transcript = request.args.get("transcript", "1") != "0"
    dirs = []
    state = library_mod.snapshot()
    for item in _library_items():
        if not item["has_article"]:
            continue
        if category and state["assignments"].get(item["dir"]) != category:
            continue
        dirs.append(item["dir"])
    if not dirs:
        return jsonify({"error": "没有可导出的文章"}), 400
    fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        info = export_mod.bundle(OUTPUT_ROOT, tmp, dirs=dirs,
                                 include_transcript=include_transcript)
        payload = tmp.read_bytes()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        tmp.unlink(missing_ok=True)
    stamp = time.strftime("%Y%m%d")
    return Response(payload, mimetype="application/zip",
                    headers=_attachment(f"podcast-articles-{stamp}.zip")
                    | {"X-Episodes": str(info["episodes"])})


# ---------------------------------------------------------------- 批量队列

@app.get("/api/queue")
def api_queue_get():
    return jsonify(queue_mod.snapshot())


@app.post("/api/queue")
def api_queue_post():
    """入队。body: {"urls": "一行一条" 或 ["...", ...], "opts": {...}, "pick": 1}"""
    data = request.get_json(force=True, silent=True) or {}
    raw = data.get("urls") or data.get("url") or ""
    opts = data.get("opts") if isinstance(data.get("opts"), dict) else {}
    # 没有显式给选项时，把首页当前的选项快照进去（避免之后改了默认值导致理解偏差）
    if not opts:
        for key in ("mode", "lang", "model", "no_subs"):
            if data.get(key) is not None:
                opts[key] = data[key]
    try:
        added = queue_mod.add(raw, opts=opts, pick=int(data.get("pick") or 1),
                              source="manual")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"added": added, "queue": queue_mod.snapshot()})


@app.delete("/api/queue/<item_id>")
def api_queue_delete(item_id: str):
    if not queue_mod.remove(item_id):
        return jsonify({"error": "队列里没有这一条"}), 404
    return jsonify(queue_mod.snapshot())


@app.post("/api/queue/<item_id>/move")
def api_queue_move(item_id: str):
    data = request.get_json(force=True, silent=True) or {}
    try:
        pos = queue_mod.move(item_id, int(data.get("delta", -1)))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"position": pos, "queue": queue_mod.snapshot()})


@app.post("/api/queue/clear")
def api_queue_clear():
    data = request.get_json(force=True, silent=True) or {}
    removed = queue_mod.clear(keep_failed=bool(data.get("keep_failed")))
    return jsonify({"removed": removed, "queue": queue_mod.snapshot()})


@app.post("/api/queue/retry")
def api_queue_retry():
    n = queue_mod.retry_failed()
    return jsonify({"retried": n, "queue": queue_mod.snapshot()})


@app.post("/api/queue/run")
def api_queue_run():
    """立刻跑一条排队中的任务（后台会自动接着跑剩下的）。"""
    job_id = run_queue_once()
    if not job_id:
        busy = _running_job_id()
        return jsonify({"error": "当前有任务在跑" if busy else "队列里没有待跑的链接",
                        "busy": bool(busy)}), 409
    return jsonify({"job_id": job_id, "queue": queue_mod.snapshot()})


# ---------------------------------------------------------------- 订阅

@app.get("/api/feeds")
def api_feeds_get():
    snap = feeds_mod.snapshot()
    snap["subscriptions"] = settings_mod.load()["subscriptions"]
    return jsonify(snap)


@app.post("/api/feeds")
def api_feeds_add():
    """订阅一个 feed。body: {"url", "auto": true, "backfill": 0}"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        entry = feeds_mod.add((data.get("url") or "").strip(),
                              auto=bool(data.get("auto", True)),
                              backfill=int(data.get("backfill") or 0))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                       # 网络类异常也要给用户一句人话
        return jsonify({"error": f"订阅失败：{type(exc).__name__}: {exc}"[:300]}), 400
    # 首次订阅若要求补跑，立刻把 backfill 的那几集入队
    found = feeds_mod.check(entry["id"]) if entry.get("backfill") else []
    added = _enqueue_feed_episodes(found) if found else []
    return jsonify({"feed": entry, "enqueued": len(added),
                    "feeds": feeds_mod.snapshot()})


@app.post("/api/feeds/discover")
def api_feeds_discover():
    """订阅前预览 feed。body: {"url"}"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(feeds_mod.discover((data.get("url") or "").strip()))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"读取失败：{type(exc).__name__}: {exc}"[:300]}), 400


@app.patch("/api/feeds/<fid>")
def api_feeds_update(fid: str):
    data = request.get_json(force=True, silent=True) or {}
    try:
        entry = feeds_mod.update(fid, **{k: v for k, v in data.items() if k != "id"})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"feed": entry, "feeds": feeds_mod.snapshot()})


@app.delete("/api/feeds/<fid>")
def api_feeds_delete(fid: str):
    try:
        feeds_mod.remove(fid)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(feeds_mod.snapshot())


@app.post("/api/feeds/check")
def api_feeds_check():
    """立刻检查所有订阅。body: {"enqueue": true} 发现新单集时是否入队。"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(check_feeds_now(enqueue=bool(data.get("enqueue", True))))
    except Exception as exc:
        return jsonify({"error": f"检查失败：{type(exc).__name__}: {exc}"[:300]}), 400


@app.post("/api/feeds/settings")
def api_feeds_settings():
    """保存订阅调度设置（存在 settings.json 的 subscriptions 段）。body: {...}"""
    data = request.get_json(force=True, silent=True) or {}
    saved = settings_mod.save(subscriptions=data)
    return jsonify(saved["subscriptions"])


# ---------------------------------------------------------------- 后台调度

def _episode_from_feed(item: dict) -> dict:
    """把订阅发现的单集固定成一份 Episode 快照。

    为什么不只存「feed + 第 N 集」：feed 每次更新时 pick 序号都会整体前移，
    排队中的任务会指向另一集。存快照才是稳定的。
    """
    return {
        "source": "rss",
        "url": item.get("episode_url") or item.get("audio_url") or item.get("feed_url"),
        "title": item.get("title") or "",
        "podcast": item.get("feed_title") or "",
        "author": "",
        "pub_date": item.get("pub_date"),
        "duration": None,
        "shownotes_html": None,
        "audio_url": item.get("audio_url"),
        "cover": None,
        "subtitle_tracks": [],
    }


def _enqueue_feed_episodes(items: list[dict], dest: str = "") -> list[dict]:
    added: list[dict] = []
    for it in items:
        opts = {"episode": _episode_from_feed(it)}
        if dest:
            opts["auto_dest"] = dest
        try:
            added.extend(queue_mod.add(
                it.get("feed_url") or it.get("episode_url"), opts=opts,
                source=f"feed:{it.get('feed_id', '')}",
                pick=int(it.get("pick") or 1), title=it.get("title") or "",
            ))
        except ValueError:
            continue          # 队列满了就停，别让一次检查炸掉
    return added


def check_feeds_now(*, enqueue: bool = True) -> dict:
    """检查所有订阅；发现新单集按设置决定是否自动入队。"""
    subs = settings_mod.load()["subscriptions"]
    found = feeds_mod.check()
    result = {
        "found": len(found),
        "enqueued": 0,
        "episodes": [{"feed": f.get("feed_title"), "title": f.get("title"),
                      "pub_date": f.get("pub_date")} for f in found],
    }
    if enqueue and found and subs.get("auto_generate", True):
        added = _enqueue_feed_episodes(found, subs.get("auto_dest") or "")
        result["enqueued"] = len(added)
    if found:
        result["feeds"] = feeds_mod.snapshot()
    return result


def run_queue_once() -> str | None:
    """取一条排队中的任务开跑，返回 job_id；没有可跑的返回 None。"""
    if _running_job_id():
        return None
    item = queue_mod.next_pending()
    if not item:
        return None
    claimed = queue_mod.claim(item["id"])
    if not claimed:
        return None
    return _new_job(claimed["url"], claimed.get("opts") or {},
                    source=claimed.get("source") or "queue", queue_id=claimed["id"])


def _wait_job(job_id: str, timeout: float = 6 * 3600, poll: float = 0.5) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return None
            if job["status"] != "running":
                return dict(job)
        time.sleep(poll)
    return None


_scheduler_stop = threading.Event()
_scheduler_started = False


def _maybe_check_feeds() -> None:
    subs = settings_mod.load()["subscriptions"]
    if not subs.get("enabled") or not subs.get("interval_minutes"):
        return
    if feeds_mod.due(int(subs["interval_minutes"])):
        check_feeds_now()


def _scheduler_loop() -> None:
    """后台循环：按间隔检查订阅 → 取队列里的任务跑完一条再跑下一条。"""
    while not _scheduler_stop.wait(5.0):
        try:
            _maybe_check_feeds()
        except Exception:
            pass
        try:
            job_id = run_queue_once()
        except Exception:
            job_id = None
        if not job_id:
            continue
        try:
            job = _wait_job(job_id) or {}
            qid = job.get("queue_id")
            if not qid:
                continue
            if job.get("status") == "done":
                queue_mod.finish(qid, "done", dir_name=job.get("workdir"))
            elif job.get("error"):
                queue_mod.finish(qid, "error", error=job["error"])
        except Exception:
            pass


def start_scheduler() -> bool:
    """启动后台调度线程（幂等）。测试与 CLI 里不会自动调用。"""
    global _scheduler_started
    if os.environ.get("PA_SCHEDULER", "1") == "0" or _scheduler_started:
        return False

    def guard() -> None:
        global _scheduler_started
        _scheduler_started = True
        _scheduler_loop()

    threading.Thread(target=guard, daemon=True, name="pa-scheduler").start()
    return True


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="podcast-article Web 界面")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PA_PORT", 8787)))
    parser.add_argument("--host", default="127.0.0.1",
                        help="默认只监听本机；如需局域网访问改为 0.0.0.0（注意无鉴权）")
    parser.add_argument("--no-scheduler", action="store_true",
                        help="不启动后台调度（订阅检查与批量队列自动执行）")
    args = parser.parse_args()
    if args.no_scheduler:
        os.environ["PA_SCHEDULER"] = "0"
    print(f"输出目录：{OUTPUT_ROOT}")
    print(f"打开 http://{args.host}:{args.port}")
    if start_scheduler():
        print("后台调度已启动：按间隔检查订阅 + 自动执行批量队列")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
