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
  autoGrow();
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
    $("hint").innerHTML = "⌘/Ctrl + Enter 直接开始（批量时一行一条链接）· Esc 关闭文章 / 退出设置 · 产物会缓存，重新生成文章不必重新转写";
  }
}

/** 输入框随内容长高（最多 6 行左右），因为要支持一次粘多条链接 */
function autoGrow() {
  const el = $("url");
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 160) + "px";
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
    syncReadDone();
    loadLibrary();
    collapseRunview();
    resetAssist();
    syncFab();
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

async function openSettings(tab) {
  closeAssist();                 // 抽屉会盖住设置页
  $("main").style.display = "none";
  $("settings").style.display = "block";
  window.scrollTo({ top: 0 });
  await loadSettings();
  if (tab) switchTab(tab);
  loadMcp();
}

function closeSettings() {
  $("settings").style.display = "none";
  $("main").style.display = "block";
  window.scrollTo({ top: 0 });
  loadLibrary();
}

/** 从设置页退出到主界面（不重新拉数据）。

    设置页是把整个 #main 藏起来的，而侧边栏的分类 / 队列 / 订阅 / 搜索只是切换
    #main **内部**的子视图 —— 不先把 #main 放出来，点了就「毫无反应」（用户反馈过）。
    所以所有进入主界面的入口都要先经过这里。
*/
function leaveSettings() {
  if ($("settings").style.display !== "block") return false;
  $("settings").style.display = "none";
  $("main").style.display = "block";
  return true;
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

  // 订阅调度
  const sub = d.subscriptions || {};
  $("sub_enabled").checked = sub.enabled !== false;
  $("sub_interval").value = sub.interval_minutes ?? 120;
  $("sub_auto").checked = sub.auto_generate !== false;
  renderDestOptions(sub.auto_dest || "");

  // 阅读助手
  const as = d.assistant || {};
  assistEnabled = as.enabled !== false;
  $("as_enabled").checked = assistEnabled;
  $("as_web").checked = as.web_default !== false;
  $("aweb_toggle").checked = as.web_default !== false;
  syncFab();
  loadSearchService();

  loadUsageBox();

  applyDefaultsToComposer(g);
  clearDirty();
}

/** 「自动生成的文章归入」下拉：未分类 + 现有分类 */
function renderDestOptions(selected) {
  const sel = $("sub_dest");
  if (!sel) return;
  sel.innerHTML = `<option value="">未分类</option>` +
    libCats.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  sel.value = selected || "";
}

/** 设置页的用量面板 */
async function loadUsageBox() {
  const box = $("usagebox");
  if (!box) return;
  box.innerHTML = `<div class="inforow"><span class="k">正在统计…</span><span class="v"></span></div>`;
  try {
    const d = await (await fetch("/api/usage")).json();
    const t = d.total || {};
    if (!t.episodes) {
      box.innerHTML = `<div class="inforow"><span class="k">还没有用量记录</span>
        <span class="v">生成第一篇文章后，这里会出现 token 与费用明细</span></div>`;
      $("usage_state").textContent = "";
      return;
    }
    $("usage_state").textContent = `${t.episodes} 篇有记录`;
    $("usage_state").className = "fstate ok";
    const rows = [
      ["累计费用（估算）", fmtCost(t.cost_cny)],
      ["总计 tokens", `${t.total_tokens.toLocaleString()}（输入 ${t.input_tokens.toLocaleString()} / 输出 ${t.out_tokens.toLocaleString()}）`],
      ["缓存命中", `${t.hit_tokens.toLocaleString()} tokens · ${t.cache_hit_rate}%（命中部分单价只有 1/50）`],
      ["模型调用次数", `${t.calls} 次`],
      ["平均每篇", t.episodes ? `${fmtCost(t.cost_cny / t.episodes)} · ${Math.round(t.total_tokens / t.episodes).toLocaleString()} tokens` : "—"],
    ];
    Object.entries(t.by_model || {}).forEach(([model, u]) => {
      rows.push([`　${model}`, `${u.episodes || ""}${u.episodes ? " 篇 · " : ""}${fmtCost(u.cost_cny)} · ${fmtTokens(u.total_tokens)} tokens`]);
    });
    box.innerHTML = rows.map(([k, v]) =>
      `<div class="inforow"><span class="k">${esc(k)}</span><span class="v">${esc(String(v))}</span></div>`).join("");
  } catch (e) {
    box.innerHTML = `<div class="inforow"><span class="k">统计失败</span><span class="v">${esc(String(e))}</span></div>`;
  }
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
    subscriptions: {
      enabled: $("sub_enabled").checked,
      interval_minutes: parseInt($("sub_interval").value, 10) || 0,
      auto_generate: $("sub_auto").checked,
      auto_dest: $("sub_dest").value,
    },
    assistant: {
      enabled: $("as_enabled").checked,
      web_default: $("as_web").checked,
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

/** 从某一秒开始播放某一集的本地音频（dir 省略时用当前打开的文章） */
function playAt(sec, el, dir) {
  const target = dir || curWorkdir;
  if (!target) return;
  const a = pa();
  if (audioDir !== target) {
    audioDir = target;
    a.src = "/api/audio/" + encodeURIComponent(target);
    $("ptitle").textContent = episodeTitle(target);
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
  syncFab();                 // 播放条升起，悬浮球要往上让位
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
  syncFab();
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
  curWorkdir = null; curUrl = null; curUsage = null;
  hidePopmenus();
  renderCostPill();
  if (assistOpen) closeAssist();
  $("selbtn").classList.remove("show");
  audioDir = null;
  pa().pause();
  pa().removeAttribute("src");
  syncFab();
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
  renderUsagePill();
  setNavCount("navqcount", d.queue_active || 0);
  // 搜索状态下刷新库时，重新跑一次当前查询（结果里的费用/状态也会跟着更新）
  if (searchQuery) await runSearch(searchQuery);
  else renderGrid();
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
  if (searchQuery) return;    // 搜索模式下由 renderSearch 负责渲染，别把结果覆盖掉
  const shown = libItems.filter(matchesFilter);
  const empty = $("empty-lib");
  if (!libItems.length) { empty.textContent = "还没有生成过任何文章。"; empty.style.display = "block"; }
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
    <div class="cardtools">
      <button class="ctool" title="文章设置（状态 / 分类 / 导出）" aria-label="文章设置"
              onclick="event.stopPropagation();openCardMenu('${dir}')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round">
          <circle cx="12" cy="12" r="3"></circle>
          <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"></path>
        </svg>
      </button>
      <button class="ctool kill" title="删除这条记录" aria-label="删除这条记录"
              onclick="event.stopPropagation();askDelete('${dir}')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
          <path d="M18 6 6 18M6 6l12 12"></path>
        </svg>
      </button>
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

/* ---- 卡片右上角的设置弹窗：状态 / 分类 / 导出 / 删除都收在这里 ----
   原先这些按钮是悬停浮在卡片上的，会压住标题和引语（用户反馈过），
   现在改成常驻的两个小图标 + 一个弹窗，标题区永远不被遮挡。 */

function openCardMenu(dir) {
  const it = libItems.find((i) => i.dir === dir) || {};
  const url = `'${encodeURIComponent(dir)}'`;

  const statusChips = SMART.map((s) => {
    const on = statusOf(dir) === s ? " on" : "";
    return `<button class="mchip${on}" onclick="cardMenuStatus('${esc(dir)}','${s}')">
      <span class="dot" style="background:${statusColor[s]};width:7px;height:7px;border-radius:50%;display:inline-block"></span>${esc(statusLabel(s))}</button>`;
  }).join("");

  const catOptions = [`<option value="">未分类</option>`]
    .concat(libCats.map((c) =>
      `<option value="${esc(c.id)}" ${libAssign[dir] === c.id ? "selected" : ""}>${esc(c.name)}</option>`))
    .join("");

  showModal({
    title: it.title || dir,
    desc: it.podcast || "",
    withInput: false,
    okText: "完成",
    bodyHtml: `
      <div class="mfield">
        <span class="mlabel">阅读状态</span>
        <div class="mchips" id="mstatus">${statusChips}
          <button class="mchip" onclick="cardMenuStatus('${esc(dir)}','')">清除标记</button></div>
      </div>
      <div class="mfield">
        <span class="mlabel">分类</span>
        <select id="mcat" onchange="cardMenuAssign('${esc(dir)}', this.value)">${catOptions}</select>
      </div>
      <div class="mfield">
        <span class="mlabel">导出</span>
        <div class="mchips">
          <button class="mchip" onclick="exportOne('md','${esc(dir)}')">Markdown</button>
          <button class="mchip" onclick="exportOne('html','${esc(dir)}')">HTML 单文件</button>
          <button class="mchip" onclick="exportOne('txt','${esc(dir)}')">纯文字稿</button>
        </div>
      </div>
      <div class="mfield">
        <span class="mlabel">其他</span>
        <div class="mchips">
          <button class="mchip" onclick="closeModal();openEpisode(${url})">打开文章</button>
          <button class="mchip danger" onclick="closeModal();askDelete('${esc(dir)}')">删除这条记录</button>
        </div>
      </div>`,
  });
}

/** 弹窗里改状态：改完就地刷新高亮，不关闭弹窗（用户可能还要改分类） */
async function cardMenuStatus(dir, st) {
  await setArticleStatus(dir, st, { quiet: true });
  const box = $("mstatus");
  if (!box) return;
  [...box.querySelectorAll(".mchip")].forEach((chip, i) => {
    const key = SMART[i];
    if (key) chip.classList.toggle("on", statusOf(dir) === key);
  });
  toast(`✦ 已标记为「${esc(statusLabel(statusOf(dir)))}」`);
  syncReadDone();
}

async function cardMenuAssign(dir, cid) {
  await assignArticle(dir, cid, { quiet: true });
  toast(cid ? `✦ 已移入「${esc((catById(cid) || {}).name || "")}」` : "✦ 已移出分类");
}

/* ---- 阅读状态 ---- */
async function setArticleStatus(dir, status, opts = {}) {
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
  if (dir === curWorkdir) { syncReadButton(); syncReadDone(); }
  // quiet：调用方自己会提示（设置弹窗里连改几项，不想弹一串 toast）
  if (!opts.quiet) toast(`✦ 已标记为「${esc(statusLabel(d.value))}」`);
}

/* ---- 读完了：文章末尾的确认按钮 ----
   自动标记「在读」只是推断，读没读完只有本人知道，所以给一个明确的收尾动作。 */
function syncReadDone() {
  const box = $("readdone");
  if (!box) return;
  const show = !!curWorkdir && statusOf(curWorkdir) !== "read";
  box.classList.toggle("show", show);
}

async function finishReading() {
  if (!curWorkdir) return;
  await setArticleStatus(curWorkdir, "read", { quiet: true });
  toast("✦ 已标记为「已读」，之后可以在侧边栏「已读」里找到它");
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

async function assignArticle(dir, cid, opts = {}) {
  const resp = await fetch("/api/assign", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dir, category_id: cid }),
  });
  const d = await resp.json();
  if (!resp.ok) { toast("⚠ " + esc(d.error || "移动失败")); return; }
  libCats = d.categories || []; libAssign = d.assignments || {};
  renderCatbar(); renderGrid();
  if (!opts.quiet) {
    const c = catById(cid);
    toast(c ? `✦ 已移入「${esc(c.name)}」` : "✦ 已移出分类");
  }
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
  // 打开即从「未读」推进到「在读」，并把这一集的累计花费显示出来
  const item = libItems.find((i) => i.dir === dir);
  curUsage = (item && item.usage) || null;
  renderCostPill();
  syncReadButton();
  syncReadDone();
  autoMarkReading(dir);
  // 换了一篇文章：助手里的上下文跟着换，历史问答也重新载入
  if (assistOpen) { resetAssist(); loadAssistHistory(); }
  syncFab();
}

/** 换文章时把助手里上一集的痕迹清掉（否则会拿旧片段回答新文章的问题） */
function resetAssist() {
  stopAssistStream();
  assistAskId = null;
  setAssistSelection("");
  $("aq").value = "";
  $("aanswerbox").style.display = "none";
  $("asources").style.display = "none";
  $("aintro").style.display = "block";
  $("astatus").textContent = "";
  $("agobtn").disabled = false;
}

// 注：输入框的 Enter / input 处理统一放在文件末尾的初始化段（见 autoGrow 的注释）
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !$("go").disabled) { e.preventDefault(); startRun(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  // 分层退出：弹窗 > 下拉菜单 > 阅读助手 > 侧边栏抽屉 > 设置 > 文字稿 > 文章
  if ($("modal").classList.contains("show")) { closeModal(); return; }
  if (document.querySelector(".popmenu:not([hidden])")) { hidePopmenus(); return; }
  if (assistOpen) { closeAssist(); return; }
  if ($("appside").classList.contains("open")) { closeSide(); return; }
  if ($("settings").style.display === "block") { closeSettings(); return; }
  if ($("result").classList.contains("withTranscript")) { toggleTranscript(); return; }
  if ($("result").classList.contains("show")) closeResult();
});

// 选中文字 → 浮出「深挖这段」。用 mouseup/keyup 而不是 selectionchange：
// selectionchange 在拖动过程中会连续触发，按钮会跟着乱抖。
document.addEventListener("mouseup", onArticleSelectionChange);
document.addEventListener("keyup", (e) => { if (e.key.startsWith("Arrow") || e.key === "Shift") onArticleSelectionChange(); });
document.addEventListener("mousedown", (e) => {
  if (!e.target.closest("#selbtn")) $("selbtn").classList.remove("show");
});
window.addEventListener("scroll", () => $("selbtn").classList.remove("show"), { passive: true });
// 时间戳回听（只绑定一次）：文章/文字稿里的时间戳靠这里冒泡上来处理。
// 搜索结果里的时间戳不用这条路（它在 .sr 卡片内部，冒泡会顺带打开文章），
// 而是内联调用 playFromTs()，见 searchCardHTML。
function playFromTs(el) {
  playAt(parseInt(el.dataset.sec, 10), el, el.dataset.dir || curWorkdir);
}

document.addEventListener("click", (e) => {
  const ts = e.target.closest(".ts");
  if (!ts) return;
  e.preventDefault();
  playFromTs(ts);
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
pa().addEventListener("pause", () => setPlayIcon(false));pa().addEventListener("error", () => {
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

/* ---------------- 视图切换（文章库 / 队列 / 订阅）---------------- */
let activeView = "lib";

function showView(name) {
  leaveSettings();          // 从设置页点侧边栏入口时，必须先把主界面放出来
  activeView = name;
  $("lib").style.display = name === "lib" ? "" : "none";
  $("queueview").style.display = name === "queue" ? "" : "none";
  $("feedsview").style.display = name === "feeds" ? "" : "none";
  $("navqueue").classList.toggle("on", name === "queue");
  $("navfeeds").classList.toggle("on", name === "feeds");
  if (name === "queue") loadQueue();
  if (name === "feeds") loadFeeds();
  closeSide();
}

/* ---------------- 侧边栏抽屉（窄屏）---------------- */
function toggleSide() {
  const side = $("appside");
  const open = side.classList.toggle("open");
  let back = document.querySelector(".sideback");
  if (!back) {
    back = document.createElement("div");
    back.className = "sideback";
    back.addEventListener("click", closeSide);
    document.body.appendChild(back);
  }
  back.classList.toggle("open", open);
}

function closeSide() {
  $("appside").classList.remove("open");
  const back = document.querySelector(".sideback");
  if (back) back.classList.remove("open");
}

/* ---------------- 下拉菜单（导出 / 状态）---------------- */
function togglePopmenu(id, btn) {
  const menu = $(id);
  const willShow = menu.hidden;
  hidePopmenus();
  menu.hidden = !willShow;
  if (willShow) setTimeout(() => document.addEventListener("pointerdown", outsidePopmenu, true), 0);
}

function hidePopmenus() {
  document.querySelectorAll(".popmenu").forEach((m) => { m.hidden = true; });
  document.removeEventListener("pointerdown", outsidePopmenu, true);
}

function outsidePopmenu(e) {
  if (!e.target.closest(".popmenu") && !e.target.closest(".menuwrap")) hidePopmenus();
}

function toggleExportMenu(ev) { ev.stopPropagation(); togglePopmenu("exportmenu", ev.currentTarget); }

/* ---------------- 导出 ---------------- */
/** 导出下载地址（单独抽出来，测试可以直接断言这个 URL） */
function exportUrl(fmt, dir) {
  const d = dir || curWorkdir;
  return `/api/export/${encodeURIComponent(d)}?fmt=${encodeURIComponent(fmt)}`;
}

function bundleUrl() {
  const cat = isSmart(activeCat) || activeCat === "all" || activeCat === "none" ? "" : activeCat;
  return "/api/export?transcript=1" + (cat ? `&category=${encodeURIComponent(cat)}` : "");
}

function triggerDownload(url) {
  const a = document.createElement("a");
  a.href = url;
  a.setAttribute("download", "");
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function exportOne(fmt, dir) {
  hidePopmenus();
  const d = dir || curWorkdir;
  if (!d) return;
  triggerDownload(exportUrl(fmt, d));
  toast(fmt === "html"
    ? "✦ 正在导出 HTML 单文件（内联样式，可以直接发给别人）"
    : fmt === "txt" ? "✦ 正在导出纯文字稿" : "✦ 正在导出 Markdown");
}

/** 整库打包：勾选当前筛选范围（某个分类时只打包那个分类） */
function exportAll() {
  const cat = isSmart(activeCat) || activeCat === "all" || activeCat === "none" ? "" : activeCat;
  const c = cat ? catById(cat) : null;
  showModal({
    title: "打包导出全部文章？",
    desc: c ? `只打包分类「${c.name}」里的文章。` : "把当前所有文章打包成一个 zip。",
    withInput: false,
    okText: "打包下载",
    bodyHtml: `<label class="opt"><input type="checkbox" id="ziptrans" checked>
      <span><span class="ot">包含文字稿</span><span class="od">每篇的 transcript.txt 一起放进去（文件会大一些）</span></span></label>`,
    onOk: () => {
      const withT = !$("ziptrans") || $("ziptrans").checked;
      triggerDownload(bundleUrl().replace("transcript=1", "transcript=" + (withT ? "1" : "0")));
      toast("✦ 正在打包，文件较大时需要几秒");
    },
  });
}

async function copyArticle() {
  hidePopmenus();
  const text = ($("article").innerText || "").trim();
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("✦ 正文已复制到剪贴板");
  } catch (e) {
    toast("⚠ 浏览器不允许直接写剪贴板，请手动选中复制");
  }
}

/* ---------------- 全文检索 ---------------- */
let searchQuery = "", searchTimer = null;

function onSearchInput() {
  const v = $("q").value.trim();
  $("q").parentElement.classList.toggle("has-text", !!v);
  clearTimeout(searchTimer);
  // 输入停顿 260ms 再查，避免每个字都打一次接口
  searchTimer = setTimeout(() => { if (v) runSearch(v); else clearSearch(); }, 260);
}

async function runSearch(q) {
  searchQuery = q;
  showView("lib");
  const d = await (await fetch("/api/search?q=" + encodeURIComponent(q))).json();
  renderSearch(d);
}

function clearSearch(keepFilter) {
  searchQuery = "";
  $("q").value = "";
  $("q").parentElement.classList.remove("has-text");
  showView("lib");
  if (!keepFilter) renderCatbar();
  renderGrid();
}

const tsToSec = (ts) => (ts || "").split(":").reduce((a, b) => a * 60 + Number(b || 0), 0);

function searchCardHTML(it) {
  const hits = (it.hits || []).map((h) => {
    const field = h.field === "transcript" ? "文字稿" : h.field === "title" ? "标题" : "正文";
    const ts = h.ts
      // 这里必须内联处理：stopPropagation 会连 document 上的委托监听一起挡掉，
      // 所以不能只写 stopPropagation 把播放交给全局委托（那样点了不会出声）。
      ? `<a class="ts" data-sec="${tsToSec(h.ts)}" data-dir="${esc(it.dir)}" title="跳到音频此处"
            onclick="event.stopPropagation();playFromTs(this)">[${esc(h.ts)}]</a> `
      : "";
    return `<div class="srhit"><span class="srfield">${field}</span>${ts}<span class="srtext">${esc(h.before)}<mark>${esc(h.match)}</mark>${esc(h.after)}</span></div>`;
  }).join("");
  return `
  <div class="sr" data-dir="${esc(it.dir)}" onclick="openEpisode('${encodeURIComponent(it.dir)}')">
    <div class="srhead">
      <div class="srtitle">${esc(it.title)}</div>
      <span class="srmeta">${[it.podcast, `${it.match_count} 处命中`, statusLabel(it.status || "unread")].filter(Boolean).map(esc).join(" · ")}</span>
    </div>
    ${hits}
  </div>`;
}

function renderSearch(d) {
  const items = d.items || [];
  // 搜索时左侧筛选不参与，去掉高亮避免误导
  document.querySelectorAll("#catnav .folder.on").forEach((f) => f.classList.remove("on"));
  document.querySelectorAll("#quicktabs button").forEach((b) => b.classList.remove("on"));
  $("libtitle").textContent = `搜索「${d.query}」`;
  $("libcount").textContent = items.length
    ? `命中 ${items.length} 篇 · 已索引 ${(d.stats || {}).indexed || 0} 集`
    : "";
  const empty = $("empty-lib");
  if (!items.length) {
    $("libgrid").innerHTML = "";
    empty.textContent = `没有找到「${d.query}」。试试更短的词；用 | 表示「或」（例如 强化学习|RL）。`;
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";
  $("libgrid").innerHTML = items.map(searchCardHTML).join("");
}

/* ---------------- 批量队列 ---------------- */
let queueState = null;

async function loadQueue() {
  try {
    renderQueueView(await (await fetch("/api/queue")).json());
  } catch (e) { /* 服务暂时不可用时不打断界面 */ }
}

function renderQueueView(d) {
  queueState = d;
  setNavCount("navqcount", d.active || 0);
  const items = d.items || [];
  $("empty-queue").style.display = items.length ? "none" : "block";
  $("queuelist").innerHTML = items.map(queueRowHTML).join("");
}

function queueRowHTML(it) {
  const labels = (queueState && queueState.labels) || {};
  const acts = [];
  if (it.state === "pending") {
    acts.push(`<button class="btn mini" title="上移" onclick="queueMove('${it.id}',-1)">↑</button>`);
    acts.push(`<button class="btn mini" title="下移" onclick="queueMove('${it.id}',1)">↓</button>`);
  }
  if (it.dir) acts.push(`<button class="btn mini" onclick="openEpisode('${encodeURIComponent(it.dir)}')">看文章</button>`);
  acts.push(`<button class="btn mini" onclick="queueRemove('${it.id}')">删除</button>`);
  return `
  <div class="qrow" data-id="${esc(it.id)}">
    <span class="qstate ${esc(it.state)}">${esc(labels[it.state] || it.state)}</span>
    <div class="qmain">
      <div class="qtitle">${esc(it.title || it.url)}</div>
      <div class="qurl">${esc(it.url)}${it.pick > 1 ? `　（第 ${it.pick} 集）` : ""}</div>
      ${it.error ? `<div class="qerr">${esc(it.error)}</div>` : ""}
    </div>
    <div class="qacts">${acts.join("")}</div>
  </div>`;
}

async function queueRun() {
  const resp = await fetch("/api/queue/run", { method: "POST" });
  const d = await resp.json();
  if (!resp.ok) { await loadQueue(); return; }   // 有任务在跑 / 队列空，都不算错误
  if (d.job_id) beginJob(d.job_id);
  loadQueue();
}

async function queueRemove(id) {
  await fetch("/api/queue/" + encodeURIComponent(id), { method: "DELETE" });
  loadQueue();
}

async function queueMove(id, delta) {
  await fetch(`/api/queue/${encodeURIComponent(id)}/move`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ delta }),
  });
  loadQueue();
}

async function queueRetry() {
  const d = await (await fetch("/api/queue/retry", { method: "POST" })).json();
  renderQueueView(d.queue);
  toast(d.retried ? `✦ ${d.retried} 条失败的已重新排队` : "没有失败的条目");
  if (d.retried) queueRun();
}

function queueClear(keepFailed) {
  showModal({
    title: "清空队列？",
    desc: keepFailed ? "只清掉已完成的，失败的会留着。" : "清掉已完成与失败的条目；排队中和正在生成的会保留。",
    withInput: false,
    danger: true,
    okText: "清空",
    onOk: async () => {
      const d = await (await fetch("/api/queue/clear", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keep_failed: !!keepFailed }),
      })).json();
      renderQueueView(d.queue);
      toast(d.removed ? `✦ 已清掉 ${d.removed} 条` : "没有可清理的条目");
    },
  });
}

/** 弹窗里一次粘多条链接 */
function openBatch() {
  showModal({
    title: "批量添加链接",
    desc: "一行一条，从聊天记录里直接粘一串也可以。会按顺序依次生成。",
    withInput: false,
    okText: "加入队列",
    bodyHtml: `<textarea id="batchtext" class="modaltarea" spellcheck="false"
      placeholder="https://www.xiaoyuzhoufm.com/episode/…&#10;https://www.youtube.com/watch?v=…&#10;podcasts.apple.com/…/id…"></textarea>
      <p class="modalhint">每条都会走完整流程：抓元信息 → 下载音频 → 获取文字稿 → 成文。
      文字稿会缓存，之后重新生成文章不必重新转写。</p>`,
    onOk: async () => {
      const text = ($("batchtext") || {}).value || "";
      await enqueueLinks([text]);
    },
  });
}

/* ---------------- 订阅 ---------------- */
let feedState = null;

function relTime(ts) {
  if (!ts) return "从未";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}

async function loadFeeds() {
  try {
    renderFeeds(await (await fetch("/api/feeds")).json());
  } catch (e) { /* 静默 */ }
}

function renderFeeds(d) {
  feedState = d;
  const feeds = d.feeds || [];
  setNavCount("navfcount", feeds.length);
  $("empty-feeds").style.display = feeds.length ? "none" : "block";
  $("feedlist").innerHTML = feeds.map(feedRowHTML).join("");
}

function feedRowHTML(f) {
  const bits = [`上次检查：${relTime(f.last_checked)}`, `已知 ${f.seen_count || 0} 集`];
  if (f.backfill) bits.push(`首次补跑 ${f.backfill} 集`);
  return `
  <div class="frow" data-id="${esc(f.id)}">
    <div class="fmain">
      <div class="ftitle">${esc(f.title || f.url)}
        <span class="ftag">${f.auto ? "自动生成" : "仅发现新单集"}</span></div>
      <div class="furl">${esc(f.url)}</div>
      <div class="fmeta">${esc(bits.join(" · "))}</div>
      ${f.last_error ? `<div class="ferr">上次抓取失败：${esc(f.last_error)}</div>` : ""}
    </div>
    <div class="facts">
      <label class="fswitch">自动生成
        <input type="checkbox" ${f.auto ? "checked" : ""} onchange="toggleFeedAuto('${f.id}', this.checked)"></label>
      <button class="btn mini" onclick="delFeed('${f.id}')">删除</button>
    </div>
  </div>`;
}

function openAddFeed() {
  showModal({
    title: "添加订阅",
    desc: "填 RSS 链接或 Apple Podcasts 节目链接。",
    withInput: true,
    placeholder: "https://feeds.example.com/show.xml 或 podcasts.apple.com/…/id123",
    okText: "订阅",
    bodyHtml: `<label class="opt"><input type="number" id="feedbackfill" min="0" max="20" value="0" style="width:78px">
      <span><span class="ot">同时补跑最新 N 集</span>
      <span class="od">0 = 只关注之后更新的新单集，不追溯历史</span></span></label>`,
    onOk: async (url) => addFeed(url),
  });
}

async function addFeed(url) {
  const el = $("feedbackfill");
  const backfill = Math.max(0, Math.min(20, parseInt((el && el.value) || "0", 10) || 0));
  const resp = await fetch("/api/feeds", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, backfill, auto: true }),
  });
  const d = await resp.json();
  if (!resp.ok) { toast("⚠ " + esc(d.error || "订阅失败")); return; }
  toast(`✦ 已订阅：${esc((d.feed || {}).title || url)}${d.enqueued ? ` · 已排队 ${d.enqueued} 集` : ""}`);
  renderFeeds(d.feeds);
  if (d.enqueued) queueRun();
}

async function toggleFeedAuto(id, on) {
  const resp = await fetch("/api/feeds/" + encodeURIComponent(id), {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auto: on }),
  });
  const d = await resp.json();
  if (!resp.ok) { toast("⚠ " + esc(d.error || "修改失败")); loadFeeds(); return; }
  renderFeeds(d.feeds);
}

function delFeed(id) {
  const f = ((feedState || {}).feeds || []).find((x) => x.id === id) || {};
  showModal({
    title: `取消订阅「${f.title || id}」？`,
    desc: "只是不再检查这个节目，已经生成的文章不受影响。",
    withInput: false,
    danger: true,
    okText: "取消订阅",
    onOk: async () => {
      const d = await (await fetch("/api/feeds/" + encodeURIComponent(id), { method: "DELETE" })).json();
      renderFeeds(d);
      toast("✦ 已取消订阅");
    },
  });
}

async function checkFeeds(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "检查中…"; }
  try {
    const d = await (await fetch("/api/feeds/check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enqueue: true }),
    })).json();
    if (d.error) { toast("⚠ " + esc(d.error)); return; }
    toast(d.found
      ? `✦ 发现 ${d.found} 集新内容${d.enqueued ? `，已排队 ${d.enqueued} 集` : "（未自动排队）"}`
      : "✦ 没有新单集");
    if (d.feeds) renderFeeds(d.feeds);
    else loadFeeds();
    if (d.enqueued) queueRun();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "立即检查"; }
  }
}

/* ================= AI 阅读助手（悬浮球 / 选中深挖 / 右侧抽屉） =================
   设计意图：读文章时卡住的地方，不该切出去另开一个对话窗口。
   选中 → 就地提问 → 依据（原文片段带时间戳，可点回听）+ 网络结果 + 流式解读。
   原文片段的时间戳能点着回听是这个功能相对「通用聊天机器人」的核心差别。 */

let assistOpen = false, assistES = null, assistAskId = null;
let assistSelection = "", assistDir = null;

function fabShouldShow() {
  // 有文章在读、设置里没关掉助手、且抽屉没开着时才出现。
  // 把「抽屉开着时不显示」放在这里而不是只靠 CSS：一处判断，测试也好断言
  // （CSS 那句 body.assistopen .fab 保留，作为双保险）。
  return $("result").classList.contains("show") && assistEnabled && !assistOpen;
}
let assistEnabled = true;

function syncFab() {
  $("fab").classList.toggle("show", fabShouldShow());
  document.body.classList.toggle("assistopen", assistOpen);
  document.body.classList.toggle("withplayer", $("player").classList.contains("show"));
}

function openAssist(preset) {
  const ex = $("assist");
  if (ex.classList.contains("show")) return;
  assistOpen = true;
  assistDir = curWorkdir;
  ex.classList.add("show");
  syncFab();
  if (preset && preset.selection !== undefined) setAssistSelection(preset.selection || "");
  loadAssistHistory();
  setTimeout(() => $("aq").focus(), 60);
}

function closeAssist() {
  assistOpen = false;
  stopAssistStream();
  $("assist").classList.remove("show");
  syncFab();
}

function toggleAssist() {
  if (assistOpen) closeAssist();
  // 悬浮球兜底：拿不到选区（例如在别处点了鼠标）时用最近一次记住的选文
  else openAssist({ selection: currentArticleSelection() || lastSelection });
}

/** 抽屉里当前展示的「选中原文」。传空串表示清掉（之后只按问题回答）。 */
function setAssistSelection(text) {
  assistSelection = (text || "").trim();
  const box = $("aquote");
  if (assistSelection) {
    $("aqtext").textContent = assistSelection;
    box.style.display = "block";
  } else {
    box.style.display = "none";
    lastSelection = "";      // 一起清掉，否则点悬浮球又会把刚才那段捞回来
  }
}

/** 用户在文章/文字稿里选中的文字（只认正文区域，避免把界面文字也带走） */
function currentArticleSelection() {
  const sel = window.getSelection ? window.getSelection() : null;
  if (!sel || sel.isCollapsed || !sel.rangeCount) return "";
  const text = String(sel.toString() || "").trim();
  if (!text) return "";
  const node = sel.getRangeAt(0).commonAncestorContainer;
  const el = node.nodeType === 1 ? node : node.parentElement;
  if (!el || !el.closest("#article, #transcript")) return "";
  return text.slice(0, 3000);
}

/* ---- 选中后浮出「深挖这段」按钮 ---- */
let selBtnTimer = null;
let lastSelection = "";     // 浮出按钮时就把选中的文字存下来

function positionSelBtn() {
  const btn = $("selbtn");
  const sel = window.getSelection ? window.getSelection() : null;
  if (!sel || sel.isCollapsed || !sel.rangeCount) { btn.classList.remove("show"); return; }
  const range = sel.getRangeAt(0);
  const node = range.commonAncestorContainer;
  const el = node.nodeType === 1 ? node : node.parentElement;
  if (!el || !el.closest("#article, #transcript")) { btn.classList.remove("show"); return; }
  const rect = range.getBoundingClientRect ? range.getBoundingClientRect() : null;
  if (!rect || (!rect.width && !rect.height)) { btn.classList.remove("show"); return; }
  // 在这里就把文本存下来：点按钮时浏览器可能已经把选区收掉了
  // （mousedown 落在按钮上会折叠选区），到那时再读 getSelection() 会拿到空串。
  lastSelection = String(sel.toString() || "").trim().slice(0, 3000);
  if (!lastSelection) { btn.classList.remove("show"); return; }
  btn.classList.add("show");
  const top = rect.top + window.scrollY - btn.offsetHeight - 8;
  const left = rect.left + window.scrollX + rect.width / 2 - btn.offsetWidth / 2;
  btn.style.top = Math.max(8, top) + "px";
  btn.style.left = Math.max(8, Math.min(left, window.innerWidth - btn.offsetWidth - 8)) + "px";
}

function onArticleSelectionChange() {
  clearTimeout(selBtnTimer);
  // 等选区稳定（拖动选取会连续触发）
  selBtnTimer = setTimeout(positionSelBtn, 130);
}

/** 点浮出按钮：把选中的文字带进抽屉并直接开问 */
function askSelection() {
  const text = lastSelection || currentArticleSelection();
  $("selbtn").classList.remove("show");
  openAssist({ selection: text });
  if (text) { setAssistSelection(text); askAI(); }
}

/* ---- 提问 ---- */
function askAI() {
  if (!curWorkdir) { toast("⚠ 先打开一篇文章"); return; }
  const question = ($("aq").value || "").trim();
  const selection = assistSelection || currentArticleSelection();
  if (!selection && !question) {
    toast("⚠ 先在文章里选一段文字，或者写一个问题");
    $("aq").focus();
    return;
  }
  assistDir = curWorkdir;
  setAssistSelection(selection);
  renderAssistAnswer("", true);
  $("asources").style.display = "none";
  $("agobtn").disabled = true;
  $("astatus").textContent = "正在找原文依据…";

  fetch("/api/ask", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      dir: curWorkdir, selection, question,
      web: $("aweb_toggle").checked,
    }),
  }).then(async (resp) => {
    const d = await resp.json();
    if (!resp.ok) {
      $("agobtn").disabled = false;
      $("astatus").textContent = "";
      renderAssistAnswer("", false);
      renderAssistError(d.error || "提问失败");
      return;
    }
    assistAskId = d.id;
    connectAssistStream(d.id);
  }).catch((e) => {
    $("agobtn").disabled = false;
    $("astatus").textContent = "";
    renderAssistAnswer("", false);
    renderAssistError(String(e));
  });
}

function connectAssistStream(id) {
  stopAssistStream();
  let text = "";
  const useSSE = typeof EventSource !== "undefined";
  const onDelta = (t) => {
    text += t;
    renderAssistAnswer(text, true);
  };
  const finish = (d) => {
    $("agobtn").disabled = false;
    $("astatus").textContent = "";
    renderAssistAnswer(text || d.answer || "", false);
    if (d.error && !(text || d.answer)) renderAssistError(d.error);
    if (d.sources) renderAssistSources(d.sources);
    loadAssistHistory();
  };

  if (useSSE) {
    assistES = new EventSource("/api/ask/" + encodeURIComponent(id) + "/stream");
    assistES.addEventListener("sources", (e) => {
      $("astatus").textContent = "正在写解读…";
      renderAssistSources(JSON.parse(e.data));
    });
    assistES.addEventListener("delta", (e) => onDelta(JSON.parse(e.data).text || ""));
    assistES.addEventListener("done", (e) => {
      stopAssistStream();
      finish(JSON.parse(e.data));
    });
    assistES.addEventListener("error", (e) => {
      // 服务端的 error 事件带 data，连接层面的错误没有
      if (e && e.data) { stopAssistStream(); finish(Object.assign({ error: "提问失败" }, JSON.parse(e.data))); }
    });
    assistES.onerror = () => {              // SSE 断了就退回轮询
      if (!assistES) return;
      stopAssistStream();
      pollAssist(id, text, finish, onDelta);
    };
  } else {
    pollAssist(id, text, finish, onDelta);
  }
}

/** SSE 不可用时的兜底：轮询同一个提问任务，增量靠已渲染文本的长度推算 */
function pollAssist(id, text, finish, onDelta) {
  let seen = text.length;
  const timer = setInterval(async () => {
    try {
      const d = await (await fetch("/api/ask/" + encodeURIComponent(id))).json();
      const full = d.answer || "";
      if (full.length > seen) { onDelta(full.slice(seen)); seen = full.length; }
      if (d.sources) renderAssistSources(d.sources);
      if (d.status !== "running") {
        clearInterval(timer);
        finish({ answer: full, error: d.error || "", sources: d.sources });
      }
    } catch (e) {
      clearInterval(timer);
      finish({ answer: text, error: String(e) });
    }
  }, 900);
}

function stopAssistStream() {
  if (assistES) { assistES.close(); assistES = null; }
}

/* ---- 渲染 ---- */
function renderAssistAnswer(md, streaming) {
  const box = $("aanswer");
  $("aanswerbox").style.display = "block";
  $("aintro").style.display = "none";
  box.classList.toggle("streaming", !!streaming);
  box.innerHTML = mdLite(md);
  if (streaming) $("abody").scrollTop = $("abody").scrollHeight;
}

function renderAssistError(msg) {
  const box = $("aanswer");
  $("aanswerbox").style.display = "block";
  $("aintro").style.display = "none";
  box.classList.remove("streaming");
  box.innerHTML = `<p class="aerr">✕ ${esc(msg)}</p>`;
}

function renderAssistSources(src) {
  const passages = (src && src.passages) || [];
  const web = src && src.web;
  const wrap = $("asources");

  if (passages.length) {
    $("apcount").textContent = `${passages.length} 段`;
    $("apassages").innerHTML = passages.map((p) => `
      <div class="apass">
        ${p.ts ? `<a class="ts" data-sec="${tsToSec(p.ts)}" data-dir="${esc(assistDir || curWorkdir || "")}"
              title="跳到音频此处" onclick="event.preventDefault();playFromTs(this)">[${esc(p.ts)}]</a>` : ""}
        <span class="aptext">${esc(p.text || "")}</span>
      </div>`).join("");
  } else {
    $("apcount").textContent = "没找到相关段落";
    $("apassages").innerHTML = `<div class="anoweb">这一集的文字稿里没有和这段明显相关的段落，下面的解读只能是背景补充。</div>`;
  }

  if (web) {
    $("aweblabel").style.display = "flex";
    if (web.ok && (web.results || []).length) {
      $("awebcount").textContent = `${web.results.length} 条 · ${web.provider || ""}`;
      $("aweb").innerHTML = web.results.map((r) => `
        <a class="aweb" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">
          <span class="awt">${esc(r.title || r.url)}</span>
          <span class="awu">${esc(r.url)}</span>
          ${r.snippet ? `<span class="aws">${esc(r.snippet)}</span>` : ""}
        </a>`).join("");
    } else {
      $("awebcount").textContent = "未取到";
      $("aweb").innerHTML = `<div class="anoweb">这次没联网成功（${esc(web.error || "原因未知")}）——
        解读只基于播客原文。可以在设置里换一个搜索服务。</div>`;
    }
  }
  wrap.style.display = "block";
}

/** 极简 markdown → HTML（抽屉里够用：段落 / 粗体 / 行内码 / 引用 / 列表 / 链接 / 时间戳） */
function mdLite(md) {
  if (!md) return "";
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\[(\d{1,2}:\d{2}:\d{2})\]/g,
      (_m, t) => `<a class="ts" data-sec="${tsToSec(t)}" data-dir="${esc(assistDir || curWorkdir || "")}"
        title="跳到音频此处" onclick="event.preventDefault();playFromTs(this)">[${t}]</a>`);
  const out = [];
  let para = [], list = [];
  const flushP = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
  const flushL = () => { if (list.length) { out.push(`<ul>${list.map((li) => `<li>${inline(li)}</li>`).join("")}</ul>`); list = []; } };
  for (const raw of md.split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { flushP(); flushL(); continue; }
    if (/^>\s?/.test(line)) { flushP(); flushL(); out.push(`<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`); continue; }
    if (/^\s*[-*]\s+/.test(line)) { flushP(); list.push(line.replace(/^\s*[-*]\s+/, "")); continue; }
    if (/^#{1,6}\s+/.test(line)) { flushP(); flushL(); out.push(`<p><strong>${inline(line.replace(/^#{1,6}\s+/, ""))}</strong></p>`); continue; }
    flushL(); para.push(line.trim());
  }
  flushP(); flushL();
  return out.join("");
}

/* ---- 历史问答 ---- */
function toggleAssistHistory() {
  const box = $("ahistory");
  const show = box.style.display === "none";
  box.style.display = show ? "block" : "none";
  if (show) loadAssistHistory();
}

async function loadAssistHistory() {
  if (!curWorkdir) return;
  try {
    const d = await (await fetch("/api/qa?dir=" + encodeURIComponent(curWorkdir))).json();
    const items = d.items || [];
    $("ahcount").textContent = items.length ? `${items.length} 条（最多留 ${d.max} 条）` : "还没有";
    $("ahlist").innerHTML = items.length
      ? items.map((it) => `
        <div class="ahitem" data-id="${esc(it.id)}">
          <div class="ahq" onclick="toggleAhItem(this)">${esc(it.question || it.selection || "(无标题)")}</div>
          <div class="ahs">${esc((it.answer || "").slice(0, 300))}</div>
          <div class="ahacts">
            <button onclick="restoreAssistItem('${esc(it.id)}')">重新查看</button>
            <button onclick="deleteAssistItem('${esc(it.id)}')">删除</button>
          </div>
        </div>`).join("")
      : `<div class="anoweb">这一集还没有问答记录。</div>`;
  } catch (e) { /* 服务不可用时静默 */ }
}

function toggleAhItem(el) { el.parentElement.classList.toggle("open"); }

async function restoreAssistItem(id) {
  if (!curWorkdir) return;
  const d = await (await fetch("/api/qa?dir=" + encodeURIComponent(curWorkdir))).json();
  const it = (d.items || []).find((x) => x.id === id);
  if (!it) return;
  setAssistSelection(it.selection || "");
  $("aq").value = it.question || "";
  $("aanswerbox").style.display = "block";
  $("aintro").style.display = "none";
  renderAssistAnswer(it.answer || "", false);
  renderAssistSources({ passages: it.passages || [], web: it.web });
  $("abody").scrollTop = 0;
}

async function deleteAssistItem(id) {
  if (!curWorkdir) return;
  await fetch(`/api/qa/${encodeURIComponent(curWorkdir)}/${encodeURIComponent(id)}`, { method: "DELETE" });
  loadAssistHistory();
}

function clearAssistHistory() {
  if (!curWorkdir) return;
  showModal({
    title: "清空这一集的问答记录？",
    desc: "只是删掉阅读助手里的问答，文章和文字稿不受影响。",
    withInput: false, danger: true, okText: "清空",
    onOk: async () => {
      await fetch("/api/qa/" + encodeURIComponent(curWorkdir), { method: "DELETE" });
      loadAssistHistory();
      toast("✦ 已清空");
    },
  });
}

/** 设置页的联网搜索服务状态 */
async function loadSearchService() {
  const box = $("as_providers");
  if (!box) return;
  try {
    const d = await (await fetch("/api/search-service")).json();
    const provs = d.providers || {};
    box.innerHTML = Object.entries(provs).map(([name, p]) => `
      <div class="inforow">
        <span class="k">${esc(p.label || name)}${p.needs_key ? "（需密钥）" : "（免密钥）"}</span>
        <span class="v">${p.configured ? (name === d.default ? "✓ 当前使用" : "可用") : "未配置"}</span>
      </div>`).join("");
    $("as_state").textContent = d.enabled ? `当前：${d.default}` : "已彻底关闭";
    $("as_state").className = "fstate" + (d.enabled ? " ok" : "");
  } catch (e) {
    box.innerHTML = `<div class="inforow"><span class="k">读取失败</span><span class="v">${esc(String(e))}</span></div>`;
  }
}

async function testSearchService(btn) {
  const out = $("as_testres");
  btn.disabled = true;
  out.className = "vres"; out.textContent = "搜索中…";
  try {
    const typed = { TAVILY_API_KEY: ($("as_tavily").value || "").trim(),
                    SERPER_API_KEY: ($("as_serper").value || "").trim() };
    const secrets = Object.fromEntries(Object.entries(typed).filter(([, v]) => v));
    if (Object.keys(secrets).length) {
      await fetch("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ secrets }),
      });
      await loadSearchService();
    }
    const d = await (await fetch("/api/search-service/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: "DeepSeek" }),
    })).json();
    if (d.ok) {
      out.className = "vres ok";
      out.textContent = `✓ ${d.provider} 返回 ${d.results.length} 条：` +
        (d.results[0] ? d.results[0].title.slice(0, 40) : "");
    } else {
      out.className = "vres bad";
      out.textContent = "✕ " + (d.error || "没有结果");
    }
  } catch (e) {
    out.className = "vres bad"; out.textContent = "✕ " + String(e);
  }
  btn.disabled = false;
}

/* ---------------- 初始化 ---------------- */
// 首页输入框是 textarea：<input type="text"> 按规范会**丢掉换行**，
// 一次粘多条链接会被粘成一条（实测被 UI 测试抓到），所以必须用多行控件。
$("url").addEventListener("input", () => { updateComposerHint(); autoGrow(); });
$("url").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); startRun(); }   // Shift+Enter 才换行
});
$("q").addEventListener("input", onSearchInput);
$("q").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  clearTimeout(searchTimer);
  const v = $("q").value.trim();
  if (v) runSearch(v); else clearSearch();
});

/** 页面刷新时如果后台还在跑，直接接上进度（不然用户会以为任务没了） */
async function pollCurrentJob() {
  try {
    const d = await (await fetch("/api/jobs/current")).json();
    if (d.status === "running" && d.job_id) beginJob(d.job_id);
  } catch (e) { /* 静默 */ }
}

loadLibrary();
loadMcp();
loadSettings();
loadQueue();
loadFeeds();
pollCurrentJob();
updateComposerHint();
autoGrow();
syncFab();
// 抽屉里的输入框也随内容长高
$("aq").addEventListener("input", () => {
  const el = $("aq");
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 110) + "px";
});
$("aq").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); askAI(); }
});

// 队列页开着时轻量刷新；侧边栏计数也顺带更新
setInterval(async () => {
  if (activeView !== "queue") return;
  loadQueue();
}, 5000);
setInterval(() => loadLibrary(), 30000);
