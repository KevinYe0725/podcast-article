"""本地 Web 界面：填链接 → 实时日志 → 展示文章。

启动：uv run python webapp.py  （默认 http://127.0.0.1:8787）
"""
from __future__ import annotations

import json
import ipaddress
import os
import shlex
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
from functools import wraps
from pathlib import Path
from urllib.parse import quote

import markdown
from flask import Flask, Response, g, jsonify, make_response, redirect, request, send_file, send_from_directory, url_for

from podcast_article import auth as auth_mod
from podcast_article.platform_store import InviteError, normalize_username
from podcast_article import library as library_mod
from podcast_article import library_ask
from podcast_article import mcp_client, mcp_config, notion
from podcast_article import object_storage
from podcast_article import publish as publish_mod
from podcast_article import qa_store
from podcast_article import settings as settings_mod
from podcast_article import usage as usage_mod
from podcast_article import cover as cover_mod
from podcast_article import export as export_mod
from podcast_article import feeds as feeds_mod
from podcast_article import kb as kb_mod
from podcast_article import links as links_mod
from podcast_article import queue as queue_mod
from podcast_article import search as search_mod
from podcast_article import timestamps as timestamps_mod
from podcast_article import tts as tts_mod
from podcast_article.config import PROJECT_ROOT
from podcast_article.util import ts_clock
from podcast_article.pipeline import Pipeline
from podcast_article.workspace import data_root, workspace_for

# 输出根目录，可用 PA_OUTPUT_DIR 覆盖（测试用独立目录，避免碰真实数据）
OUTPUT_ROOT = Path(os.environ.get("PA_OUTPUT_DIR") or (PROJECT_ROOT / "output"))
WEB_DIR = PROJECT_ROOT / "web"

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")
app.config.update(
    PA_SESSION_IDLE_SECONDS=int(os.environ.get("PA_SESSION_IDLE_SECONDS", 12 * 60 * 60)),
    PA_SESSION_ABSOLUTE_SECONDS=int(os.environ.get("PA_SESSION_ABSOLUTE_SECONDS", 7 * 24 * 60 * 60)),
    PA_COOKIE_DOMAIN=os.environ.get("PA_COOKIE_DOMAIN") or None,
    PA_TRUSTED_PROXY=(os.environ.get("PA_TRUSTED_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}),
    PA_COOKIE_SECURE=(
        os.environ.get("PA_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes", "on"}
        if "PA_COOKIE_SECURE" in os.environ
        else bool(os.environ.get("PA_COOKIE_DOMAIN"))
    ),
)

_PUBLIC_ENDPOINTS = {
    "login_page", "invite_page", "api_auth_login", "api_auth_register",
    "api_auth_logout", "api_auth_me", "api_auth_csrf", "static", "favicon",
}
_AUTH_ONLY_ENDPOINTS = {"api_auth_me", "api_auth_password", "api_auth_logout", "api_auth_csrf"}


@app.before_request
def authenticate_request():
    endpoint = request.endpoint
    account = auth_mod.resolve_request(request)

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not auth_mod.csrf_request_valid(request, account):
            return jsonify({"error": "csrf_failed"}), 403

    if endpoint in _PUBLIC_ENDPOINTS:
        if account is not None:
            g.current_user = account
        return None

    if account is None:
        if request.path.startswith("/api/"):
            return jsonify({"error": "authentication_required"}), 401
        return redirect(url_for("login_page", next=auth_mod.safe_next_path(request.full_path)))

    g.current_user = account
    g.workspace = workspace_for(account.id, data_root())
    if account.must_change_password and endpoint not in _AUTH_ONLY_ENDPOINTS:
        if request.path.startswith("/api/"):
            return jsonify({"error": "password_change_required"}), 403
        return redirect(url_for("login_page", must_change="1"))
    return None


def _workspace():
    """The immutable paths derived from this request's authenticated account."""
    return g.workspace

# ---------------------------------------------------------------- 只读镜像
#
# 部署到公网服务器时用 PA_READONLY=1：那台机器只负责「看」——文章、检索、
# 时间戳回听、导出都能用，但生成 / 转写 / AI 助手 / 发布一律拦住。
# 为什么需要它：流水线要转写（服务器没 GPU，2 核跑不动）与 DeepSeek 密钥，
# 而这些都留在你自己的 Mac 上（算力在 Mac，服务器只做控制面 + 阅读面）。
#
# 这里刻意在**调用时**读全局 READONLY（而不是导入时定死），测试可以直接改它。
READONLY = (os.environ.get("PA_READONLY") or "").strip().lower() in ("1", "true", "yes", "on")
READONLY_HINT = ("这是一台只读镜像：生成文章、语音转写与 AI 助手都在你的 Mac 上跑。"
                 "请在 Mac 上打开 http://127.0.0.1:8787 提交链接。")


def _readonly_guard(fn):
    """装饰器：只读镜像下拦住「跑活 / 要密钥 / 删文件」的动作，给可读的 503。"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if READONLY:
            return jsonify({"error": READONLY_HINT, "readonly": True}), 503
        return fn(*args, **kwargs)

    return wrapper


_LOGIN_PAGE_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Podcast Article · 登录</title>
<style>
@font-face{font-family:Inter;src:url('/static/fonts/inter-400.woff2') format('woff2');font-weight:400;font-display:swap}@font-face{font-family:Inter;src:url('/static/fonts/inter-600.woff2') format('woff2');font-weight:600;font-display:swap}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f7f8;color:#0d0d0d;font:15px/1.6 Inter,system-ui,sans-serif}
main{width:min(94vw,920px);min-height:540px;display:grid;grid-template-columns:1fr 1fr;border:1px solid #ececf1;background:#fff;border-radius:14px;overflow:hidden}
.identity{padding:42px;background:#0d0d0d;color:#fff;display:flex;flex-direction:column;justify-content:space-between}.wordmark{display:flex;align-items:center;gap:12px;font:600 11px/1.2 ui-monospace,monospace;letter-spacing:.15em}.wave{height:35px;display:flex;align-items:center;gap:3px}.wave i{display:block;width:3px;border-radius:2px;background:#10a37f}.manifest{margin:54px 0}.eyebrow,.foot,.formbrand{font:11px/1.4 ui-monospace,monospace;letter-spacing:.12em;text-transform:uppercase}.eyebrow{color:#10a37f}h2{font-size:30px;line-height:1.25;letter-spacing:-.045em;margin:14px 0 12px}.manifest p:last-child{max-width:310px;color:#b4b4bc;font-size:14px}.foot{color:#8f8f8f}
.form-panel{padding:52px 48px;display:flex;flex-direction:column;justify-content:center}.formbrand{color:#8f8f8f}h1{font-size:26px;letter-spacing:-.04em;margin:13px 0 5px}p{color:#6e6e80;margin:0 0 25px}label{display:block;font-size:13px;margin:16px 0 6px}input{width:100%;height:46px;padding:0 12px;border:1px solid #d9d9e0;border-radius:7px;font:inherit;color:#0d0d0d}input:focus{outline:2px solid #10a37f;outline-offset:1px}button{margin-top:22px;width:100%;height:46px;border:0;border-radius:7px;background:#0d0d0d;color:#fff;font:600 14px Inter,system-ui,sans-serif;cursor:pointer;transition:background .15s}button:hover{background:#10a37f}button:focus-visible{outline:3px solid #10a37f;outline-offset:3px}.status{min-height:24px;margin-top:14px;color:#c43f32;font-size:14px}section[hidden]{display:none}.hint{font:11px/1.5 ui-monospace,monospace;color:#8f8f8f;margin-top:24px}.status:empty{min-height:0}
@media(max-width:680px){body{display:block;padding:14px}main{width:100%;min-height:0;grid-template-columns:1fr;margin:0 auto}.identity{padding:24px 25px 22px}.manifest{margin:28px 0 18px}h2{font-size:24px}.foot{display:none}.form-panel{padding:28px 25px 30px}}
</style><main><aside class="identity"><div class="wordmark"><span class="wave" aria-hidden="true"><i style="height:9px"></i><i style="height:18px"></i><i style="height:27px"></i><i style="height:14px"></i><i style="height:33px"></i><i style="height:21px"></i><i style="height:11px"></i><i style="height:25px"></i><i style="height:8px"></i></span>PODCAST ARTICLE</div><div class="manifest"><div class="eyebrow">PRIVATE LISTENING LIBRARY</div><h2>把时间留给<br>值得听的内容。</h2><p>播客文章、回听线索与听后想法，按账号独立保存。</p></div><div class="foot">ACCOUNT-LEVEL STORAGE · INVITE ONLY</div></aside><section class="form-panel"><div class="formbrand">ACCOUNT ACCESS</div>
<section id="login"><h1>欢迎回来</h1><p>登录后继续使用你的播客文章库。</p><form id="login-form">
<label for="username">用户名</label><input id="username" name="username" autocomplete="username" required>
<label for="password">密码</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<button>登录</button></form></section>
<section id="change" hidden><h1>请更新密码</h1><p>管理员重置了你的密码。继续使用前，请设置新密码。</p><form id="change-form">
<label for="current">当前密码</label><input id="current" type="password" autocomplete="current-password" required>
<label for="next-password">新密码（12–256 字节）</label><input id="next-password" type="password" autocomplete="new-password" required>
<button>更新密码</button></form></section>
<div class="status" id="status" role="status"></div><div class="hint">仅限受邀账号使用 · 你的内容按账号隔离</div></section></main>
<script>
const statusEl=document.querySelector('#status');
async function csrf(){const r=await fetch('/api/auth/csrf',{credentials:'same-origin'});const d=await r.json();return d.csrf_token}
async function send(path,payload){const token=await csrf();return fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(payload)})}
document.querySelector('#login-form').addEventListener('submit',async e=>{e.preventDefault();statusEl.textContent='';try{const r=await send('/api/auth/login',{username:document.querySelector('#username').value,password:document.querySelector('#password').value,next:new URLSearchParams(location.search).get('next')});const d=await r.json();if(!r.ok)throw new Error(d.message||'登录失败');location.assign(d.next||'/')}catch(err){statusEl.textContent=err.message}});
document.querySelector('#change-form').addEventListener('submit',async e=>{e.preventDefault();statusEl.textContent='';try{const r=await send('/api/auth/password',{current_password:document.querySelector('#current').value,new_password:document.querySelector('#next-password').value});const d=await r.json();if(!r.ok)throw new Error(d.message||'更新失败');location.assign('/')}catch(err){statusEl.textContent=err.message}});
fetch('/api/auth/me',{credentials:'same-origin'}).then(r=>r.json()).then(d=>{if(d.authenticated&&d.must_change_password){document.querySelector('#login').hidden=true;document.querySelector('#change').hidden=false}}).catch(()=>{});
</script></html>"""

_INVITE_PAGE_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Podcast Article · 接受邀请</title>
<style>
@font-face{font-family:Inter;src:url('/static/fonts/inter-400.woff2') format('woff2');font-weight:400;font-display:swap}@font-face{font-family:Inter;src:url('/static/fonts/inter-600.woff2') format('woff2');font-weight:600;font-display:swap}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f7f8;color:#0d0d0d;font:15px/1.6 Inter,system-ui,sans-serif}main{width:min(94vw,920px);min-height:540px;display:grid;grid-template-columns:1fr 1fr;border:1px solid #ececf1;background:#fff;border-radius:14px;overflow:hidden}.identity{padding:42px;background:#0d0d0d;color:#fff;display:flex;flex-direction:column;justify-content:space-between}.wordmark{display:flex;align-items:center;gap:12px;font:600 11px/1.2 ui-monospace,monospace;letter-spacing:.15em}.wave{height:35px;display:flex;align-items:center;gap:3px}.wave i{display:block;width:3px;border-radius:2px;background:#10a37f}.manifest{margin:54px 0}.eyebrow,.foot,.formbrand{font:11px/1.4 ui-monospace,monospace;letter-spacing:.12em;text-transform:uppercase}.eyebrow{color:#10a37f}h2{font-size:30px;line-height:1.25;letter-spacing:-.045em;margin:14px 0 12px}.manifest p:last-child{max-width:310px;color:#b4b4bc;font-size:14px}.foot{color:#8f8f8f}.form-panel{padding:52px 48px;display:flex;flex-direction:column;justify-content:center}.formbrand{color:#8f8f8f}h1{font-size:26px;letter-spacing:-.04em;margin:13px 0 5px}p{color:#6e6e80;margin:0 0 25px}label{display:block;font-size:13px;margin:16px 0 6px}input{width:100%;height:46px;padding:0 12px;border:1px solid #d9d9e0;border-radius:7px;font:inherit}input:focus{outline:2px solid #10a37f;outline-offset:1px}button{margin-top:22px;width:100%;height:46px;border:0;border-radius:7px;background:#0d0d0d;color:#fff;font:600 14px Inter,system-ui,sans-serif;cursor:pointer}button:hover{background:#10a37f}button:focus-visible{outline:3px solid #10a37f;outline-offset:3px}.status{min-height:24px;margin-top:14px;color:#c43f32;font-size:14px}@media(max-width:680px){body{display:block;padding:14px}main{width:100%;min-height:0;grid-template-columns:1fr}.identity{padding:24px 25px 22px}.manifest{margin:28px 0 18px}h2{font-size:24px}.foot{display:none}.form-panel{padding:28px 25px 30px}}
</style><main><aside class="identity"><div class="wordmark"><span class="wave" aria-hidden="true"><i style="height:9px"></i><i style="height:18px"></i><i style="height:27px"></i><i style="height:14px"></i><i style="height:33px"></i><i style="height:21px"></i><i style="height:11px"></i><i style="height:25px"></i><i style="height:8px"></i></span>PODCAST ARTICLE</div><div class="manifest"><div class="eyebrow">INVITATION ONLY</div><h2>一个账号，<br>一份独立空间。</h2><p>你的文章、书库与偏好不会和其他账号混在一起。</p></div><div class="foot">PRIVATE BY DESIGN · NO PUBLIC SIGN-UP</div></aside><section class="form-panel"><div class="formbrand">CREATE YOUR ACCOUNT</div><h1>接受邀请</h1><p>创建账号后即可开始使用。用户名仅用于登录。</p>
<form id="invite-form"><label for="username">用户名</label><input id="username" autocomplete="username" required>
<label for="password">密码（12–256 字节）</label><input id="password" type="password" autocomplete="new-password" required>
<button>创建账号</button></form><div class="status" id="status" role="status"></div></section></main>
<script>
const statusEl=document.querySelector('#status');const rawInviteToken=location.hash.slice(1);history.replaceState(null,'',location.pathname+location.search);let inviteToken='';try{inviteToken=decodeURIComponent(rawInviteToken)}catch{}
async function csrf(){const r=await fetch('/api/auth/csrf',{credentials:'same-origin'});const d=await r.json();return d.csrf_token}
document.querySelector('#invite-form').addEventListener('submit',async e=>{e.preventDefault();statusEl.textContent='';if(!inviteToken){statusEl.textContent='邀请链接无效或已使用';return}try{const token=await csrf();const r=await fetch('/api/auth/register',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({invite_token:inviteToken,username:document.querySelector('#username').value,password:document.querySelector('#password').value})});const d=await r.json();if(!r.ok)throw new Error(d.message||'邀请无效或已过期');inviteToken='';location.assign('/')}catch(err){statusEl.textContent=err.message}});
</script></html>"""


def _auth_store():
    return auth_mod._request_store()


def _auth_client_ip_key() -> str:
    remote_key = request.remote_addr or "unknown"
    if app.config.get("PA_TRUSTED_PROXY"):
        forwarded_ip = request.headers.get("X-Real-IP", "").strip()
        try:
            return str(ipaddress.ip_address(forwarded_ip))
        except ValueError:
            pass
    return remote_key


def _set_auth_cookies(response, session_token: str | None, csrf_token: str, *, absolute_seconds: int | None = None):
    options = {
        "path": "/",
        "secure": bool(app.config["PA_COOKIE_SECURE"]),
        "samesite": "Lax",
    }
    domain = app.config.get("PA_COOKIE_DOMAIN")
    if domain:
        options["domain"] = domain
    if session_token:
        response.set_cookie(
            "pa_session",
            session_token,
            max_age=absolute_seconds,
            httponly=True,
            **options,
        )
    else:
        response.delete_cookie("pa_session", httponly=True, **options)
    response.set_cookie("pa_csrf", csrf_token, max_age=absolute_seconds, httponly=False, **options)
    return response


def _authenticated_response(account, next_path: str = "/"):
    now = time.time()
    idle_seconds = int(app.config["PA_SESSION_IDLE_SECONDS"])
    absolute_seconds = int(app.config["PA_SESSION_ABSOLUTE_SECONDS"])
    if idle_seconds <= 0 or absolute_seconds <= 0 or idle_seconds > absolute_seconds:
        raise RuntimeError("invalid session timeout configuration")
    session_token = auth_mod.new_token()
    csrf_token = auth_mod.new_token()
    store = _auth_store()
    store.create_session(
        account.id,
        auth_mod.token_hash(session_token),
        now,
        now + idle_seconds,
        now + absolute_seconds,
        csrf_token_hash=auth_mod.token_hash(csrf_token),
    )
    response = jsonify({
        "authenticated": True,
        "username": account.username,
        "must_change_password": account.must_change_password,
        "next": auth_mod.safe_next_path(next_path),
    })
    return _set_auth_cookies(response, session_token, csrf_token, absolute_seconds=absolute_seconds)


@app.get("/login")
def login_page():
    return Response(_LOGIN_PAGE_HTML, mimetype="text/html")


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/invite")
def invite_page():
    return Response(_INVITE_PAGE_HTML, mimetype="text/html")


@app.get("/api/auth/csrf")
def api_auth_csrf():
    account = getattr(g, "current_user", None)
    csrf_token = auth_mod.issue_csrf(request, account)
    response = jsonify({"csrf_token": csrf_token})
    absolute_seconds = int(app.config["PA_SESSION_ABSOLUTE_SECONDS"])
    return _set_auth_cookies(response, None if account is None else request.cookies.get("pa_session"), csrf_token,
                             absolute_seconds=absolute_seconds)


@app.get("/api/auth/me")
def api_auth_me():
    account = auth_mod.resolve_request(request)
    if account is None:
        return jsonify({"authenticated": False})
    return jsonify({
        "authenticated": True,
        "id": account.id,
        "username": account.username,
        "role": account.role,
        "must_change_password": account.must_change_password,
    })


@app.post("/api/auth/login")
def api_auth_login():
    payload = request.get_json(silent=True) or {}
    username = payload.get("username", "")
    password = payload.get("password", "")
    try:
        username_key = normalize_username(username)[1]
    except ValueError:
        username_key = unicodedata.normalize("NFKC", str(username)).strip().casefold()
    remote_key = _auth_client_ip_key()
    now = time.time()
    store = _auth_store()
    if not store.login_allowed(username_key, remote_key, now):
        return jsonify({"error": "login_rate_limited", "message": "登录尝试过多，请稍后再试"}), 429

    credential = store.credential_for_username(username) if isinstance(username, str) else None
    encoded_hash = credential.password_hash if credential is not None else auth_mod._DUMMY_PASSWORD_HASH
    valid_password = auth_mod.verify_password(encoded_hash, password if isinstance(password, str) else "")
    if credential is None or not credential.account.enabled or not valid_password:
        store.record_login_attempt(username_key, remote_key, now)
        return jsonify({"error": "invalid_credentials", "message": "用户名或密码错误"}), 401

    requested_next = payload.get("next") or request.args.get("next")
    return _authenticated_response(credential.account, requested_next or "/")


@app.post("/api/auth/register")
def api_auth_register():
    payload = request.get_json(silent=True) or {}
    invite_token = payload.get("invite_token")
    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(invite_token, str) or not invite_token or len(invite_token) > 256:
        return jsonify({"error": "invalid_invite", "message": "邀请链接无效或已过期"}), 400
    try:
        auth_mod.validate_new_password(password)
        normalize_username(username)
    except ValueError:
        return jsonify({"error": "invalid_registration", "message": "用户名或密码格式无效"}), 400
    store = _auth_store()
    token_digest = auth_mod.token_hash(invite_token)
    remote_key = _auth_client_ip_key()
    now = time.time()
    if not store.login_allowed(token_digest, remote_key, now):
        return jsonify({"error": "registration_rate_limited", "message": "尝试过多，请稍后再试"}), 429
    try:
        store.validate_invite(token_digest, now=now)
    except InviteError as error:
        store.record_login_attempt(token_digest, remote_key, now)
        return jsonify({"error": "invalid_invite", "message": "邀请链接无效或已过期"}), 400
    if store.credential_for_username(username) is not None:
        return jsonify({"error": "invalid_registration", "message": "用户名或邀请链接不可用"}), 400
    try:
        password_hash = auth_mod.hash_password(password)
    except ValueError:
        return jsonify({"error": "invalid_password", "message": "密码长度需为 12–256 个 UTF-8 字节"}), 400
    try:
        account = store.register_invite(token_digest, username, password_hash, now=now)
    except InviteError as error:
        store.record_login_attempt(token_digest, remote_key, now)
        return jsonify({"error": "invalid_invite", "reason": error.code, "message": "邀请链接无效或已过期"}), 400
    except ValueError as error:
        return jsonify({"error": "invalid_registration", "message": str(error)}), 400
    return _authenticated_response(account)


@app.post("/api/auth/logout")
def api_auth_logout():
    raw_session = request.cookies.get("pa_session", "")
    if raw_session:
        _auth_store().revoke_session(auth_mod.token_hash(raw_session))
    csrf_token = auth_mod.new_token()
    response = jsonify({"authenticated": False})
    response = _set_auth_cookies(response, None, csrf_token)
    return response


@app.post("/api/auth/password")
def api_auth_password():
    account = g.current_user
    payload = request.get_json(silent=True) or {}
    current_password = payload.get("current_password", "")
    new_password = payload.get("new_password", "")
    credential = _auth_store().credential_for_username(account.username)
    if credential is None or not auth_mod.verify_password(credential.password_hash, current_password):
        return jsonify({"error": "invalid_current_password", "message": "当前密码错误"}), 401
    try:
        new_hash = auth_mod.hash_password(new_password)
    except ValueError:
        return jsonify({"error": "invalid_password", "message": "密码长度需为 12–256 个 UTF-8 字节"}), 400
    store = _auth_store()
    updated = store.replace_password_hash(account.id, new_hash, must_change=False)
    return _authenticated_response(updated)


_JOBS: dict[str, dict] = {}          # job_id -> 状态字典
_JOBS_LOCK = threading.Lock()
_ALLOWED_FILES = {"article.md", "transcript.txt", "transcript.json", "meta.json",
                  "usage.json", "outline.json", "qa.json"}


def _new_job(url: str, opts: dict, *, source: str = "manual",
             queue_id: str | None = None, workspace=None) -> str:
    output_root = workspace.output_root if workspace is not None else OUTPUT_ROOT
    settings_path = workspace.settings_path if workspace is not None else None
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
            defaults = settings_mod.load(settings_path=settings_path)["generation"]
            lang = opts.get("lang") or defaults.get("language") or "auto"
            pipe = Pipeline(
                url=url,
                output_dir=output_root,
                language=lang,
                backend=(opts.get("backend")
                         or os.environ.get("PA_ASR_BACKEND")
                         or defaults.get("backend")
                         or "auto"),
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
                settings_path=settings_path,
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
            _after_done(workdir.name, opts, log, workspace=workspace)
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            job["logs"].append(f"[error] {exc}")
        finally:
            _finish_queue_item(job)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def _finish_queue_item(job: dict) -> None:
    """把任务结果回写到它对应的队列条目上。

    这个动作必须挂在**任务自己**身上，不能只交给后台调度循环：否则用
    `/api/queue/run` 手动开一条、或者 `--no-scheduler` 启动时，条目会永远停在
    running，而队列只挑 pending —— 整条队列就被一条失败任务堵死了（实测踩过）。
    """
    qid = job.get("queue_id")
    if not qid:
        return
    try:
        if job["status"] == "done":
            queue_mod.finish(qid, "done", dir_name=job.get("workdir"))
        elif job["status"] == "error":
            queue_mod.finish(qid, "error", error=job.get("error") or "未知错误")
    except ValueError:
        pass          # 条目被用户删掉了，不用管


def _after_done(dir_name: str, opts: dict, log, *, workspace=None) -> None:
    """任务成功后的小动作：订阅自动归类、新文章自动入库。失败不影响主流程。"""
    try:
        dest = (opts or {}).get("auto_dest")
        if dest:
            library_mod.assign(dir_name, dest,
                               store_path=workspace.library_path if workspace is not None else None)
            log(f"[done] 已归入分类：{dest}")
    except Exception as exc:                 # 归类失败不该让「文章已生成」变成失败
        log(f"[done] 自动归类失败：{exc}")
    _auto_index(dir_name, log, workspace=workspace)


def _auto_index(dir_name: str, log=print, *, workspace=None) -> None:
    """新文章生成后**自动进知识库**：用户要的是「收集库」，不该每次手动点一次重建。

    只索引这一篇（增量），失败只写一行日志 —— 文章已经生成好了，索引是锦上添花，
    绝不能因为它把一次成功的生成变成失败。只读模式（算力在 Mac 上）直接跳过。
    """
    if READONLY:
        return
    try:
        output_root = workspace.output_root if workspace is not None else OUTPUT_ROOT
        db_path = workspace.kb_path if workspace is not None else None
        conn = kb_mod.ensure_schema(kb_mod.connect(db_path))
        try:
            r = kb_mod.index_dir(conn, output_root / dir_name, force=False,
                                 log=lambda *_a, **_k: None)
        finally:
            conn.close()
        log(f"[done] 已存入知识库：{r.get('passages', 0)} 个切片")
    except Exception as exc:
        log(f"[done] 入库失败（不影响文章）：{type(exc).__name__}: {exc}")


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


_AUDIO_NAMES = ("audio.m4a", "audio.mp3", "audio.webm", "audio.wav", "audio.m4a")


def _audio_path(base: Path) -> Path | None:
    for name in _AUDIO_NAMES:
        p = base / name
        if p.is_file():
            return p
    return None


def _linkify_timestamps(html: str) -> str:
    """把正文里的时间戳变成可点元素，点了跳到音频对应位置。

    认单个时间点 `[00:10:07]`，也认引用一段话的时间区间 `[00:06:36-00:06:49]`
    （区间按起点跳转）—— 只认前者时，整篇都是区间的文章一个可点的链接都没有。
    规则都在 podcast_article.timestamps 里（前端 web/app.js 有一份对应的实现）。

    用 <span> 而不是 <a>：时间戳本来就不是链接，而 <a> 无论带不带 href 都可能触发一次
    「导航」—— 带 href="#" 会跳到 #，不带 href 时 jsdom 也会把它当成空相对地址去 follow。
    阅读页把地址栏当路由（#/a/<目录名>），这种导航会被判成「离开了这一篇」，整页收起来。
    span 没有默认动作，点击完全交给页面上的委托监听；role/tabindex 是为了键盘也能用。
    """
    return timestamps_mod.linkify(html)


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
@_readonly_guard
def api_run():
    data = request.get_json(force=True, silent=True) or {}
    # 粘贴过来的常常是「【标题】https://…」：任务里存的、界面上显示的都该是链接本身
    url = links_mod.first_link(data.get("url") or "")
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
    job_id = _new_job(url, data, workspace=_workspace())
    return jsonify({"job_id": job_id})


@app.get("/api/config")
def api_config():
    """前端启动时问一次：这台机器允许做什么。

    只读镜像（PA_READONLY=1）下前端会收起输入框、悬浮球与发布入口，
    并说明「算力在 Mac 上」——比让人点一个必然失败的按钮诚实。
    """
    return jsonify({
        "readonly": READONLY,
        "hint": READONLY_HINT if READONLY else "",
        "assistant": not READONLY,      # 阅读助手要 DeepSeek 密钥
        "publish": not READONLY,        # Notion / MCP 同理
    })


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
@_readonly_guard
def api_notion():
    """把某一集的文章写入内置 Notion 集成。body: {"dir": "<输出目录名>"}"""
    return _publish_with({"target": "builtin"})


def _episode_ctx(dir_name: str, workspace=None):
    """读取一集的文章与元信息，组装成发布上下文。返回 (ctx, error_response)。"""
    output_root = workspace.output_root if workspace is not None else _workspace().output_root
    base = (output_root / (dir_name or "")).resolve()
    if not dir_name or not base.is_dir() or output_root.resolve() not in base.parents:
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
    ctx, err = _episode_ctx((data.get("dir") or "").strip(), _workspace())
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
@_readonly_guard
def api_publish():
    """发布文章。body: {"dir", "target": "builtin"|"mcp:<server>:<tool>", "template": {...}}"""
    return _publish_with()


@app.get("/api/settings")
def api_settings_get():
    """设置页数据：当前账号的偏好与存储信息。服务器凭据不通过成员接口显示。"""
    workspace = _workspace()
    current = settings_mod.load(settings_path=workspace.settings_path)
    return jsonify({
        "profile": current["profile"],
        "generation": current["generation"],
        "subscriptions": current["subscriptions"],
        "assistant": current["assistant"],
        "tts": current["tts"],
        "tts_voices": tts_mod.macos_voices(),      # macOS 上可用的中文音色（给设置页做提示）
        "tts_available": bool(shutil.which("say")) or tts_mod.DEFAULTS["provider"] != "off",
        "server_credentials": "managed_by_server",
        "storage": settings_mod.storage_info(output_root=workspace.output_root,
                                              settings_path=workspace.settings_path),
    })


@app.post("/api/settings")
@_readonly_guard
def api_settings_post():
    """保存当前账号偏好。服务端模型密钥由管理员管理，不接受成员写入。"""
    data = request.get_json(force=True, silent=True) or {}
    if data.get("secrets"):
        return jsonify({"error": "server_credentials_managed"}), 400
    workspace = _workspace()
    saved = settings_mod.save(profile=data.get("profile"),
                              generation=data.get("generation"),
                              subscriptions=data.get("subscriptions"),
                              assistant=data.get("assistant"),
                              tts=data.get("tts"),
                              settings_path=workspace.settings_path)
    return jsonify({
        "profile": saved["profile"],
        "generation": saved["generation"],
        "subscriptions": saved["subscriptions"],
        "assistant": saved["assistant"],
        "tts": saved["tts"],
        "server_credentials": "managed_by_server",
    })


@app.post("/api/settings/verify")
@_readonly_guard
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
@_readonly_guard
def api_mcp_preset():
    """按预设一键添加。body: {"preset": "notion", "secret": "ntn_…"（可选）}"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        entry = mcp_config.build_from_preset(data.get("preset", ""), data.get("secret"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"server": mcp_config.mask_entry(entry)})


@app.post("/api/mcp/servers")
@_readonly_guard
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
@_readonly_guard
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
@_readonly_guard
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
@_readonly_guard
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
    workspace = _workspace()
    items = _library_items(output_root=workspace.output_root)
    cat_state = library_mod.snapshot(store_path=workspace.library_path)
    totals = usage_mod.summary_over(workspace.output_root)
    return jsonify({
        "items": items,
        "categories": cat_state["categories"],
        "assignments": cat_state["assignments"],
        "status": cat_state["status"],
        "status_counts": cat_state["status_counts"],
        "status_labels": cat_state["status_labels"],
        "usage_total": totals,
        "search_stats": search_mod.stats(workspace.output_root),
        "queue_active": queue_mod.snapshot()["active"],
    })


@app.get("/api/categories")
def api_categories():
    """分类列表（含各自文章数）与文章归属。"""
    return jsonify(library_mod.snapshot(store_path=_workspace().library_path))


@app.post("/api/categories")
@_readonly_guard
def api_category_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        cat = library_mod.create(data.get("name", ""), store_path=_workspace().library_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"category": cat})


@app.patch("/api/categories/<cid>")
def api_category_rename(cid: str):
    data = request.get_json(force=True, silent=True) or {}
    try:
        cat = library_mod.rename(cid, data.get("name", ""), store_path=_workspace().library_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"category": cat})


@app.delete("/api/categories/<cid>")
@_readonly_guard
def api_category_delete(cid: str):
    try:
        library_mod.delete(cid, store_path=_workspace().library_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"ok": True})


@app.post("/api/assign")
@_readonly_guard
def api_assign():
    """把文章放进分类（category_id 为空 → 移出分类）。body: {"dir", "category_id"}"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        library_mod.assign(data.get("dir", ""), (data.get("category_id") or "").strip() or None,
                           store_path=_workspace().library_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(library_mod.snapshot(store_path=_workspace().library_path))


@app.delete("/api/episode/<job_dir>")
@_readonly_guard
def api_episode_delete(job_dir: str):
    """删除一条历史记录。

    body: {"scope": "all"}     → 删整个输出目录（文章 + 音频 + 文字稿）
          {"scope": "article"} → 只删 article.md，保留音频与文字稿以便重新生成
    """
    data = request.get_json(force=True, silent=True) or {}
    scope = data.get("scope", "all")
    workspace = _workspace()
    base = (workspace.output_root / job_dir).resolve()
    if not job_dir or not base.is_dir() or workspace.output_root.resolve() not in base.parents:
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
            library_mod.forget(job_dir, store_path=workspace.library_path)
    except OSError as exc:
        return jsonify({"error": f"删除失败：{exc}"}), 500
    return jsonify({"ok": True, "scope": scope, "freed": freed, "dir": job_dir})


# ---------------------------------------------------------------- 知识库与记忆
#
# 为什么要有：书库是「一篇一篇」的 —— 检索只在正文里找词，助手只认当前这一集。
# 知识库把跨集检索、实体索引、用户记忆补上，全部落在本地一个 SQLite 文件里
# （派生数据，删了能重建），不需要任何外部服务。
_KB_LOCK = threading.Lock()
_KB_JOB: dict = {"state": "idle", "done": 0, "total": 0, "note": "", "error": "",
                 "result": None}


@app.get("/api/kb/status")
def api_kb_status():
    workspace = _workspace()
    try:
        st = kb_mod.stats(output_root=workspace.output_root, db_path=workspace.kb_path)
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"[:200]}), 500
    st["indexing"] = _KB_JOB
    return jsonify(st)


@app.post("/api/kb/reindex")
@_readonly_guard
def api_kb_reindex():
    """重建索引（派生数据，force 会重算切片）。后台跑，前端轮询 status。"""
    with _KB_LOCK:
        if _KB_JOB.get("state") == "running":
            return jsonify({"error": "已经在索引了", "indexing": _KB_JOB}), 409
        _KB_JOB.update({"state": "running", "done": 0, "total": 0, "note": "准备中",
                        "error": "", "result": None})

    workspace = _workspace()

    def work(force: bool, workspace) -> None:
        def progress(done: int, total: int, note: str) -> None:
            _KB_JOB.update({"done": done, "total": total, "note": note})
        try:
            r = kb_mod.index_all(workspace.output_root, force=force, progress=progress,
                                 db_path=workspace.kb_path)
            _KB_JOB.update({"state": "done", "result": r, "note": ""})
        except Exception as exc:
            _KB_JOB.update({"state": "error", "error": f"{type(exc).__name__}: {exc}"[:200]})

    force = bool((request.get_json(force=True, silent=True) or {}).get("force"))
    threading.Thread(target=work, args=(force, workspace), daemon=True).start()
    return jsonify({"state": "running", "indexing": _KB_JOB})


@app.get("/api/kb/search")
def api_kb_search():
    """跨集检索。返回带出处的切片（哪一集、小标题、时间戳）。"""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "缺少 q"}), 400
    k = min(30, max(1, int(request.args.get("k") or 8)))
    mode = (request.args.get("mode") or "auto").lower()
    dirs = [d for d in (request.args.get("dir") or "").split(",") if d] or None
    workspace = _workspace()
    try:
        res = kb_mod.search(q, k=k, mode=mode, dirs=dirs, db_path=workspace.kb_path)
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"[:200]}), 500
    res["hits"] = [kb_mod._hit_public(h) for h in res["hits"]]
    # 一个搜索框同时搜两样东西：资料（书库切片）和他自己存下的记忆。
    # 分开搜的话，「我记过这个吗」永远要用户自己想 —— 那就等于没记住。
    try:
        res["memory"] = [{"id": m["id"], "text": m["text"], "kind": m["kind"],
                          "pinned": bool(m["pinned"]), "source_dir": m.get("source_dir") or ""}
                         for m in kb_mod.memory_list(query=q, limit=5, db_path=workspace.kb_path)]
    except Exception:
        res["memory"] = []
    return jsonify(res)


@app.post("/api/kb/ask")
@_readonly_guard
def api_kb_ask():
    """跨集问答：先检索，再让模型**只依据检索到的原文**回答，并给出处。

    跟单集助手的区别：证据来自整个书库。回答里每一条都要能对上 [n] 编号的出处，
    编号由服务端按检索结果生成（不让模型自己编号，否则一定会瞎编）。
    """
    data = request.get_json(force=True, silent=True) or {}
    q = (data.get("question") or "").strip()
    if len(q) < 2:
        return jsonify({"error": "问题太短"}), 400
    # 候选给到 10：重排要做去重与每集上限，多给几条才有得挑（融合池 = k*3）。
    k = min(12, max(3, int(data.get("k") or 10)))
    workspace = _workspace()
    try:
        res = kb_mod.search(q, k=k, db_path=workspace.kb_path)
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"[:200]}), 500
    hits = [kb_mod._hit_public(h, limit=700) for h in res["hits"]]
    if not hits:
        return jsonify({"answer": "书库里没有检索到相关内容。可以先重建索引，或换个说法。",
                        "sources": [], "mode": res["mode"]})

    memories = kb_mod.memory_for_prompt(q, db_path=workspace.kb_path)
    try:
        # 作答规则统一在 podcast_article/library_ask.py（Web / CLI / MCP 共用一份）
        answer = library_ask.answer(q, hits, [m["text"] for m in memories])
    except Exception as exc:
        # 模型不可用时把检索结果给出去 —— 有出处的原文比一句报错有用
        return jsonify({"answer": "", "sources": hits, "mode": res["mode"],
                        "error": f"模型调用失败：{type(exc).__name__}: {str(exc)[:160]}"})
    return jsonify({"answer": (answer or "").strip(), "sources": hits, "mode": res["mode"],
                    "memory_used": [m["text"] for m in memories]})


@app.get("/api/kb/entities")
def api_kb_entities():
    etype = (request.args.get("type") or "").strip() or None
    limit = min(200, max(1, int(request.args.get("limit") or 60)))
    min_count = max(1, int(request.args.get("min_count") or 2))
    return jsonify({"entities": kb_mod.entities(limit=limit, etype=etype, min_count=min_count,
                                                 db_path=_workspace().kb_path),
                    "types": list(kb_mod.ENTITY_TYPES)})


@app.get("/api/kb/entity/<path:name>")
def api_kb_entity(name: str):
    d = kb_mod.entity_detail(name, db_path=_workspace().kb_path)
    if not d.get("found"):
        return jsonify({"error": "没有这个实体"}), 404
    return jsonify(d)


# ---------------------------------------------------------------- 记忆

@app.get("/api/memory")
def api_memory_list():
    q = (request.args.get("q") or "").strip()
    kind = (request.args.get("kind") or "").strip() or None
    items = kb_mod.memory_list(kind=kind, query=q, db_path=_workspace().kb_path)
    return jsonify({"items": items, "kinds": list(kb_mod.MEMORY_KINDS)})


@app.post("/api/memory")
@_readonly_guard
def api_memory_add():
    data = request.get_json(force=True, silent=True) or {}
    workspace = _workspace()
    try:
        item = kb_mod.memory_add(
            data.get("text") or "", kind=data.get("kind") or "fact",
            tags=data.get("tags") or "", source_dir=data.get("dir") or "",
            source_kind=data.get("source") or "user", pinned=bool(data.get("pinned")),
            db_path=workspace.kb_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(item)


@app.patch("/api/memory/<int:mid>")
@_readonly_guard
def api_memory_update(mid: int):
    data = request.get_json(force=True, silent=True) or {}
    item = kb_mod.memory_update(mid, db_path=_workspace().kb_path,
                                **{k: v for k, v in data.items()
                                   if k in ("text", "kind", "tags", "pinned")})
    if not item:
        return jsonify({"error": "没有这条记忆"}), 404
    return jsonify(item)


@app.delete("/api/memory/<int:mid>")
@_readonly_guard
def api_memory_delete(mid: int):
    return jsonify({"ok": kb_mod.memory_delete(mid, db_path=_workspace().kb_path)})


# ---------------------------------------------------------------- 朗读（TTS）
#
# 把文章念出来。设计要点：
#   · 生成是**后台线程 + 轮询状态**（不是 SSE）：长文分 6-10 块、每块一次接口调用，
#     总共几十秒到几分钟，前端每 1.2 秒问一次状态就够，实现也简单得多。
#   · 音频按块存盘（output/<dir>/tts/000.mp3 …），有 ffmpeg 再拼成整篇 article.<ext>。
#     分块的价值在于单块可重试：第 8 块失败时，前 7 块（已经花钱了）不用重来。
#   · 内容与音色一起哈希，没变就复用，不重复付费。
_TTS_LOCK = threading.Lock()
_TTS_JOBS: dict[str, dict] = {}          # dir 名 -> {state, done, total, note, error}


def _tts_cfg(workspace=None) -> dict:
    workspace = workspace or _workspace()
    s = settings_mod.load(settings_path=workspace.settings_path).get("tts") or {}
    cfg = dict(tts_mod.DEFAULTS)
    cfg.update({k: v for k, v in s.items() if k in tts_mod.DEFAULTS})
    return cfg


def _tts_preview_stem(workspace=None) -> Path:
    workspace = workspace or _workspace()
    return workspace.root / "tmp" / "tts-preview"


def _tts_key() -> str:
    return (settings_mod.read_env().get("TTS_API_KEY") or "").strip()


def _tts_payload(job_dir: str, base: Path) -> dict:
    """给前端的朗读状态：能不能用、跑到哪了、有哪些块。"""
    cfg = _tts_cfg()
    idx = tts_mod.load_index(base)
    job = _TTS_JOBS.get(job_dir) or {}
    state = job.get("state") or ("ready" if idx and idx.get("chunks") else "idle")
    fresh = False
    article = base / "article.md"
    if idx and article.exists():
        try:
            fresh = idx.get("text_sha") == tts_mod.text_sha(
                tts_mod.prepare_text(article.read_text(encoding="utf-8")), cfg)
        except Exception:
            fresh = False
    enc = quote(job_dir, safe="")
    merged = None
    if idx and tts_mod.merged_path(base):
        merged = f"/api/tts/{enc}/audio"
    return {
        "state": state,
        "enabled": cfg.get("provider") not in ("", "off"),
        "provider": cfg.get("provider") or "off",
        "voice": cfg.get("voice") or "",
        "model": cfg.get("model") or "",
        "speed": cfg.get("speed") or 1.0,
        "done": job.get("done", 0),
        "total": job.get("total") or (len(idx["chunks"]) if idx else 0),
        "note": job.get("note", ""),
        "error": job.get("error", ""),
        "fresh": fresh,
        "chars": (idx or {}).get("chars"),
        "seconds": (idx or {}).get("seconds"),
        "merged": merged,
        "chunks": [
            {"url": f"/api/tts/{enc}/chunk/{i}", "chars": c.get("chars")}
            for i, c in enumerate((idx or {}).get("chunks") or [])
        ] if (idx and not merged) else [],
    }


def _run_tts(job_dir: str, base: Path, article: str, cfg: dict, force: bool) -> None:
    """后台线程：跑完就更新 _TTS_JOBS，前端轮询看到状态变化。"""

    def progress(done: int, total: int, note: str) -> None:
        with _TTS_LOCK:
            _TTS_JOBS[job_dir] = {"state": "running", "done": done, "total": total, "note": note}

    try:
        idx = tts_mod.synthesize(base, article, cfg, api_key=_tts_key(),
                                 progress=progress, log=lambda m: _log_tts(job_dir, m), force=force)
        n = len(idx.get("chunks") or [])
        # 完成时 total 要保留真实块数（写成 0 会让前端进度归零）
        with _TTS_LOCK:
            _TTS_JOBS[job_dir] = {"state": "ready", "done": n, "total": n, "note": ""}
    except Exception as exc:
        with _TTS_LOCK:
            _TTS_JOBS[job_dir] = {"state": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}


def _log_tts(job_dir: str, message: str) -> None:
    """朗读日志并进任务日志（文章页的「详细日志」里能看到），同时写后台日志文件。"""
    print(f"[tts:{job_dir}] {message}", flush=True)


@app.get("/api/tts/<job_dir>/status")
def api_tts_status(job_dir: str):
    base = _safe_dir(job_dir)
    if not base:
        return jsonify({"error": "目录不存在"}), 404
    return jsonify(_tts_payload(job_dir, base))


@app.post("/api/tts/<job_dir>")
@_readonly_guard
def api_tts_start(job_dir: str):
    """开始（或强制重新）朗读这一篇。"""
    base = _safe_dir(job_dir)
    if not base:
        return jsonify({"error": "目录不存在"}), 404
    article_path = base / "article.md"
    if not article_path.exists():
        return jsonify({"error": "这一集还没有文章"}), 400
    cfg = _tts_cfg()
    if cfg.get("provider") in ("", "off"):
        return jsonify({"error": "还没有启用朗读：设置 → 朗读，选一个语音后端"
                                 "（macOS 本地免费，或任何兼容 OpenAI 的语音接口）"}), 400
    with _TTS_LOCK:
        if (_TTS_JOBS.get(job_dir) or {}).get("state") == "running":
            return jsonify({"error": "这一篇正在生成朗读，稍等", "tts": _tts_payload(job_dir, base)}), 409
        _TTS_JOBS[job_dir] = {"state": "running", "done": 0, "total": 0, "note": "准备中"}
    force = bool((request.get_json(force=True, silent=True) or {}).get("force"))
    article = article_path.read_text(encoding="utf-8")
    threading.Thread(target=_run_tts, args=(job_dir, base, article, cfg, force), daemon=True).start()
    return jsonify({"state": "running", "tts": _tts_payload(job_dir, base)})


@app.delete("/api/tts/<job_dir>")
@_readonly_guard
def api_tts_delete(job_dir: str):
    base = _safe_dir(job_dir)
    if not base:
        return jsonify({"error": "目录不存在"}), 404
    tts_mod.remove(base)
    with _TTS_LOCK:
        _TTS_JOBS.pop(job_dir, None)
    return jsonify({"ok": True, "freed": tts_mod.TTS_DIR})


def _tts_file(base: Path, relative: str) -> Path | None:
    d = tts_mod.tts_dir(base).resolve()
    p = (d / relative).resolve()
    return p if p.is_file() and d in p.parents else None


@app.get("/api/tts/<job_dir>/audio")
def api_tts_audio(job_dir: str):
    """整篇音频（有 ffmpeg 拼过才有）；带 Range，能拖进度条。"""
    base = _safe_dir(job_dir)
    if not base:
        return jsonify({"error": "目录不存在"}), 404
    path = tts_mod.merged_path(base)
    if not path:
        return jsonify({"error": "还没有拼成整篇（可能没装 ffmpeg，按分段播放即可）"}), 404
    return send_file(path, conditional=True, mimetype=_audio_mime(path.suffix))


@app.get("/api/tts/<job_dir>/chunk/<int:index>")
def api_tts_chunk(job_dir: str, index: int):
    """分段音频。没有 ffmpeg 时前端就按顺序连播这些块。"""
    base = _safe_dir(job_dir)
    if not base:
        return jsonify({"error": "目录不存在"}), 404
    idx = tts_mod.load_index(base) or {}
    chunks = idx.get("chunks") or []
    if index < 0 or index >= len(chunks):
        return jsonify({"error": "没有这一段"}), 404
    path = _tts_file(base, chunks[index]["file"])
    if not path:
        return jsonify({"error": "音频文件不见了"}), 404
    return send_file(path, conditional=True, mimetype=_audio_mime(path.suffix))


def _audio_mime(suffix: str) -> str:
    return {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
            ".opus": "audio/ogg", ".ogg": "audio/ogg"}.get(suffix.lower(), "application/octet-stream")


@app.post("/api/tts/preview")
@_readonly_guard
def api_tts_preview():
    """试听：用当前设置读一句短话（保存设置后点一下就知道能不能用）。"""
    workspace = _workspace()
    cfg = _tts_cfg(workspace)
    if cfg.get("provider") in ("", "off"):
        return jsonify({"ok": False, "error": "先选一个语音后端"}), 400
    try:
        stem = _tts_preview_stem(workspace)
        stem.parent.mkdir(parents=True, exist_ok=True)
        info = tts_mod.preview(cfg, _tts_key(), stem)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
    return jsonify({"ok": True, "url": f"/api/tts/preview?ts={int(time.time())}",
                    "seconds": info.get("seconds"), "bytes": info.get("bytes"),
                    "provider": cfg.get("provider"), "voice": cfg.get("voice")})


@app.get("/api/tts/preview")
def api_tts_preview_get():
    """试听音频本身。"""
    stem = _tts_preview_stem()
    for suffix in (".m4a", ".mp3", ".wav", ".opus"):
        p = stem.with_suffix(suffix)
        if p.exists():
            return send_file(p, conditional=True, mimetype=_audio_mime(suffix))
    return jsonify({"error": "还没有试听音频"}), 404


@app.get("/api/audio/<job_dir>")
def api_audio(job_dir: str):
    """本地音频流。conditional=True 让 werkzeug 处理 Range 请求，200MB 的文件也能拖动进度条。"""
    base = _safe_dir(job_dir)
    if not base:
        return jsonify({"error": "目录不存在"}), 404
    path = _audio_path(base)
    if path:
        return send_file(path, conditional=True, mimetype="audio/mp4")
    try:
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        object_key = meta.get("audio_object_key")
        if object_key:
            storage = object_storage.ObjectStorage()
            if storage.exists(object_key):
                return redirect(storage.signed_url(object_key), code=302)
    except (OSError, json.JSONDecodeError, RuntimeError):
        pass
    return jsonify({"error": "这一集没有可用音频"}), 404


# ---------------------------------------------------------------- AI 阅读助手
#
# 与「生成文章」的任务分开管理：提问是**秒级**的交互，不该被单个任务锁挡住，
# 也不该出现在批量队列里。所以另开一套 _ASKS，允许多个提问并行（都是小请求）。

_ASKS: dict[str, dict] = {}
_ASKS_LOCK = threading.Lock()
_MAX_ASKS = 50                     # 只留最近这些提问的内存状态
MAX_HISTORY_TURNS = 12             # 连续追问时最多带上多少轮（再多也放不下、也更贵）
MAX_HISTORY_CHARS = 4000           # 单轮上限；解读本身也就几百字，这个够宽松了


def _slim_ask(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],           # running / done / error
        "stage": job.get("stage") or "",   # retrieve（找原文）/ search（联网）/ write（写解读）
        "answer": job.get("answer") or "",
        "sources": job.get("sources"),
        "thread": job.get("thread") or "",
        "error": job.get("error") or "",
    }


def _clean_history(raw) -> list[dict]:
    """清洗前端传来的历史轮次：只认 user/assistant 两个角色，单条与总长都截断。

    这是**用户可控输入**，会被拼进提示词，所以不能原样透传：
    条数与长度都要有上限，避免把整个对话历史灌成超长上下文（既贵又容易跑题）。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for turn in raw[-MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip()
        content = turn.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        out.append({"role": role, "content": text[:MAX_HISTORY_CHARS]})
    return out


def _ask_query(selection: str, question: str) -> str:
    """**文字稿检索**用的查询串：以选中文字为主，带上追问。

    直接把两者拼起来交给检索器：选中文字往往已经是最有信息量的短语，
    追问通常是「这是什么意思」这类没有检索价值的句子，所以追问只在选文很短时才起主导作用。
    注意这与**联网检索**分开：联网要的是短关键词（deepdive.search_query），
    文字稿检索要的是原句（子串命中越长越准）。
    """
    sel = (selection or "").strip()
    q = (question or "").strip()
    if len(sel) >= 8:
        return sel if len(sel) <= 400 else sel[:400]
    return (sel + " " + q).strip()[:400]


@app.post("/api/ask")
@_readonly_guard
def api_ask():
    """发起一次深挖提问。body: {"dir", "selection", "question", "web"}"""
    data = request.get_json(force=True, silent=True) or {}
    dir_name = (data.get("dir") or "").strip()
    base = _safe_dir(dir_name)
    if not base:
        return jsonify({"error": "目录不存在"}), 404
    selection = (data.get("selection") or "").strip()[:3000]
    question = (data.get("question") or "").strip()[:600]
    if not selection and not question:
        return jsonify({"error": "先选中一段文字，或者写一个问题"}), 400

    workspace = _workspace()
    subs = settings_mod.load(settings_path=workspace.settings_path).get("assistant") or {}
    use_web = data.get("web")
    use_web = bool(subs.get("web_default", True)) if use_web is None else bool(use_web)
    # 篇幅档位：默认简洁（用户反馈：原来的 400-900 字里只有一段是回答所问的）
    mode = (data.get("mode") or subs.get("length_mode") or "concise").strip()
    # 连续追问：同一段选文下的多轮问答串成一个 thread；history 是之前的轮次
    thread = (data.get("thread") or "").strip()
    history = _clean_history(data.get("history"))

    ask_id = uuid.uuid4().hex[:12]
    job = {
        "id": ask_id, "dir": dir_name, "status": "running", "stage": "retrieve",
        "selection": selection, "question": question, "web": use_web, "mode": mode,
        "thread": thread, "history": history,
        "answer": "", "deltas": [], "sources": None, "error": "", "at": time.time(),
    }
    with _ASKS_LOCK:
        _ASKS[ask_id] = job
        if len(_ASKS) > _MAX_ASKS:                      # 丢掉最老的已结束项
            for old in sorted((j for j in _ASKS.values() if j["status"] != "running"),
                              key=lambda j: j["at"])[: max(0, len(_ASKS) - _MAX_ASKS)]:
                _ASKS.pop(old["id"], None)

    def log(msg: str) -> None:
        job.setdefault("logs", []).append(msg)

    def worker() -> None:
        try:
            # 1) 先把「依据」准备好并立刻发给界面：原文片段（可点回听）与网络结果
            from podcast_article import deepdive, websearch

            passages = deepdive.retrieve(base, _ask_query(selection, question), limit=6)
            web = None
            if use_web:
                job["stage"] = "search"
                # 联网用短关键词（并把**实际发出的那几个词**交给搜索层做相关性过滤：
                # 拿不到相关结果时宁可退化成「这次没联网」，也不要把无关网页喂给模型）
                terms = deepdive.query_terms(selection, question)
                web = websearch.search(" ".join(terms), limit=5, terms=terms)
            job["sources"] = {"passages": passages, "web": web}
            # 记忆：这位读者自己存下的背景（置顶的 + 与他这次选文/疑问相关的）。
            # 只当「判断他关注点」的背景注入，并且**回给界面** —— 「AI 记得我什么」必须
            # 是看得见的，否则记忆跑偏时用户无从发现，更无从纠正。
            try:
                memories = kb_mod.memory_for_prompt(_ask_query(selection, question), db_path=workspace.kb_path)
            except Exception:                 # 记忆坏了不该让提问失败
                memories = []
            mem_text = "\n".join(f"- {m['text']}" for m in memories)
            job["sources"]["memory"] = [m["text"] for m in memories]
            job["stage"] = "write"

            meta = {}
            try:
                meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}

            result = deepdive.stream_answer(
                workdir=base,
                selection=selection,
                question=question,
                title=meta.get("title") or dir_name,
                podcast=meta.get("podcast") or "",
                use_web=use_web,
                mode=mode,
                memory=mem_text,
                history=history,
                settings_path=workspace.settings_path,
                log=log,
                on_delta=lambda t: job["deltas"].append(t),
            )
            job["answer"] = result.get("answer") or ""
            if result.get("error"):
                job["error"] = result["error"]
            if not job["answer"] and not job["error"]:
                job["error"] = "模型没有返回内容"
            job["sources"] = {
                "passages": result.get("passages") or passages,
                "web": result.get("web") if result.get("web") is not None else web,
                "memory": job["sources"].get("memory") or [],     # 保留下发时那份，别被覆盖掉
            }
            if job["answer"]:
                saved = qa_store.append(base, selection=selection, question=question,
                                        answer=job["answer"],
                                        passages=job["sources"]["passages"],
                                        web=job["sources"]["web"], error=job["error"],
                                        thread=thread)
                job["thread"] = saved["thread"]           # 首轮时后端生成，回给前端继续用
            job["status"] = "error" if (job["error"] and not job["answer"]) else "done"
        except Exception as exc:                        # 任何意外都要变成一句话给用户
            job["status"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"[:300]
            job.setdefault("logs", []).append(f"[error] {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"id": ask_id, "web": use_web})


@app.get("/api/ask/<ask_id>/stream")
def api_ask_stream(ask_id: str):
    """SSE：sources（依据就绪）→ delta（逐段正文）→ done/error。"""
    def gen():
        sent = 0
        sent_sources = False
        while True:
            job = _ASKS.get(ask_id)
            if not job:
                yield _sse("error", {"error": "提问不存在或已过期"})
                return
            if job.get("sources") and not sent_sources:
                sent_sources = True
                yield _sse("sources", job["sources"])
            deltas = job.get("deltas") or []
            while sent < len(deltas):
                yield _sse("delta", {"text": deltas[sent]})
                sent += 1
            if job["status"] != "running":
                yield _sse("done", {"status": job["status"], "answer": job.get("answer") or "",
                                    "error": job.get("error") or "",
                                    "sources": job.get("sources")})
                return
            time.sleep(0.15)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/ask/<ask_id>")
def api_ask_get(ask_id: str):
    """轮询兜底（SSE 不可用时）。"""
    job = _ASKS.get(ask_id)
    if not job:
        return jsonify({"error": "提问不存在或已过期"}), 404
    return jsonify(_slim_ask(job))


@app.get("/api/qa")
def api_qa_list():
    """某一集的历史问答。

    GET /api/qa?dir=...               → 全部轮次（倒序）+ 最近一段会话的 id
    GET /api/qa?dir=...&thread=<id>   → 那一段会话的全部轮次（**正序**，供界面从上往下读）
    """
    dir_name = (request.args.get("dir") or "").strip()
    base = _safe_dir(dir_name)
    if not base:
        return jsonify({"error": "目录不存在"}), 404
    thread = (request.args.get("thread") or "").strip()
    if thread:
        return jsonify({"thread": thread, "turns": qa_store.load_thread(base, thread)})
    return jsonify({"items": qa_store.load(base), "max": qa_store.MAX_ITEMS,
                    "last_thread": qa_store.last_thread(base)})


@app.delete("/api/qa/<dir_name>/<item_id>")
@_readonly_guard
def api_qa_delete(dir_name: str, item_id: str):
    base = _safe_dir(dir_name)
    if not base:
        return jsonify({"error": "目录不存在"}), 404
    if not qa_store.remove(base, item_id):
        return jsonify({"error": "没有这条记录"}), 404
    return jsonify({"ok": True, "items": qa_store.load(base)})


@app.delete("/api/qa/<dir_name>")
@_readonly_guard
def api_qa_clear(dir_name: str):
    base = _safe_dir(dir_name)
    if not base:
        return jsonify({"error": "目录不存在"}), 404
    return jsonify({"removed": qa_store.clear(base)})


@app.get("/api/search-service")
def api_search_service():
    """联网搜索服务的状态（给设置页用）。"""
    from podcast_article import websearch

    return jsonify(websearch.available())


@app.post("/api/search-service/test")
@_readonly_guard
def api_search_service_test():
    """实测一次联网搜索。body: {"query": "..."}（可选）"""
    from podcast_article import websearch

    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip() or "DeepSeek"
    result = websearch.search(query, limit=3)
    return jsonify(result)


@app.get("/api/cover/<job_dir>")
def api_cover(job_dir: str):
    """这一集的本地封面图（生成时下载到 output/<dir>/cover.<ext>）。

    从**本地**发而不是让浏览器去热链图床：断网/防盗链都不该让卡片变成碎图。
    单集封面不会变，所以给一个长缓存。
    """
    workspace = _workspace()
    base = _safe_dir(job_dir, output_root=workspace.output_root)
    if not base:
        return jsonify({"error": "目录不存在"}), 404
    path = cover_mod.find(base)
    if not path:
        return jsonify({"error": "这一集没有封面"}), 404
    return send_file(path, mimetype=cover_mod.mimetype(path),
                     conditional=True, max_age=86400 * 30)


@app.post("/api/covers/backfill")
@_readonly_guard
def api_covers_backfill():
    """给「元信息里有封面链接、但本地还没下载」的历史单集补下封面。

    老文章是在这个功能之前生成的（当时只把封面 URL 存进了 meta.json），
    需要一次补齐；以后新生成的会自动下载。
    """
    workspace = _workspace()
    output_root = workspace.output_root
    filled, skipped, failed = [], [], []
    for d in sorted(output_root.iterdir()) if output_root.exists() else []:
        meta_file = d / "meta.json"
        if not d.is_dir() or not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if cover_mod.find(d):
            skipped.append(d.name)
            continue
        url = (meta.get("cover") or "").strip()
        if not url:
            failed.append({"dir": d.name, "why": "元信息里没有封面链接"})
            continue
        (filled if cover_mod.fetch(url, d) else failed).append(
            d.name if cover_mod.find(d) else {"dir": d.name, "why": "下载失败"})
    return jsonify({"filled": [x for x in filled if isinstance(x, str)],
                    "skipped": len(skipped), "failed": failed})


@app.get("/api/file/<job_dir>/<name>")
def api_file(job_dir: str, name: str):
    """安全地取输出目录里的文件（白名单）。"""
    if name not in _ALLOWED_FILES:
        return jsonify({"error": "不支持的文件"}), 400
    output_root = _workspace().output_root
    base = (output_root / job_dir).resolve()
    if not base.is_dir() or output_root.resolve() not in base.parents:
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


def _library_items(*, output_root: Path | None = None) -> list[dict]:
    """扫一遍输出目录，拼出历史库列表（按修改时间倒序）。"""
    output_root = Path(output_root) if output_root is not None else _workspace().output_root
    items: list[dict] = []
    if not output_root.exists():
        return items
    for d in sorted(output_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
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
            "source": meta.get("source") or "",
            "pub_date": meta.get("pub_date"),
            "url": meta.get("url") or "",
            "has_article": (d / "article.md").exists(),
            "has_transcript": (d / "transcript.txt").exists(),
            "has_audio": _audio_path(d) is not None or bool(meta.get("audio_object_key")),
            "has_cover": cover_mod.find(d) is not None,
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
    workspace = _workspace()
    if not query:
        return jsonify({"query": "", "count": 0, "items": [],
                        "stats": search_mod.stats(workspace.output_root)})
    items = search_mod.search(workspace.output_root, query, limit=limit)
    state = library_mod.snapshot(store_path=workspace.library_path)
    for it in items:
        it["category"] = state["assignments"].get(it["dir"])
        it["status"] = state["status"].get(it["dir"], library_mod.DEFAULT_STATUS)
    return jsonify({"query": query, "count": len(items), "items": items,
                    "stats": search_mod.stats(workspace.output_root)})


# ---------------------------------------------------------------- 阅读状态

@app.post("/api/status")
@_readonly_guard
def api_status():
    """设置阅读状态。body: {"dir": "...", "status": "unread|reading|read|later"}（空 = 清除）"""
    data = request.get_json(force=True, silent=True) or {}
    dir_name = (data.get("dir") or "").strip()
    workspace = _workspace()
    try:
        value = library_mod.set_status(dir_name, (data.get("status") or "").strip() or None,
                                       store_path=workspace.library_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"dir": dir_name, "value": value,
                    "state": library_mod.snapshot(store_path=workspace.library_path)})


# ---------------------------------------------------------------- 用量与费用

@app.get("/api/usage")
def api_usage():
    """累计 token 与费用（扫一遍所有 usage.json）+ 当前任务的实时用量。"""
    workspace = _workspace()
    live = usage_mod.current()
    return jsonify({
        "total": usage_mod.summary_over(workspace.output_root),
        "live": usage_mod.describe(live.usage) if live else None,
        "prices": usage_mod.MODEL_PRICES,
        "busy": _running_job_id() is not None,
    })


# ---------------------------------------------------------------- 导出

def _safe_dir(dir_name: str, *, output_root: Path | None = None) -> Path | None:
    """把目录名解析成输出根目录下的真实目录；越界或不存在返回 None。"""
    root = Path(output_root) if output_root is not None else _workspace().output_root
    base = (root / (dir_name or "")).resolve()
    if not dir_name or not base.is_dir() or root.resolve() not in base.parents:
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
    workspace = _workspace()
    if not _safe_dir(job_dir, output_root=workspace.output_root):
        return jsonify({"error": "目录不存在"}), 404
    try:
        name, payload, mime = export_mod.export_episode(workspace.output_root, job_dir, fmt)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    # 去掉 export 模块带的 charset：Flask 会再补一个，否则头里出现两次 charset=utf-8
    return Response(payload, mimetype=str(mime).split(";")[0].strip(),
                    headers=_attachment(name))


@app.get("/api/export")
def api_export_all():
    """整库导出 zip。GET /api/export?category=<id>&transcript=1"""
    category = (request.args.get("category") or "").strip()
    include_transcript = request.args.get("transcript", "1") != "0"
    workspace = _workspace()
    dirs = []
    state = library_mod.snapshot(store_path=workspace.library_path)
    for item in _library_items(output_root=workspace.output_root):
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
        info = export_mod.bundle(workspace.output_root, tmp, dirs=dirs,
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
@_readonly_guard
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
@_readonly_guard
def api_queue_delete(item_id: str):
    if not queue_mod.remove(item_id):
        return jsonify({"error": "队列里没有这一条"}), 404
    return jsonify(queue_mod.snapshot())


@app.post("/api/queue/<item_id>/move")
@_readonly_guard
def api_queue_move(item_id: str):
    data = request.get_json(force=True, silent=True) or {}
    try:
        pos = queue_mod.move(item_id, int(data.get("delta", -1)))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"position": pos, "queue": queue_mod.snapshot()})


@app.post("/api/queue/clear")
@_readonly_guard
def api_queue_clear():
    data = request.get_json(force=True, silent=True) or {}
    removed = queue_mod.clear(keep_failed=bool(data.get("keep_failed")))
    return jsonify({"removed": removed, "queue": queue_mod.snapshot()})


@app.post("/api/queue/retry")
@_readonly_guard
def api_queue_retry():
    n = queue_mod.retry_failed()
    return jsonify({"retried": n, "queue": queue_mod.snapshot()})


@app.post("/api/queue/run")
@_readonly_guard
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
    workspace = _workspace()
    snap = feeds_mod.snapshot(feeds_path=workspace.feeds_path)
    snap["subscriptions"] = settings_mod.load(settings_path=workspace.settings_path)["subscriptions"]
    return jsonify(snap)


@app.post("/api/feeds")
@_readonly_guard
def api_feeds_add():
    """订阅一个 feed。body: {"url", "auto": true, "backfill": 0}"""
    data = request.get_json(force=True, silent=True) or {}
    workspace = _workspace()
    try:
        entry = feeds_mod.add((data.get("url") or "").strip(),
                              auto=bool(data.get("auto", True)),
                              backfill=int(data.get("backfill") or 0),
                              feeds_path=workspace.feeds_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                       # 网络类异常也要给用户一句人话
        return jsonify({"error": f"订阅失败：{type(exc).__name__}: {exc}"[:300]}), 400
    # 首次订阅若要求补跑，立刻把 backfill 的那几集入队
    found = feeds_mod.check(entry["id"], feeds_path=workspace.feeds_path) if entry.get("backfill") else []
    added = _enqueue_feed_episodes(found) if found else []
    return jsonify({"feed": entry, "enqueued": len(added),
                    "feeds": feeds_mod.snapshot(feeds_path=workspace.feeds_path)})


@app.post("/api/feeds/discover")
@_readonly_guard
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
    workspace = _workspace()
    try:
        entry = feeds_mod.update(fid, feeds_path=workspace.feeds_path,
                                 **{k: v for k, v in data.items() if k in ("auto", "backfill", "title")})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"feed": entry, "feeds": feeds_mod.snapshot(feeds_path=workspace.feeds_path)})


@app.delete("/api/feeds/<fid>")
@_readonly_guard
def api_feeds_delete(fid: str):
    workspace = _workspace()
    try:
        feeds_mod.remove(fid, feeds_path=workspace.feeds_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(feeds_mod.snapshot(feeds_path=workspace.feeds_path))


@app.post("/api/feeds/check")
@_readonly_guard
def api_feeds_check():
    """立刻检查所有订阅。body: {"enqueue": true} 发现新单集时是否入队。"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(check_feeds_now(enqueue=bool(data.get("enqueue", True)), workspace=_workspace()))
    except Exception as exc:
        return jsonify({"error": f"检查失败：{type(exc).__name__}: {exc}"[:300]}), 400


@app.post("/api/feeds/settings")
@_readonly_guard
def api_feeds_settings():
    """保存订阅调度设置（存在 settings.json 的 subscriptions 段）。body: {...}"""
    data = request.get_json(force=True, silent=True) or {}
    saved = settings_mod.save(subscriptions=data, settings_path=_workspace().settings_path)
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


def check_feeds_now(*, enqueue: bool = True, workspace=None) -> dict:
    """检查所有订阅；发现新单集按设置决定是否自动入队。"""
    settings_path = workspace.settings_path if workspace else None
    feeds_path = workspace.feeds_path if workspace else None
    subs = settings_mod.load(settings_path=settings_path)["subscriptions"]
    found = feeds_mod.check(feeds_path=feeds_path)
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
        result["feeds"] = feeds_mod.snapshot(feeds_path=feeds_path)
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
    """后台循环：按间隔检查订阅 → 取队列里的任务跑完一条再跑下一条。

    注意条目状态的回写不在这里（见 _finish_queue_item）：手动开一条时这个循环
    可能压根没在跑，把回写挂在这里会让条目永远停在 running。
    """
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
        _wait_job(job_id)          # 等它跑完再取下一条（转写吃满 GPU，不并发）
        queue_mod.recover_running()   # 万一有别的 worker 崩了，别让条目卡住


def start_scheduler() -> bool:
    """启动后台调度线程（幂等）。测试与 CLI 里不会自动调用。"""
    global _scheduler_started
    if os.environ.get("PA_SCHEDULER", "1") == "0" or _scheduler_started:
        return False
    # 上次进程被杀时留下的 running 条目先归位，否则队列会被它堵住
    recovered = queue_mod.recover_running()
    if recovered:
        print(f"队列：回收了 {recovered} 条上次中断的任务，已重新排队")

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
