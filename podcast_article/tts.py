"""把文章读出来：清洗文本 → 分块 → 调 TTS 接口 → 落盘成可播放的音频。

**为什么要分块**：几乎所有 TTS 接口都有单次字数上限（4096 字符是常见值），长文一次
发过去直接被拒；分块之后每块还能单独重试 —— 一篇 6000 字的文章读到第 8 块时网络抖一
下，不用把前面 7 块（已经花过钱的）重来一遍。块大小默认 700 字。

**为什么按内容哈希缓存**：TTS 按字符计费，改一个字就整篇重读是最贵的用法。这里把
「清洗后的正文 + provider + model + voice + speed」一起哈希，只有真的变了才重读。

支持的后端（`provider`）：
- `macos`：系统自带的 `say`。**零配置、离线、免费**，中文音色有 Tingting / Sinji 等；
  直接输出 m4a（AAC），浏览器能播，连 ffmpeg 都不需要。只有 macOS 能用。
- `openai`：任何**兼容 OpenAI `/v1/audio/speech`** 的服务。填 base_url 就能接
  OpenAI、硅基流动、Groq、DeepInfra、各家中转站 —— 这是覆盖面最广的一种。
- `off`：关掉（默认）。

多块之间如果有 ffmpeg，会再拼成一个整篇文件（`article.<ext>`），这样前端能用一个
`<audio>` 直接从任意位置拖进度；没有 ffmpeg 就按块连播（前端支持）。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests

PROVIDERS = ("off", "macos", "openai")
DEFAULTS = {
    "provider": "off",
    "base_url": "",
    "model": "",
    "voice": "",
    "speed": 1.0,
    "format": "mp3",       # 仅 openai 用：response_format
    "chunk_chars": 700,    # 每块字数（接口上限普遍 4096，留足余量）
}

TTS_DIR = "tts"
INDEX = "index.json"
TIMEOUT = 180          # 单块最长等待（长句 + 慢接口）
PREVIEW_TEXT = "这是 podcast-article 的朗读试听。如果你听到这句话，说明语音接口配置可用。"


# ---------------------------------------------------------------- 文本清洗

_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_CODE = re.compile(r"```.*?```", re.S)
_MD_INLINE_CODE = re.compile(r"`([^`]*)`")
# 加粗可能跨行（实测有 `**三、…\n…**` 这种），所以强调整体用 DOTALL；
# 单个 `*` 斜体另外处理：只匹配较短的跨度，避免把两处无关的 `*` 中间大段文字当成一体。
# 单个 `_` 刻意不处理 —— 会误伤 foo_bar 这类下划线。
_MD_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_MD_EM = re.compile(r"\*(?=\S)([^*\n]{1,200}?)(?<=\S)\*")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_MD_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)、])\s+", re.M)
_MD_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.M)
_MD_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.M)
_MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_TS = re.compile(r"[\[［【]\d{1,2}:\d{2}(?::\d{2})?(?:\s*[-–—~～至到]\s*\d{1,2}:\d{2}(?::\d{2})?)?[\]］】]")
_WS = re.compile(r"[ \t\u3000]+")
_MULTI_NL = re.compile(r"\n{2,}")


def prepare_text(markdown: str) -> str:
    """把 markdown 变成「适合念出来」的纯文本。

    去掉的都是念出来只会添乱的东西：代码块、表格、链接地址、强调符号、时间戳。
    表格里的数据在正文里通常已经叙述过，念一遍表格反而难听懂。
    """
    text = str(markdown or "")
    text = _MD_CODE.sub("\n", text)             # 代码块整段跳过
    text = _MD_IMG.sub("", text)
    text = _MD_LINK.sub(r"\1", text)            # 链接只留文字
    text = _TS.sub("", text)                    # 时间戳不用念
    text = _MD_TABLE_SEP.sub("", text)
    text = _MD_TABLE_ROW.sub("", text)
    text = _MD_RULE.sub("", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_QUOTE.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    text = _MD_STRONG.sub(r"\2", text)
    text = _MD_EM.sub(r"\1", text)
    # 模型偶尔写出配不成对的强调符号（实测有 `**四、…` 这样只有开头没有结尾的）：
    # 成对的上面已经处理掉，剩下的 `**` 一定是噪音；行首的单个 `*` 是列表残留
    text = text.replace("**", "")
    text = re.sub(r"(?m)^\s*\*\s*", "", text)
    text = text.replace("|", " ")
    lines = [_WS.sub(" ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = _MULTI_NL.sub("\n", text)
    return text.strip()


# ------------------------------------------------------------------ 分块

_SENT_END = re.compile(r"(?<=[。！？!?；;…])")


def chunk_text(text: str, limit: int = 700) -> list[str]:
    """按段落优先、句子其次切块；单句超长时按逗号硬切。"""
    limit = max(120, int(limit or 700))
    chunks: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for para in text.split("\n"):
        para = para.strip()
        if not para:
            flush()
            continue
        if len(para) <= limit:
            if len(buf) + len(para) + 1 > limit:
                flush()
            buf = f"{buf}\n{para}" if buf else para
            continue
        # 超长段落：拆句子
        flush()
        for sent in _SENT_END.split(para):
            sent = sent.strip()
            if not sent:
                continue
            while len(sent) > limit:            # 单句还是太长（少见）：按逗号再切
                cut = max(sent.rfind("，", 0, limit), sent.rfind(",", 0, limit), sent.rfind(" ", 0, limit))
                # 逗号太靠前就不按逗号切（会切出很多碎片），否则连逗号一起切走。
                # 注意切出来的长度必须 <= limit：`[:cut + 1]` 配 `cut = limit` 会多一个字。
                take = cut + 1 if cut >= limit // 2 else limit
                chunks.append(sent[:take].strip())
                sent = sent[take:].strip()
            if len(buf) + len(sent) > limit:
                flush()
            buf = f"{buf}{sent}" if buf else sent
    flush()
    return chunks


# ---------------------------------------------------------------- 缓存键

def text_sha(text: str, cfg: dict) -> str:
    """内容 + 音色参数的联合指纹：任何一个变了就重读。"""
    raw = "\u0000".join([
        str(text),
        str(cfg.get("provider") or ""),
        str(cfg.get("model") or ""),
        str(cfg.get("voice") or ""),
        str(cfg.get("speed") or ""),
        str(cfg.get("format") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 各后端

def macos_voices() -> list[str]:
    """系统里可用的中文音色（给设置页做提示用）。"""
    if not shutil.which("say"):
        return []
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    names = []
    for line in out.splitlines():
        if "zh_CN" in line or "zh_TW" in line or "zh_HK" in line:
            name = line.split("(")[0].strip().split()[0] if line.strip() else ""
            if name and name not in names:
                names.append(name)
    return names


def _synth_macos(text: str, cfg: dict, out: Path, log) -> None:
    """用系统 say 生成 m4a/AAC —— 浏览器直接能播，不需要 ffmpeg。"""
    if not shutil.which("say"):
        raise RuntimeError("这台机器上没有 macOS 的 say 命令（macos 后端只能在 macOS 上用）")
    voice = (cfg.get("voice") or "").strip()
    try:
        speed = float(cfg.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    # say 的速度单位是「词/分钟」，中文按默认 180 折算
    rate = max(80, min(400, int(180 * speed)))
    cmd = ["say", "-o", str(out), "--file-format=m4af", "--data-format=aac", "-r", str(rate)]
    if voice:
        cmd += ["-v", voice]
    proc = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=TIMEOUT)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"say 失败：{(proc.stderr or proc.stdout or '').strip()[:200] or '没有输出'}")


def _synth_openai(text: str, cfg: dict, api_key: str, out: Path, log) -> None:
    """任何兼容 OpenAI /v1/audio/speech 的服务。"""
    base = (cfg.get("base_url") or "https://api.openai.com/v1").strip().rstrip("/")
    if not base.endswith("/v1") and not re.search(r"/v\d", base):
        base += "/v1"
    if not api_key:
        raise RuntimeError("没有配语音接口的 API key（设置 → 朗读）")
    payload = {
        "model": (cfg.get("model") or "tts-1").strip(),
        "input": text,
        "voice": (cfg.get("voice") or "alloy").strip(),
        "response_format": (cfg.get("format") or "mp3").strip(),
    }
    try:
        speed = float(cfg.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    if abs(speed - 1.0) > 0.01:
        payload["speed"] = max(0.25, min(4.0, speed))
    resp = requests.post(
        f"{base}/audio/speech",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload, timeout=TIMEOUT, stream=True,
    )
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.text[:200]
        except Exception:
            pass
        raise RuntimeError(f"语音接口返回 {resp.status_code}：{detail or resp.reason}")
    with out.open("wb") as fh:
        for block in resp.iter_content(chunk_size=64 * 1024):
            if block:
                fh.write(block)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("语音接口返回了空文件")


def synthesize_chunk(text: str, cfg: dict, api_key: str, out: Path, log=print) -> None:
    provider = (cfg.get("provider") or "off").strip()
    if provider == "macos":
        _synth_macos(text, cfg, out, log)
    elif provider == "openai":
        _synth_openai(text, cfg, api_key, out, log)
    else:
        raise RuntimeError(f"没有启用朗读后端（provider={provider}）")


def ext_for(cfg: dict) -> str:
    if (cfg.get("provider") or "") == "macos":
        return "m4a"
    fmt = (cfg.get("format") or "mp3").strip().lower()
    return "wav" if fmt == "wav" else ("opus" if fmt in ("opus", "ogg") else "mp3")


# ------------------------------------------------------------ 整篇生成

def tts_dir(workdir: Path) -> Path:
    return Path(workdir) / TTS_DIR


def load_index(workdir: Path) -> dict | None:
    p = tts_dir(workdir) / INDEX
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_index(workdir: Path, data: dict) -> None:
    d = tts_dir(workdir)
    d.mkdir(parents=True, exist_ok=True)
    (d / INDEX).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def merged_path(workdir: Path) -> Path | None:
    """拼好的整篇文件（有 ffmpeg 才有）。"""
    idx = load_index(workdir)
    if not idx or not idx.get("merged"):
        p = tts_dir(workdir) / f"article.{idx.get('ext', 'mp3')}" if idx else None
        return p if p and p.exists() else None
    p = tts_dir(workdir) / f"article.{idx.get('ext', 'mp3')}"
    return p if p.exists() else None


def is_fresh(workdir: Path, text: str, cfg: dict) -> bool:
    idx = load_index(workdir)
    if not idx or not idx.get("chunks"):
        return False
    if idx.get("text_sha") != text_sha(text, cfg):
        return False
    return all((tts_dir(workdir) / c["file"]).exists() for c in idx["chunks"])


def merge_chunks(workdir: Path, idx: dict, log=print) -> bool:
    """把各块拼成一个文件（这样前端能用一个 <audio> 直接拖进度）。没 ffmpeg 就跳过。"""
    ext = idx.get("ext", "mp3")
    files = [tts_dir(workdir) / c["file"] for c in idx["chunks"]]
    if len(files) < 2 or not shutil.which("ffmpeg"):
        return False
    out = tts_dir(workdir) / f"article.{ext}"
    listing = tts_dir(workdir) / "concat.txt"
    listing.write_text("".join(f"file '{f.name}'\n" for f in files), encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
           "-safe", "0", "-i", str(listing), "-c", "copy", str(out)]
    try:
        proc = subprocess.run(cmd, cwd=str(tts_dir(workdir)), capture_output=True, text=True, timeout=600)
    except Exception as exc:
        log(f"[tts] 拼接失败（忽略，仍然按块播放）：{exc}")
        return False
    finally:
        listing.unlink(missing_ok=True)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        log(f"[tts] 拼接失败（忽略，仍然按块播放）：{(proc.stderr or '').strip()[:160]}")
        return False
    return True


def synthesize(workdir: Path, markdown: str, cfg: dict, *, api_key: str = "",
               progress=None, log=print, force: bool = False) -> dict:
    """把一篇文章读成音频。返回 index（含 chunks 列表）。

    progress(done, total, note)：每块前后各回调一次，供界面显示进度。
    """
    workdir = Path(workdir)
    provider = (cfg.get("provider") or "off").strip()
    if provider not in PROVIDERS or provider == "off":
        raise RuntimeError("还没有启用朗读：设置 → 朗读，选一个语音后端（macOS 本地 / OpenAI 兼容接口）")

    text = prepare_text(markdown)
    if len(text) < 20:
        raise RuntimeError("这篇正文太短，没什么可读的")

    if not force and is_fresh(workdir, text, cfg):
        idx = load_index(workdir)
        log(f"[tts] 复用已生成的朗读（{len(idx['chunks'])} 块，音色未变）")
        if progress:
            progress(len(idx["chunks"]), len(idx["chunks"]), "复用已有音频")
        return idx

    chunks = chunk_text(text, int(cfg.get("chunk_chars") or 700))
    ext = ext_for(cfg)
    d = tts_dir(workdir)
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*"):                 # 换音色 / 换内容：清掉旧块，免得混在一起
        if old.name != INDEX:
            old.unlink(missing_ok=True)

    total = len(chunks)
    log(f"[tts] 开始朗读：{provider}，{total} 块，共 {len(text)} 字"
        + (f"，音色 {cfg.get('voice') or '默认'}" if cfg.get("voice") else ""))
    made: list[dict] = []
    for i, part in enumerate(chunks):
        name = f"{i:03d}.{ext}"
        out = d / name
        if progress:
            progress(i, total, f"第 {i + 1}/{total} 块")
        t0 = time.time()
        synthesize_chunk(part, cfg, api_key, out, log)
        made.append({"file": name, "chars": len(part), "bytes": out.stat().st_size,
                     "seconds": round(time.time() - t0, 1)})
        log(f"[tts] {i + 1}/{total} 完成（{len(part)} 字，{made[-1]['bytes'] // 1024}KB，"
            f"{made[-1]['seconds']}s）")

    idx = {
        "provider": provider,
        "model": cfg.get("model") or "",
        "voice": cfg.get("voice") or "",
        "speed": cfg.get("speed") or 1.0,
        "format": cfg.get("format") or "",
        "ext": ext,
        "chars": len(text),
        "text_sha": text_sha(text, cfg),
        "chunks": made,
        "created_at": round(time.time(), 1),
        "merged": False,
        "seconds": None,
    }
    idx["merged"] = merge_chunks(workdir, idx, log)
    if idx["merged"] and shutil.which("ffprobe"):
        try:
            idx["seconds"] = _probe_seconds(d / f"article.{ext}")
        except Exception:
            pass
    _save_index(workdir, idx)
    if idx["merged"]:
        log(f"[tts] 已拼成整篇：article.{ext}"
            + (f"（约 {int(idx['seconds'] // 60)} 分 {int(idx['seconds'] % 60)} 秒）" if idx.get("seconds") else ""))
    if progress:
        progress(total, total, "完成")
    return idx


def _probe_seconds(path: Path) -> float | None:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()
    return round(float(out), 1) if out else None


def remove(workdir: Path) -> bool:
    d = tts_dir(workdir)
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


def preview(cfg: dict, api_key: str, dest: Path, log=print) -> dict:
    """试听：用当前设置读一句短话，返回 {ok, path, seconds} 或抛异常。"""
    ext = ext_for(cfg)
    out = dest.with_suffix("." + ext)
    synthesize_chunk(PREVIEW_TEXT, cfg, api_key, out, log)
    seconds = None
    if shutil.which("ffprobe"):
        try:
            seconds = _probe_seconds(out)
        except Exception:
            seconds = None
    return {"ok": True, "path": out, "seconds": seconds, "bytes": out.stat().st_size}
