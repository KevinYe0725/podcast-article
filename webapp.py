"""本地 Web 界面：填链接 → 实时日志 → 展示文章。

启动：uv run python webapp.py  （默认 http://127.0.0.1:8787）
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import markdown
from flask import Flask, Response, jsonify, request, send_from_directory

from podcast_article import notion
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
            pipe = Pipeline(
                url=url,
                output_dir=OUTPUT_ROOT,
                language=opts.get("lang", "auto"),
                backend=opts.get("backend", "auto"),
                asr_model=opts.get("model") or None,
                no_subs=bool(opts.get("no_subs")),
                force_transcript=bool(opts.get("force_transcript")),
                force_article=bool(opts.get("force_article")),
                pick=int(opts.get("pick", 1)),
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
    """把某一集的文章写入 Notion。body: {"dir": "<输出目录名>"}"""
    data = request.get_json(force=True, silent=True) or {}
    dir_name = (data.get("dir") or "").strip()
    base = (OUTPUT_ROOT / dir_name).resolve()
    if not dir_name or not base.is_dir() or OUTPUT_ROOT.resolve() not in base.parents:
        return jsonify({"error": "目录不存在"}), 404
    article = base / "article.md"
    meta_file = base / "meta.json"
    if not article.is_file():
        return jsonify({"error": "该单集还没有生成文章"}), 400
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        meta = {}
    title = f"{meta.get('podcast', '')}｜{meta.get('title', '')}".strip("｜") or dir_name
    try:
        url = notion.push_article(
            title=title,
            markdown_text=article.read_text(encoding="utf-8"),
            source_url=meta.get("url"),
            podcast=meta.get("podcast"),
            pub_date=meta.get("pub_date"),
            duration=meta.get("duration"),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"url": url})


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
