const $ = (id) => document.getElementById(id);
let es = null, pollTimer = null, jobId = null, curWorkdir = null, curUrl = null, shownLogs = 0;
const STAGE_PREFIX = ["[meta]", "[audio]", "[text]", "[write]"];
let runStartedAt = null;
const stageNotes = {};   // 阶段序号 -> 运行过程中最有信息量的一句细节

/** 只记录有信息量的细节（"完成"这类占位不算） */
function noteStage(i, text) {
  const t = (text || "").trim();
  if (t && t !== "完成") stageNotes[i] = t;
}

const fmtDur = (ms) => {
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, "0")} 秒`;
};

/** 跑完后收起成一行摘要：保留"这次花了多久、多大"，但不占地方 */
function collapseRunview(errorText) {
  const details = [0, 1, 2, 3].map((i) => stageNotes[i] || "").filter(Boolean);
  const elapsed = runStartedAt ? fmtDur(Date.now() - runStartedAt) : "";
  const head = errorText
    ? `<span class="ok bad">✕ 失败</span>`
    : `<span class="ok">✓ 已完成</span>`;
  const parts = [elapsed ? `用时 ${elapsed}` : "", ...details].filter(Boolean);
  $("runsum").innerHTML = `${head}<span class="txt">${esc(parts.join(" · "))}</span><span class="arw">▸ 展开</span>`;
  $("runsum").classList.add("show");
  $("runbody").style.display = "none";
}

function expandRunview() {
  $("runsum").classList.remove("show");
  $("runbody").style.display = "block";
}

function toggleRunview() {
  if ($("runbody").style.display === "none") expandRunview();
  else collapseRunview();
}

const esc = (s) => { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; };
const mb = (b) => (b / 1048576).toFixed(1);
const mmss = (s) => { s = Math.round(s); const m = Math.floor(s / 60); return m ? `${m}分${String(s % 60).padStart(2, "0")}秒` : `${s}秒`; };
const humanDur = (sec) => { if (!sec) return ""; const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60); return h ? `${h}小时${m}分` : `${m}分钟`; };
let toastTimer = null;
function toast(html) {
  $("toast").innerHTML = html; $("toast").classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").classList.remove("show"), 6000);
}

/** 从一段文本里抠出所有链接（用来判断用户是不是一次粘了多条） */
const URL_RE = /(?:https?:\/\/|~?\/)[^\s,，、]+/g;
function urlsIn(text) {
  return [...new Set((text || "").match(URL_RE) || [])];
}

/** 首页提交：一条直接跑，多条自动改成「加入队列」 */
async function startRun(opts = {}) {
  const raw = opts.url !== undefined ? opts.url : $("url").value.trim();
  if (!raw) { $("url").focus(); return; }
  const many = urlsIn(raw);
  if (!opts.url && many.length > 1) { await enqueueLinks(many); return; }
  const url = raw;
  const resp = await fetch("/api/run", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url, lang: $("lang").value, model: $("model").value.trim(), mode: $("mode").value,
      no_subs: $("no_subs").checked,
      force_transcript: opts.force_transcript ?? $("ft").checked,
      force_article: opts.force_article ?? $("fa").checked,
    }),
  });
  const data = await resp.json();
  if (!resp.ok) {
    // 409 = 已有任务在跑。别只说"失败"，告诉用户在跑什么，并自动等它结束
    if (resp.status === 409 && data.busy) {
      toast("⚠ 已有任务在运行：" + esc((data.url || "").slice(0, 60)) + " —— 结束后会自动恢复");
      if (data.job_id) beginJob(data.job_id);
      startBusyWatch();
      return;
    }
    toast("⚠ " + esc(data.error || "启动失败"));
    return;
  }
  curUrl = url;
  beginJob(data.job_id);
}

/** 一次粘了多条链接：全部排进队列，然后切到队列视图 */
async function enqueueLinks(links) {
  const resp = await fetch("/api/queue", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      urls: links, mode: $("mode").value, lang: $("lang").value,
      model: $("model").value.trim(), no_subs: $("no_subs").checked,
    }),
  });
  const d = await resp.json();
  if (!resp.ok) { toast("⚠ " + esc(d.error || "入队失败")); return; }
  $("url").value = "";
  updateComposerHint();
  toast(`✦ 已排入队列 ${d.added.length} 条，后台会依次生成`);
  showView("queue");
  renderQueueView(d.queue);
  queueRun();          // 立刻开跑第一条（有任务在跑时会返回 409，忽略即可）
}

/* ---- 「已有任务在跑」的提示与自动恢复 ---- */
let busyTimer = null;
function startBusyWatch() {
  if (busyTimer) return;
  const tick = async () => {
    try {
      const d = await (await fetch("/api/jobs/current")).json();
      $("go").disabled = d.status === "running";
      $("go").textContent = d.status === "running" ? "有任务在跑…" : "生成文章";
      if (d.status !== "running") {
        clearInterval(busyTimer); busyTimer = null;
        toast("✦ 之前的任务已结束，可以继续了");
      }
    } catch (e) { /* 服务不可用时静默 */ }
  };
  tick();
  busyTimer = setInterval(tick, 3000);
}

function updateComposerHint() {
  const n = urlsIn($("url").value).length;
  const btn = $("go");
  if (n > 1) {
    btn.textContent = `加入队列 (${n})`;
    $("hint").innerHTML = `检测到 ${n} 条链接 —— 点按钮会全部排队，后台依次跑完；不想排队就只留一条。`;
  } else {
    btn.textContent = "生成文章";
    $("hint").innerHTML = "⌘/Ctrl + Enter 直接开始 · Esc 关闭文章 / 退出设置 · 产物会缓存，重新生成文章不必重新转写";
  }
}

function beginJob(id) {
  jobId = id; shownLogs = 0; curWorkdir = null;
  $("console").textContent = ""; $("logdetails").open = false;
  $("runview").classList.add("show");
  runStartedAt = Date.now();
  Object.keys(stageNotes).forEach((k) => delete stageNotes[k]);
  expandRunview();
  $("result").classList.remove("show");
  $("errline").style.display = "none";
  curUsage = null; renderCostPill();
  setStage(-1, null);
  $("go").disabled = true; $("go").textContent = "生成中…";
  shownLogs = 0; $("console").innerHTML = "";
  connectSSE(id);
  $("runview").scrollIntoView({ behavior: "smooth", block: "start" });
}

function setStage(active, progress) {
  for (let i = 0; i < 4; i++) {
    const el = $("st" + i);
    const isActive = i === active && active < 4;
    const isDone = active === 4 || i < active;
    el.classList.toggle("active", isActive);
    el.classList.toggle("done", isDone);
    el.querySelector(".ico").textContent = isDone ? "✓" : i + 1;
    const bar = el.querySelector(".bar"), fill = el.querySelector(".bar i");
    const detail = el.querySelector(".detail");
    bar.classList.remove("indet");
    if (isDone) {
      // 已完成：优先显示运行中记下的实质细节（如"211.3 MB"），没有才写"完成"
      fill.style.width = "100%";
      detail.textContent = stageNotes[i] || "完成";
      bar.classList.remove("indet");
    }
    else if (isActive && progress && progress.stage === ["", "download", "asr", "llm"][i]) {
      detail.textContent = detailText(progress);
      noteStage(i, detail.textContent);
      if (progress.pct !== undefined && progress.pct !== null) { fill.style.width = progress.pct + "%"; }
      else { bar.classList.add("indet"); fill.style.width = "30%"; }
    } else if (isActive) {
      detail.textContent = ""; fill.style.width = progress && progress.pct != null ? progress.pct + "%" : "0%";
    } else { detail.textContent = ""; fill.style.width = "0%"; }
  }
}

function detailText(p) {
  if (p.stage === "download" && p.total) return `${mb(p.downloaded)} / ${mb(p.total)} MB · ${p.pct}%`;
  if (p.stage === "asr") {
    let s = p.pct != null ? `${p.pct}%` : "";
    if (p.eta_s != null) s += ` · 剩余 ${mmss(p.eta_s)}`;
    return s;
  }
  if (p.stage === "llm") {
    if (p.chunk) return `第 ${p.chunk}/${p.total} 段` + (p.chars ? ` · 已生成 ${p.chars.toLocaleString()} 字` : "");
    return p.chars ? `已生成 ${p.chars.toLocaleString()} 字` : "";
  }
  return "";
}

function stageIndexFromLogs(logs) {
  const seen = STAGE_PREFIX.map((p) => logs.some((l) => l.startsWith(p)));
  if (logs.some((l) => l.startsWith("[done]"))) return 4;
  return seen.lastIndexOf(true);
}

function appendLog(line) {
  const div = document.createElement("div");
  div.className = "ln" + (line.startsWith("[error]") ? " err" : line.startsWith("[done]") ? " ok" : "");
  div.innerHTML = esc(line);
  $("console").appendChild(div);
  $("console").scrollTop = $("console").scrollHeight;
}

function connectSSE(id) {
  if (es) es.close();
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  es = new EventSource("/api/stream/" + id);
  es.addEventListener("log", (e) => appendLog(JSON.parse(e.data).line));
  es.addEventListener("progress", (e) => {
    const p = JSON.parse(e.data);
    const order = { download: 1, asr: 2, llm: 3 };
    const fromLogs = stageIndexFromLogs([...$("console").children].map((c) => c.textContent));
    const stageIdx = Math.max(order[p.stage] ?? 0, Math.min(fromLogs, 2));
    setStage(stageIdx, p);
  });
  es.addEventListener("usage", (e) => {
    // 实时花费：每完成一次 LLM 调用就会推一次
    curUsage = JSON.parse(e.data);
    renderCostPill();
  });
  es.addEventListener("status", (e) => {
    const d = JSON.parse(e.data);
    es.close(); es = null;
    finishJob(d);
  });
  es.onerror = () => {
    // SSE 失败时回退到轮询
    if (es) { es.close(); es = null; }
    if (!pollTimer && jobId) pollTimer = setInterval(pollJob, 800);
  };
}

async function pollJob() {
  if (!jobId) return;
  const resp = await fetch("/api/job/" + jobId);
  if (!resp.ok) return;
  const job = await resp.json();
  (job.logs || []).slice(shownLogs).forEach((l) => { appendLog(l); shownLogs++; });
  if (job.progress) {
    const order = { download: 1, asr: 2, llm: 3 };
    const fromLogs = stageIndexFromLogs(job.logs || []);
    setStage(Math.max(order[job.progress.stage] ?? 0, Math.min(fromLogs, 2)), job.progress);
  }
  if (job.usage) { curUsage = job.usage; renderCostPill(); }
  if (job.status !== "running") {
    clearInterval(pollTimer); pollTimer = null;
    finishJob({ status: job.status, error: job.error, workdir: job.workdir, meta: job.meta,
                usage: job.usage, article_html: job.article_html });
  }
}

function finishJob(d) {
  $("go").disabled = false;
  updateComposerHint();
  if (d.status === "done") {
    setStage(4, null);
    curWorkdir = d.workdir;
    const m = d.meta || {};
    $("rmeta").innerHTML =
      `<span class="pod">${esc(m.podcast || "")}</span><span>${esc(m.title || "")}</span>` +
      (m.duration ? `<span>${humanDur(m.duration)}</span>` : "");
    $("article").innerHTML = d.article_html || "";
    $("transcript").textContent = ""; $("transcript").dataset.loaded = "";
    $("transcript").classList.remove("show"); $("result").classList.remove("withTranscript", "wide");
    $("tbtn").textContent = "查看文字稿";
    $("nbtn").disabled = false;
    $("result").dataset.fromLib = "";
    $("result").classList.add("show");
    $("result").scrollIntoView({ behavior: "smooth", block: "start" });
    curUsage = d.usage || null;
    renderCostPill();
    syncReadButton();
    loadLibrary();
    collapseRunview();
  } else {
    setStage(-1, null);
    $("errline").textContent = "✕ " + (d.error || "任务失败");
    $("errline").style.display = "block";
    collapseRunview(d.error || "任务失败");
  }
}

/* ---- 费用显示 ---- */
let curUsage = null;

const fmtTokens = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n || 0));
const fmtCost = (c) => (c >= 1 ? c.toFixed(2) : c.toFixed(3)) + " 元";

function renderCostPill() {
  const el = $("costpill");
  if (!el) return;
  if (!curUsage || !curUsage.calls) { el.textContent = ""; return; }
  const hit = curUsage.cache_hit_rate ? ` · 缓存命中 ${curUsage.cache_hit_rate}%` : "";
  el.textContent = `≈ ${fmtCost(curUsage.cost_cny)} · ${fmtTokens(curUsage.total_tokens)} tokens · ${curUsage.calls} 次调用${hit}`;
  el.title = `输入 ${curUsage.input_tokens.toLocaleString()} tokens（其中缓存命中 ${curUsage.hit_tokens.toLocaleString()}）\n` +
             `输出 ${curUsage.out_tokens.toLocaleString()} tokens\n` +
             `按 DeepSeek 官方价格表估算，分高峰/空闲时段计价`;
}

async function toggleTranscript() {
  const el = $("transcript");
  if (el.classList.contains("show")) {
    el.classList.remove("show"); $("result").classList.remove("withTranscript", "wide");
    $("tbtn").textContent = "查看文字稿"; return;
  }
  if (!el.dataset.loaded && curWorkdir) {
    const resp = await fetch(`/api/file/${encodeURIComponent(curWorkdir)}/transcript.txt`);
    el.innerHTML = renderTranscript(await resp.text());
    el.dataset.loaded = "1";
  }
  el.classList.add("show"); $("result").classList.add("withTranscript", "wide");
  $("tbtn").textContent = "收起文字稿";
}

function regenArticle() {
  if (!curUrl) { toast("⚠ 请从输入框重新提交链接"); return; }
  startRun({ url: curUrl, force_article: true });
}

async function pushArticle() {
  if (!curWorkdir) return;
  const btn = $("nbtn"), target = $("dest").value;
  btn.disabled = true; btn.textContent = "发布中…";
  try {
    const body = { dir: curWorkdir, target };
    if (target.startsWith("mcp:")) body.template = $("tpl").value;
    const resp = await fetch("/api/publish", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) toast("⚠ " + esc(data.error || "发布失败"));
    else if (data.url) toast(`✦ 已发布（${esc(data.via)}）· <a href="${esc(data.url)}" target="_blank">打开页面</a>`);
    else toast(`✦ 已发布（${esc(data.via)}）· ${esc((data.text || "").slice(0, 120))}`);
  } catch (e) { toast("⚠ " + esc(String(e))); }
  btn.disabled = false; btn.textContent = "✦ 发布";
}

/* ---------------- 设置 ---------------- */
const SECRET_KEYS = ["DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "NOTION_TOKEN", "NOTION_DATABASE_ID", "NOTION_PARENT_PAGE_ID"];

async function openSettings() {
  $("main").style.display = "none";
  $("settings").style.display = "block";
  window.scrollTo({ top: 0 });
  await loadSettings();
  loadMcp();
}

function closeSettings() {
  $("settings").style.display = "none";
  $("main").style.display = "block";
  window.scrollTo({ top: 0 });
  loadLibrary();
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.setAttribute("data-active", String(t.dataset.tab === name)));
  document.querySelectorAll(".pane").forEach((p) => { p.style.display = p.id === "pane-" + name ? "block" : "none"; });
  if (name === "mcp") loadMcp();
}

const markDirty = () => $("savebar").classList.add("dirty");
const clearDirty = () => $("savebar").classList.remove("dirty");

async function loadSettings() {
  const d = await (await fetch("/api/settings")).json();
  const p = d.profile || {}, g = d.generation || {}, s = d.storage || {};
  $("s_name").value = p.name || "";
  $("s_interests").value = p.interests || "";
  $("s_plang").value = p.language || "zh";
  SECRET_KEYS.forEach((k) => { const el = $("k_" + k); if (el) el.value = ""; });
  for (const [k, v] of Object.entries(d.secrets || {})) {
    const tag = $("st_" + k);
    if (!tag) continue;
    tag.textContent = v.configured ? "已配置 " + v.masked : "未配置";
    tag.className = "fstate" + (v.configured ? " ok" : "");
  }
  $("g_language").value = g.language || "";
  $("g_backend").value = g.backend || "mlx";
  $("g_asr_model").value = g.asr_model || "";
  $("g_max_chars").value = g.max_chars || 75000;
  $("g_mode").value = g.length_mode || "standard";
  $("g_llm").value = g.llm_model || "deepseek-flash";
  $("g_polish").checked = g.auto_polish !== false;
  $("g_no_subs").checked = !!g.no_subs;
  $("g_models").textContent = (s.models || []).join("、") || "（未检测到本地模型）";
  $("storagerows").innerHTML = [
    ["输出目录", s.output_dir], ["已生成", `${s.episodes} 集 · ${s.size_mb} MB`],
    ["本地模型", (s.models || []).join("、") || "—"],
    ["设置文件", s.settings_path], ["环境变量", s.env_path],
  ].map(([k, v]) => `<div class="inforow"><span class="k">${esc(k)}</span><span class="v">${esc(String(v))}</span></div>`).join("");
  applyDefaultsToComposer(g);
  clearDirty();
}

function applyDefaultsToComposer(g) {
  if (!g) return;
  $("lang").value = g.language || "auto";
  $("model").value = g.asr_model || "";
  $("no_subs").checked = !!g.no_subs;
  $("mode").value = g.length_mode || "standard";
}

async function saveSettings() {
  const payload = {
    profile: {
      name: $("s_name").value.trim(),
      interests: $("s_interests").value.trim(),
      language: $("s_plang").value,
    },
    generation: {
      language: $("g_language").value,
      backend: $("g_backend").value,
      asr_model: $("g_asr_model").value.trim(),
      max_chars: parseInt($("g_max_chars").value, 10) || 75000,
      no_subs: $("g_no_subs").checked,
      length_mode: $("g_mode").value,
      llm_model: $("g_llm").value,
      auto_polish: $("g_polish").checked,
    },
    secrets: {},
  };
  // 密钥留空 = 不改动，因此只提交真正输入的字段
  SECRET_KEYS.forEach((k) => { const el = $("k_" + k); if (el && el.value.trim()) payload.secrets[k] = el.value.trim(); });
  const resp = await fetch("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  const d = await resp.json();
  if (!resp.ok) { toast("⚠ " + esc(d.error || "保存失败")); return; }
  await loadSettings();
  toast(d.env_changed && d.env_changed.length
    ? `✦ 已保存，并写回 .env：${esc(d.env_changed.join("、"))}`
    : "✦ 已保存");
}

async function verifyKey(what, btn) {
  const out = $("v_" + what), keyName = what === "deepseek" ? "DEEPSEEK_API_KEY" : "NOTION_TOKEN";
  const typed = ($("k_" + keyName).value || "").trim();
  btn.disabled = true;
  out.className = "vres"; out.textContent = "检测中…";
  try {
    // 先落盘再检测：否则测试的是旧密钥，会误导
    if (typed) {
      await fetch("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ secrets: { [keyName]: typed } }),
      });
      await loadSettings();
      btn.disabled = true;
    }
    const resp = await fetch("/api/settings/verify", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ what }),
    });
    const d = await resp.json();
    out.className = "vres " + (d.ok ? "ok" : "bad");
    out.textContent = (d.ok ? "✓ " : "✕ ") + (d.detail || d.error || "");
  } catch (e) {
    out.className = "vres bad"; out.textContent = "✕ " + String(e);
  }
  btn.disabled = false;
}

/* ---------------- 时间戳回听（本地音频）---------------- */
let audioDir = null;
const pa = () => $("paudio");

const fmtClock = (sec) => {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(ss).padStart(2, "0");
};

function episodeTitle(dir) {
  const it = libItems.find((i) => i.dir === dir);
  if (it && it.title) return it.title;
  const meta = $("rmeta") ? $("rmeta").textContent.replace(/\s+/g, " ").trim() : "";
  return meta || dir;
}

function setPlayIcon(playing) {
  $("picon-play").style.display = playing ? "none" : "block";
  $("picon-pause").style.display = playing ? "block" : "none";
}

/** 从某一秒开始播放当前这集的本地音频 */
function playAt(sec, el) {
  if (!curWorkdir) return;
  const a = pa();
  if (audioDir !== curWorkdir) {
    audioDir = curWorkdir;
    a.src = "/api/audio/" + encodeURIComponent(curWorkdir);
    $("ptitle").textContent = episodeTitle(curWorkdir);
    $("pfill").style.width = "0%";
    $("pnow").textContent = fmtClock(sec);
    $("ptotal").textContent = "0:00";
  }
  const jump = () => { a.currentTime = Math.max(0, sec); a.play().catch(() => {}); };
  if (a.readyState >= 1) jump();
  else a.addEventListener("loadedmetadata", jump, { once: true });
  document.querySelectorAll(".ts.active").forEach((n) => n.classList.remove("active"));
  if (el) el.classList.add("active");
  $("player").classList.add("show");
}

function togglePlay() {
  const a = pa();
  if (!a.src) { toast("⚠ 先点文章里的时间戳，才会载入音频"); return; }
  a.paused ? a.play().catch(() => {}) : a.pause();
}

function seekBy(delta) {
  const a = pa();
  if (!a.duration) return;
  a.currentTime = Math.min(a.duration, Math.max(0, a.currentTime + delta));
}

function closePlayer() {
  const a = pa();
  a.pause();
  $("player").classList.remove("show");
  document.querySelectorAll(".ts.active").forEach((n) => n.classList.remove("active"));
}

/** 文字稿里的时间戳也做成可点 */
function renderTranscript(text) {
  return esc(text).replace(/\[(\d{1,2}):(\d{2}):(\d{2})\]/g,
    (_m, h, mi, s) => `<a class="ts" data-sec="${+h * 3600 + +mi * 60 + +s}" title="跳到音频此处">[${h}:${mi}:${s}]</a>`);
}

/* ---------------- 模态框（同风格弹窗）---------------- */
let modalHandler = null;

function showModal({ title, desc = "", value = "", placeholder = "", okText = "确定",
                     danger = false, withInput = true, bodyHtml = "", onOk = null }) {
  $("modaltitle").textContent = title;
  $("modaldesc").textContent = desc;
  $("modaldesc").style.display = desc ? "block" : "none";
  $("modalbody").innerHTML = bodyHtml;
  $("modalbody").style.display = bodyHtml ? "block" : "none";
  const inp = $("modalinput");
  inp.style.display = withInput ? "block" : "none";
  inp.value = value;
  inp.placeholder = placeholder;
  const ok = $("modalok");
  ok.textContent = okText;
  ok.classList.toggle("danger", !!danger);
  ok.classList.toggle("primary", !danger);
  modalHandler = onOk;
  $("modal").classList.add("show");
  if (withInput) setTimeout(() => { inp.focus(); inp.select(); }, 30);
}

function closeModal() {
  $("modal").classList.remove("show");
  modalHandler = null;
}

function modalOk() {
  const needsInput = $("modalinput").style.display !== "none";
  const val = $("modalinput").value.trim();
  if (needsInput && !val) { $("modalinput").focus(); return; }   // 空值不提交
  const handler = modalHandler;
  closeModal();
  if (handler) handler(val);
}

/* ---------------- MCP ---------------- */
let mcpServers = [], mcpTools = {}, mcpPresets = [], notionDbId = "", pendingPreset = null;

const NOTION_TPL = (dbId) => ({
  parent: { database_id: dbId || "<你的数据库 ID>" },
  properties: {
    "标题": { title: [{ text: { content: "{{title}}" } }] },
    "播客": { rich_text: [{ text: { content: "{{podcast}}" } }] },
    "来源": { url: "{{url}}" },
  },
  children: "{{blocks}}",
});

async function loadMcp() {
  try {
    const resp = await fetch("/api/mcp/servers");
    const d = await resp.json();
    mcpServers = d.servers || [];
    mcpPresets = d.presets || [];
    notionDbId = (d.defaults || {}).notion_database_id || "";
    renderServers();
    renderPresets();
    // 连接状态自动检测，用户不用点「测试」
    mcpServers.slice(0, 5).forEach((s) => testServer(s.name, true));
  } catch (e) { /* 服务未就绪时静默 */ }
}

function renderServers() {
  const el = $("srvlist");
  if (!mcpServers.length) {
    el.innerHTML = `<div style="font-size:13px;color:var(--dim);padding:4px 0 2px">还没有连接任何服务器。从下面挑一个添加。</div>`;
    refreshDest(); return;
  }
  el.innerHTML = mcpServers.map((s) => `
    <div class="srv">
      <div class="info">
        <div class="nm">${esc(s.name)}<span class="tag" id="tag-${esc(s.name)}">检测中…</span></div>
        <div class="cmd">${esc([s.command, ...(s.args || [])].join(" "))}</div>
        <details class="tdet" id="det-${esc(s.name)}" style="display:none">
          <summary>工具列表</summary>
          <div class="tools" id="tools-${esc(s.name)}"></div>
        </details>
      </div>
      <div class="acts">
        <button class="btn mini" onclick="delServer('${esc(s.name)}')">删除</button>
      </div>
    </div>`).join("");
  refreshDest();
}

function renderPresets() {
  const rows = mcpPresets.map((p) => {
    const state = p.added ? "已添加" : (p.ready ? "添加" : "需要密钥");
    return `<button class="preset" ${p.added ? "disabled" : ""} onclick="addPreset('${esc(p.id)}')">
      <span class="pl">${esc(p.label)}</span><span class="ph">${esc(p.hint)}</span><span class="ps">${state}</span>
    </button>`;
  });
  rows.push(`<button class="preset" onclick="showCustom()">
    <span class="pl">自定义…</span><span class="ph">填一条启动命令即可，支持任意 stdio MCP 服务器</span><span class="ps">高级</span>
  </button>`);
  $("presets").innerHTML = rows.join("");
}

async function testServer(name, silent) {
  const tag = $("tag-" + name), box = $("tools-" + name), det = $("det-" + name);
  if (tag) { tag.className = "tag"; tag.textContent = "检测中…"; }
  try {
    const resp = await fetch("/api/mcp/test", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
    });
    const d = await resp.json();
    if (!resp.ok) throw new Error(d.error || "连接失败");
    mcpTools[name] = d.tools || [];
    if (tag) { tag.className = "tag ok"; tag.textContent = `✓ ${mcpTools[name].length} 个工具`; tag.title = d.server || ""; }
    if (box) box.innerHTML = mcpTools[name].map((t) =>
      `<span class="tool" title="${esc(t.description || "")}" onclick="pickTool('${esc(name)}','${esc(t.name)}')">${esc(t.name)}</span>`).join("");
    if (det) det.style.display = mcpTools[name].length ? "block" : "none";
    refreshDest();
  } catch (e) {
    delete mcpTools[name];
    if (tag) { tag.className = "tag bad"; tag.textContent = "连接失败"; tag.title = String(e.message || e); }
    if (box) box.innerHTML = `<span style="font-size:12px;color:var(--red)">${esc(String(e.message || e))}</span>
      <span class="retry" onclick="testServer('${esc(name)}')">重试</span>`;
    if (det) det.style.display = "block";
  }
  if (silent) return;
}

function refreshDest() {
  const sel = $("dest"), cur = sel.value;
  // 与「发布」最相关的工具排最前：建页 > 写内容 > 其他写入 > 读取
  const rank = (n) => {
    const s = n.toLowerCase();
    const readish = /retrieve|get-|list-|search|query/.test(s);
    if (/post-page|create-a-page|create-page/.test(s)) return 4;
    if (!readish && /markdown|patch-block-children|append/.test(s)) return 3;
    if (!readish && /post|create|update|patch|insert|write/.test(s)) return 2;
    return 1;
  };
  let html = '<option value="builtin">内置 Notion 集成（REST）</option>';
  for (const [name, tools] of Object.entries(mcpTools)) {
    const sorted = [...tools].sort((a, b) => rank(b.name) - rank(a.name));
    html += `<optgroup label="MCP · ${esc(name)}（${tools.length} 个工具）">`;
    html += sorted.map((t) => `<option value="mcp:${esc(name)}:${esc(t.name)}">${rank(t.name) >= 3 ? "★ " : ""}${esc(t.name)}</option>`).join("");
    html += "</optgroup>";
  }
  sel.innerHTML = html;
  if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
  onDestChange();
}

function onDestChange() {
  const isMcp = $("dest").value.startsWith("mcp:");
  $("tplbox").style.display = isMcp ? "block" : "none";
  if (isMcp && !$("tpl").value.trim()) $("tpl").value = JSON.stringify(NOTION_TPL(notionDbId), null, 2);
}

function pickTool(name, tool) {
  refreshDest();
  const v = `mcp:${name}:${tool}`;
  if ([...$("dest").options].some((o) => o.value === v)) $("dest").value = v;
  onDestChange();
  $("dest").scrollIntoView({ behavior: "smooth", block: "center" });
  toast(`发布目标：MCP · ${esc(name)} · ${esc(tool)}`);
}

function hideAddForms() {
  $("needform").style.display = "none";
  $("customform").style.display = "none";
  pendingPreset = null;
}

function showCustom() {
  hideAddForms();
  $("customform").style.display = "block";
  $("m_cmdline").focus();
}

async function addPreset(id) {
  const p = mcpPresets.find((x) => x.id === id);
  if (p && !p.ready) {           // 缺密钥：只问这一件事
    hideAddForms();
    pendingPreset = id;
    $("m_secret").value = "";
    $("m_secret").placeholder = p.need_placeholder || "粘贴密钥";
    $("needhelp").textContent = p.need_help || "";
    $("needform").style.display = "block";
    $("m_secret").focus();
    return;
  }
  await postPreset(id);
}

async function confirmPreset() {
  if (!pendingPreset) return;
  await postPreset(pendingPreset, $("m_secret").value.trim());
}

async function postPreset(id, secret) {
  const msg = $("m_msg");
  msg.textContent = "添加中…";
  const resp = await fetch("/api/mcp/servers/preset", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ preset: id, secret: secret || "" }),
  });
  const d = await resp.json();
  if (!resp.ok) { msg.textContent = "⚠ " + (d.error || "添加失败"); return; }
  msg.textContent = "";
  hideAddForms();
  await loadMcp();
  toast(`✦ 已添加：${esc(d.server.name)}`);
}

async function addCustomServer() {
  const msg = $("m_msg");
  if (!$("m_cmdline").value.trim()) { msg.textContent = "⚠ 请填写启动命令"; return; }
  msg.textContent = "添加中…";
  const resp = await fetch("/api/mcp/servers", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: $("m_name").value.trim() || $("m_cmdline").value.trim().split(/\s+/)[0],
      command_line: $("m_cmdline").value.trim(),
      env: $("m_env").value,
    }),
  });
  const d = await resp.json();
  if (!resp.ok) { msg.textContent = "⚠ " + (d.error || "添加失败"); return; }
  msg.textContent = "";
  ["m_name", "m_cmdline", "m_env"].forEach((id) => { $(id).value = ""; });
  hideAddForms();
  await loadMcp();
  toast(`✦ 已添加：${esc(d.server.name)}`);
}

function delServer(name) {
  showModal({
    title: `删除 MCP 服务器「${name}」？`,
    desc: "只是从本机配置里移除，不影响该服务器本身。",
    withInput: false,
    danger: true,
    okText: "删除",
    onOk: async () => {
      await fetch("/api/mcp/servers/" + encodeURIComponent(name), { method: "DELETE" });
      delete mcpTools[name];
      loadMcp();
    },
  });
}

function closeResult() {
  const el = $("result");
  const fromLib = el.dataset.fromLib === "1";
  el.classList.remove("show", "withTranscript", "wide");
  el.dataset.fromLib = "";
  $("tplbox").style.display = "none";
  $("article").innerHTML = "";
  $("transcript").classList.remove("show");
  $("transcript").textContent = ""; $("transcript").dataset.loaded = "";
  $("tbtn").textContent = "查看文字稿";
  curWorkdir = null; curUrl = null;
  audioDir = null;
  pa().pause();
  pa().removeAttribute("src");
  if (fromLib) $("lib").scrollIntoView({ behavior: "smooth", block: "start" });
  else $("url").focus();
}

/* ---------------- 历史库：分类 + 阅读状态 + 拖拽投放 ---------------- */
let libItems = [], libCats = [], libAssign = {}, activeCat = "all";
let libStatus = {}, libStatusCounts = {}, libStatusLabels = {};
let libUsageTotal = null, dragState = null;

const catById = (id) => libCats.find((c) => c.id === id) || null;
const statusOf = (dir) => libStatus[dir] || "unread";
const statusColor = { unread: "#d9d9e0", reading: "#4d6bfe", read: "#10a37f", later: "#d97706" };
const statusLabel = (s) => libStatusLabels[s] || s;
/** 筛选用的「智能列表」；"all"/"none" 是结构筛选，其余按阅读状态筛 */
const SMART = ["unread", "reading", "read", "later"];
const isSmart = (v) => SMART.includes(v);

async function loadLibrary() {
  const resp = await fetch("/api/library");
  const d = await resp.json();
  libItems = d.items || [];
  libCats = d.categories || [];
  libAssign = d.assignments || {};
  libStatus = d.status || {};
  libStatusCounts = d.status_counts || {};
  libStatusLabels = d.status_labels || {};
  libUsageTotal = d.usage_total || null;
  renderCatbar();
  renderGrid();
  renderUsagePill();
  setNavCount("navqcount", d.queue_active || 0);
}

/** 顶栏的累计用量胶囊 */
function renderUsagePill() {
  const el = $("usagepill");
  const t = libUsageTotal;
  el.textContent = t && t.episodes
    ? `累计 ${fmtCost(t.cost_cny)} · ${fmtTokens(t.total_tokens)} tokens`
    : "";
  if (t && t.episodes) {
    el.title = `${t.episodes} 篇 · ${t.calls} 次模型调用\n` +
      `输入 ${t.input_tokens.toLocaleString()} tokens（缓存命中 ${t.hit_tokens.toLocaleString()}，命中率 ${t.cache_hit_rate}%）\n` +
      `输出 ${t.out_tokens.toLocaleString()} tokens\n点开看分模型明细`;
  }
}

function setNavCount(id, n) {
  const el = $(id);
  if (el) el.textContent = n ? String(n) : "";
}

const FOLDER_ICON = `<svg class="ficon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.2h7A1.5 1.5 0 0 1 19 9.7v8.6a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 3 18.3z"></path></svg>`;
const CLOCK_ICON = `<svg class="ficon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"></circle><path d="M12 8v4l2.5 2"></path></svg>`;

function renderCatbar() {
  const unassigned = libItems.filter((it) => !libAssign[it.dir]).length;
  const rows = [
    `<button class="folder ${activeCat === "all" ? "on" : ""}" data-drop="all" onclick="setCat('all')">
       <svg class="ficon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h10"></path></svg>
       <span class="fname">全部文章</span><span class="fcount">${libItems.length}</span></button>`,
    `<button class="folder ${activeCat === "none" ? "on" : ""}" data-drop="none" data-dropkind="cat" onclick="setCat('none')">
       ${CLOCK_ICON}
       <span class="fname">未分类</span><span class="fcount">${unassigned}</span></button>`,
  ];
  // 智能列表：按阅读状态筛（未读 / 在读 / 已读 / 稍后读）。也可以直接把卡片拖到这些行上改状态。
  rows.push('<div class="sidegap"></div>');
  SMART.forEach((s) => {
    rows.push(`<button class="folder ${activeCat === s ? "on" : ""}" data-drop="${s}" data-dropkind="status" onclick="setCat('${s}')" title="把卡片拖到这里即可标记为「${esc(statusLabel(s))}」">
      <span class="fdot ${s}"></span>
      <span class="fname">${esc(statusLabel(s))}</span><span class="fcount">${statusCount(s)}</span></button>`);
  });
  if (libCats.length) {
    rows.push('<div class="sidegap"></div>');
    libCats.forEach((c) => {
      rows.push(`<button class="folder ${activeCat === c.id ? "on" : ""}" data-drop="${c.id}" data-dropkind="cat" onclick="setCat('${c.id}')">
        ${FOLDER_ICON}
        <span class="fname">${esc(c.name)}</span>
        <span class="fcount">${c.count || 0}</span>
        <span class="fedit">
          <span title="重命名" onclick="renameCat(event,'${c.id}')">✎</span>
          <span title="删除分类" onclick="deleteCat(event,'${c.id}')">✕</span>
        </span></button>`);
    });
  }
  rows.push(`<button class="folder add" onclick="createCat()">
    <svg class="ficon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 5v14M5 12h14"></path></svg>
    <span class="fname">新建分类</span></button>`);
  $("catnav").innerHTML = rows.join("");
}

/** 某个阅读状态下实际有几篇（未读要把没有标记的也算上） */
function statusCount(s) {
  const marked = libStatusCounts[s] || 0;
  if (s !== "unread") return marked;
  return libItems.filter((it) => statusOf(it.dir) === "unread").length;
}

function setCat(v) {
  activeCat = v;
  if (searchQuery) clearSearch(true);      // 从搜索结果切回筛选时把搜索状态清掉
  showView("lib");
  renderCatbar(); renderGrid();
}

function matchesFilter(it) {
  if (activeCat === "all") return true;
  if (activeCat === "none") return !libAssign[it.dir];
  if (isSmart(activeCat)) return statusOf(it.dir) === activeCat;
  return libAssign[it.dir] === activeCat;
}

function renderGrid() {
  const shown = libItems.filter(matchesFilter);
  const empty = $("empty-lib");
  if (searchQuery) { /* 搜索模式由 renderSearch 负责 */ }
  else if (!libItems.length) { empty.textContent = "还没有生成过任何文章。"; empty.style.display = "block"; }
  else if (!shown.length) {
    empty.textContent = isSmart(activeCat)
      ? `「${statusLabel(activeCat)}」里还没有文章 —— 打开一篇文章后在工具条里标记即可。`
      : "这个分类下还没有文章 —— 把下面的卡片按住拖到上方分类里即可。";
    empty.style.display = "block";
  } else empty.style.display = "none";

  document.querySelectorAll("#quicktabs button").forEach((b) =>
    b.classList.toggle("on", b.dataset.quick === activeCat));
  const cur = isSmart(activeCat) || activeCat === "all" || activeCat === "none" ? null : catById(activeCat);
  $("libtitle").textContent = activeCat === "all" ? "全部文章"
    : activeCat === "none" ? "未分类"
    : isSmart(activeCat) ? statusLabel(activeCat)
    : (cur ? cur.name : "分类");
  $("libcount").textContent = shown.length ? `共 ${shown.length} 篇` : "";
  $("libgrid").innerHTML = shown.map(cardHTML).join("");
}

/** 一张文章卡片 */
function cardHTML(it) {
  const pv = it.preview || {};
  const c = catById(libAssign[it.dir]);
  const st = statusOf(it.dir);
  const take = (pv.takeaways || []).map((t) => `<li>${esc(t)}</li>`).join("");
  const dir = esc(it.dir);
  const cost = it.usage && it.usage.calls ? ` · ≈${fmtCost(it.usage.cost_cny)}` : "";
  return `
  <div class="ep" data-dir="${dir}" onclick="openEpisode('${encodeURIComponent(it.dir)}')">
    <button class="kill" title="删除这条记录" onclick="event.stopPropagation();askDelete('${dir}')">✕</button>
    <div class="cardacts">
      <button class="mini-act" title="标为已读" onclick="event.stopPropagation();setArticleStatus('${dir}','read')">✓ 已读</button>
      <button class="mini-act" title="加入稍后读" onclick="event.stopPropagation();setArticleStatus('${dir}','later')">◷ 稍后读</button>
      <button class="mini-act" title="导出 Markdown" onclick="event.stopPropagation();exportOne('md','${dir}')">导出</button>
    </div>
    <div class="t">${esc(it.title)}</div>
    ${pv.deck ? `<div class="deck">${esc(pv.deck)}</div>` : ""}
    ${take ? `<ul class="tk">${take}</ul>` : ""}
    <div class="s">
      <span class="catlabel" title="阅读状态：${esc(statusLabel(st))}（点击更换）"
            onclick="event.stopPropagation();openStatusMenu(event,'${dir}')"><span class="dot" style="background:${statusColor[st]}"></span>${esc(statusLabel(st))}</span>
      ${c
        ? `<span class="catlabel" title="点击更换分类" onclick="event.stopPropagation();openCatMenu(event,'${dir}')"><span class="dot" style="background:${esc(c.color)}"></span>${esc(c.name)}</span>`
        : `<span class="catlabel empty" title="点击归类" onclick="event.stopPropagation();openCatMenu(event,'${dir}')">＋ 分类</span>`}
      ${it.podcast ? `<span>${esc(it.podcast)}</span>` : ""}
      ${it.has_article ? `<span class="ok">✓ 有文章</span>` : `<span>无文章</span>`}
      ${pv.chars ? `<span>${pv.chars} 字</span>` : ""}
      ${cost ? `<span title="这一集累计消耗">${cost.trim().replace(/^·\s*/, "")}</span>` : ""}
    </div>
  </div>`;
}

/* ---- 阅读状态 ---- */
async function setArticleStatus(dir, status) {
  const resp = await fetch("/api/status", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dir, status }),
  });
  const d = await resp.json();
  if (!resp.ok) { toast("⚠ " + esc(d.error || "标记失败")); return; }
  libStatus = d.state.status || {};
  libStatusCounts = d.state.status_counts || {};
  libStatusLabels = d.state.status_labels || {};
  renderCatbar(); renderGrid();
  if (dir === curWorkdir) syncReadButton();
  toast(`✦ 已标记为「${esc(statusLabel(d.value))}」`);
}

/** 打开文章时自动从「未读」推进到「在读」（不打断用户，静默执行） */
async function autoMarkReading(dir) {
  if (statusOf(dir) !== "unread") return;
  try {
    const resp = await fetch("/api/status", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dir, status: "reading" }),
    });
    if (!resp.ok) return;
    const d = await resp.json();
    libStatus = d.state.status || {};
    libStatusCounts = d.state.status_counts || {};
    renderCatbar();
  } catch (e) { /* 标记失败不该影响阅读 */ }
}

/** 文章工具条上的状态按钮：显示当前状态，点开菜单选择 */
function syncReadButton() {
  const btn = $("readbtn");
  if (!btn) return;
  btn.textContent = curWorkdir ? `${statusLabel(statusOf(curWorkdir))} ▾` : "状态 ▾";
}

function openStatusMenu(ev, dir) {
  ev.stopPropagation();
  const menu = document.createElement("div");
  menu.className = "catmenu";
  menu.innerHTML = SMART.map((s) =>
    `<div class="mi" data-st="${s}"><span class="dot" style="background:${statusColor[s]}"></span>${esc(statusLabel(s))}</div>`
  ).join("") + `<div class="sep"></div><div class="mi" data-st=""><span class="dot" style="background:#e6e6ec"></span>清除标记</div>`;
  document.body.appendChild(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 12) + "px";
  menu.style.top = Math.min(ev.clientY + 8, window.innerHeight - r.height - 12) + "px";
  menu.addEventListener("click", async (e) => {
    const mi = e.target.closest(".mi");
    if (!mi) return;
    e.stopPropagation();
    closeCatMenu();
    await setArticleStatus(dir, mi.dataset.st);
  });
  setTimeout(() => document.addEventListener("pointerdown", outsideCatMenu, true), 0);
}

/* ---- 工具条上的状态菜单 ---- */
function toggleReadMenu(ev) {
  ev.stopPropagation();
  togglePopmenu("readmenu", ev.currentTarget);
  if (curWorkdir) {
    document.querySelectorAll("#readmenu .mi").forEach((mi) => {
      const st = mi.dataset.st;
      mi.style.fontWeight = st && st === statusOf(curWorkdir) ? "600" : "";
    });
  }
}

async function markStatus(st) {
  hidePopmenus();
  if (!curWorkdir) return;
  await setArticleStatus(curWorkdir, st);
}

async function clearStatusMark() {
  hidePopmenus();
  if (!curWorkdir) return;
  await setArticleStatus(curWorkdir, "");
}

function createCat() {
  showModal({
    title: "新建分类",
    desc: "建好后，按住卡片拖到分类上即可归类。",
    placeholder: "例如：AI 技术 / 商业访谈 / 读书",
    okText: "创建",
    onOk: async (name) => {
      const resp = await fetch("/api/categories", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
      });
      const d = await resp.json();
      if (!resp.ok) { toast("⚠ " + esc(d.error || "创建失败")); return; }
      await loadLibrary();
      toast(`✦ 已创建分类「${esc(d.category.name)}」`);
    },
  });
}

function renameCat(ev, cid) {
  ev.stopPropagation();
  const cur = catById(cid);
  showModal({
    title: "重命名分类",
    value: cur ? cur.name : "",
    okText: "保存",
    onOk: async (name) => {
      const resp = await fetch("/api/categories/" + encodeURIComponent(cid), {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
      });
      const d = await resp.json();
      if (!resp.ok) { toast("⚠ " + esc(d.error || "重命名失败")); return; }
      await loadLibrary();
      toast("✦ 已重命名");
    },
  });
}

function deleteCat(ev, cid) {
  ev.stopPropagation();
  const cur = catById(cid);
  showModal({
    title: `删除分类「${cur ? cur.name : cid}」？`,
    desc: "分类里的文章不会被删除，只会退回「未分类」。",
    withInput: false,
    danger: true,
    okText: "删除",
    onOk: async () => {
      const resp = await fetch("/api/categories/" + encodeURIComponent(cid), { method: "DELETE" });
      if (!resp.ok) { const d = await resp.json(); toast("⚠ " + esc(d.error || "删除失败")); return; }
      if (activeCat === cid) activeCat = "all";
      await loadLibrary();
      toast("✦ 已删除分类");
    },
  });
}

async function assignArticle(dir, cid) {
  const resp = await fetch("/api/assign", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dir, category_id: cid }),
  });
  const d = await resp.json();
  if (!resp.ok) { toast("⚠ " + esc(d.error || "移动失败")); return; }
  libCats = d.categories || []; libAssign = d.assignments || {};
  renderCatbar(); renderGrid();
  const c = catById(cid);
  toast(c ? `✦ 已移入「${esc(c.name)}」` : "✦ 已移出分类");
}

/* ---- 删除历史记录（两种粒度）---- */
const fmtSize = (b) => (b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB");

function askDelete(dir) {
  const item = libItems.find((i) => i.dir === dir) || {};
  const size = item.size || 0;
  const hasArticle = item.has_article !== false;
  showModal({
    title: "删除这条记录？",
    desc: esc(item.title || dir),
    withInput: false,
    danger: true,
    okText: "删除",
    bodyHtml: `
      <label class="opt"><input type="radio" name="delscope" value="all" checked>
        <span><span class="ot">删除整条记录</span>
        <span class="od">文章、音频、文字稿一起删除，释放 ${fmtSize(size)}</span></span></label>
      <label class="opt"><input type="radio" name="delscope" value="article" ${hasArticle ? "" : "disabled"}>
        <span><span class="ot">只删文章</span>
        <span class="od">保留音频与文字稿，之后可重新生成；不释放磁盘空间</span></span></label>`,
    onOk: async () => {
      const picked = document.querySelector('input[name="delscope"]:checked');
      const scope = picked ? picked.value : "all";
      const resp = await fetch("/api/episode/" + encodeURIComponent(dir), {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope }),
      });
      const d = await resp.json();
      if (!resp.ok) { toast("⚠ " + esc(d.error || "删除失败")); return; }
      if (curWorkdir === dir) closeResult();           // 正在看的被删了，收起阅读区
      await loadLibrary();
      toast(scope === "article"
        ? "✦ 已删除文章（音频与文字稿保留，可重新生成）"
        : `✦ 已删除整条记录，释放 ${fmtSize(d.freed || 0)}`);
    },
  });
}

/* ---- 点击归类菜单（拖拽之外的备用入口）---- */
function closeCatMenu() {
  const m = document.querySelector(".catmenu");
  if (m) m.remove();
  document.removeEventListener("pointerdown", outsideCatMenu, true);
}

function outsideCatMenu(e) {
  if (!e.target.closest(".catmenu")) closeCatMenu();
}

function openCatMenu(ev, dir) {
  closeCatMenu();
  const menu = document.createElement("div");
  menu.className = "catmenu";
  const items = libCats.map((c) =>
    `<div class="mi" data-cid="${esc(c.id)}"><span class="dot" style="background:${esc(c.color)}"></span>${esc(c.name)}</div>`
  ).join("");
  menu.innerHTML = items + (libCats.length ? '<div class="sep"></div>' : "")
    + `<div class="mi" data-cid=""><span class="dot" style="background:#d9d9e0"></span>移出分类</div>`
    + `<div class="mi" data-new="1"><span class="dot" style="background:#e6e6ec"></span>＋ 新建分类…</div>`;
  document.body.appendChild(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 12) + "px";
  menu.style.top = Math.min(ev.clientY + 8, window.innerHeight - r.height - 12) + "px";
  menu.addEventListener("click", async (e) => {
    const mi = e.target.closest(".mi");
    if (!mi) return;
    e.stopPropagation();
    if (mi.dataset.new) { closeCatMenu(); await createCat(); return; }
    const cid = mi.dataset.cid;
    closeCatMenu();
    assignArticle(dir, cid);
  });
  setTimeout(() => document.addEventListener("pointerdown", outsideCatMenu, true), 0);
}

/* ---- 按住拖拽：pointer 事件统一处理鼠标与触屏 ---- */
function dropChipAt(x, y) {
  const el = document.elementFromPoint(x, y);
  const zone = el ? el.closest("[data-drop]") : null;
  return zone && zone.dataset.drop !== "all" ? zone : null;   // "全部文章" 不是分类，不接受投放
}

function beginDrag(ev, dir, card) {
  if (ev.button !== undefined && ev.button !== 0) return;      // 只响应左键 / 触摸
  if (ev.target.closest(".kill, .fedit")) return;               // 点删除/编辑图标不算拖拽
  const sx = ev.clientX, sy = ev.clientY;
  let started = false;

  const move = (e) => {
    if (!started) {
      if (Math.hypot(e.clientX - sx, e.clientY - sy) < 6) return;
      const item = libItems.find((i) => i.dir === dir) || {};
      const ghost = document.createElement("div");
      ghost.className = "dragghost";
      ghost.innerHTML = `<div class="t">${esc(item.title || dir)}</div>`;
      document.body.appendChild(ghost);
      dragState = { ghost, dir };
      card.classList.add("dragging");
      document.body.classList.add("dragging");
      started = true;
    }
    dragState.ghost.style.left = e.clientX + 14 + "px";
    dragState.ghost.style.top = e.clientY - 22 + "px";
    document.querySelectorAll("[data-drop].drop").forEach((c) => c.classList.remove("drop"));
    const zone = dropChipAt(e.clientX, e.clientY);
    if (zone) zone.classList.add("drop");
  };

  const up = (e) => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    document.removeEventListener("pointercancel", up);
    if (!started) return;
    const zone = dropChipAt(e.clientX, e.clientY);
    dragState.ghost.remove();
    document.querySelectorAll(".ep.dragging").forEach((c) => c.classList.remove("dragging"));
    document.querySelectorAll("[data-drop].drop").forEach((c) => c.classList.remove("drop"));
    document.body.classList.remove("dragging");
    dragState = null;
    // 拖过之后抑制这次 click，避免顺手把文章打开
    window.__dragged = true;
    setTimeout(() => { window.__dragged = false; }, 80);
    if (zone) {
      const key = zone.dataset.drop;
      // 拖到智能列表行 = 改阅读状态；拖到分类行 = 改分类
      if (zone.dataset.dropkind === "status") setArticleStatus(dir, key);
      else assignArticle(dir, key === "none" ? "" : key);
    }
  };

  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
  document.addEventListener("pointercancel", up);
}

async function openEpisode(dirEnc) {
  if (window.__dragged) return;   // 刚才是拖拽，不要顺手打开文章
  const dir = decodeURIComponent(dirEnc);  const metaResp = await fetch(`/api/file/${dirEnc}/meta.json`);
  const meta = await metaResp.json();
  curWorkdir = dir; curUrl = meta.url || null;
  $("result").dataset.fromLib = "1";   // 关闭时回到历史库而不是回到输入框
  const artResp = await fetch(`/api/file/${dirEnc}/article.md`);
  if (!artResp.ok) {
    $("rmeta").innerHTML = `<span class="pod">${esc(meta.podcast || "")}</span><span>${esc(meta.title || "")}</span>`;
    $("article").innerHTML = `<p style="color:var(--dim)">该单集还没有生成文章。</p>`;
    const tResp = await fetch(`/api/file/${dirEnc}/transcript.txt`);
    $("transcript").innerHTML = renderTranscript(await tResp.text()); $("transcript").dataset.loaded = "1";
    $("transcript").classList.add("show"); $("result").classList.add("withTranscript");
    $("tbtn").textContent = "收起文字稿";
    $("nbtn").disabled = true;
  } else {
    const { html } = await artResp.json();
    $("rmeta").innerHTML =
      `<span class="pod">${esc(meta.podcast || "")}</span><span>${esc(meta.title || "")}</span>` +
      (meta.duration ? `<span>${humanDur(meta.duration)}</span>` : "");
    $("article").innerHTML = html;
    $("transcript").textContent = ""; $("transcript").dataset.loaded = "";
    $("transcript").classList.remove("show"); $("result").classList.remove("withTranscript", "wide");
    $("tbtn").textContent = "查看文字稿";
    $("nbtn").disabled = false;
  }
  $("result").classList.add("show");
  $("result").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") startRun();
});
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !$("go").disabled) { e.preventDefault(); startRun(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if ($("modal").classList.contains("show")) { closeModal(); return; }
  if ($("settings").style.display === "block") { closeSettings(); return; }
  if ($("result").classList.contains("withTranscript")) { toggleTranscript(); return; }
  if ($("result").classList.contains("show")) closeResult();
});
// 时间戳回听（只绑定一次）
document.addEventListener("click", (e) => {
  const ts = e.target.closest(".ts");
  if (!ts) return;
  e.preventDefault();
  playAt(parseInt(ts.dataset.sec, 10), ts);
});
$("pbar").addEventListener("click", (e) => {
  const a = pa();
  if (!a.duration) return;
  const r = e.currentTarget.getBoundingClientRect();
  a.currentTime = ((e.clientX - r.left) / r.width) * a.duration;
});
pa().addEventListener("timeupdate", () => {
  const a = pa();
  $("pnow").textContent = fmtClock(a.currentTime);
  if (a.duration) $("pfill").style.width = (a.currentTime / a.duration) * 100 + "%";
});
pa().addEventListener("loadedmetadata", () => { $("ptotal").textContent = fmtClock(pa().duration); });
pa().addEventListener("play", () => setPlayIcon(true));
pa().addEventListener("pause", () => setPlayIcon(false));
pa().addEventListener("error", () => {
  if (pa().src) toast("⚠ 这一集没有本地音频（可能已被删除）");
  closePlayer();
});

// 模态框交互（只绑定一次）
$("modalok").addEventListener("click", modalOk);
$("modalcancel").addEventListener("click", closeModal);
$("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
$("modalinput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); modalOk(); }
});

// 事件委托：卡片按住即开始拖拽（只绑定一次）
$("libgrid").addEventListener("pointerdown", (e) => {
  const card = e.target.closest(".ep");
  if (card) beginDrag(e, card.dataset.dir, card);
});

loadLibrary();
loadMcp();
loadSettings();
