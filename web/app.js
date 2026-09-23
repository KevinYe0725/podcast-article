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

/* ---------------- 知识库（AI 的底座，界面里不出现）----------------
   后端是 podcast_article/kb.py：SQLite FTS5 + jieba 分词（中文必须自己切词），
   有嵌入模型时再加向量检索做混合排序。

   产品定位：知识库是**服务于 AI** 的，不是让人来搜索的，所以它不是用户的入口 ——
   界面上没有「知识库」这个页面：粘链接就生成文章，打字提问就「问你的库」，AI 自己
   去库里检索、把用户存过的记忆带上，回答里的时间戳可以直接回听。「切片 / 索引 /
   实体」这些词一个都不该出现在用户面前，只有设置页里留了一个重建索引的折叠区。 */

let kbStatusCache = null;       // GET /api/kb/status 的结果（问句提示与设置页共用）

/** 书库规模：拿不到就返回 null，界面退化成只说「问你的库」，不报错也不假装知道。 */
async function kbStatus(force) {
  if (kbStatusCache && !force) return kbStatusCache;
  try {
    const d = await (await fetch("/api/kb/status")).json();
    if (d && !d.error) kbStatusCache = d;
  } catch (e) { /* 后端不可用：静默 */ }
  return kbStatusCache;
}

/** 「（12 集 · 5612 条切片）」——规模本身就是「问你的库」值不值得问的依据 */
function kbScaleText() {
  const d = kbStatusCache;
  if (!d) return "";
  const parts = [];
  if (d.episodes_on_disk != null) parts.push(`${d.episodes_on_disk} 集`);
  if (d.passages != null) parts.push(`${d.passages} 条切片`);
  return parts.length ? `（${parts.join(" · ")}）` : "";
}

const mmssShort = (sec) => (sec == null ? "" :
  `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, "0")}`);

function kbHitHTML(h, i) {
  const ts = h.start_sec != null
    ? `<a class="ts" data-sec="${Math.round(h.start_sec)}" data-dir="${esc(h.dir)}" role="button" tabindex="0"
         title="跳到音频此处" onclick="event.stopPropagation();playFromTs(this)">[${mmssShort(h.start_sec)}]</a> `
    : "";
  return `<div class="kbhit">
    <div class="kbhhead">
      ${h.sources ? "" : ""}<span class="kbidx">[${i}]</span>
      <span class="kbtitle" title="${esc(h.title || "")}">${esc((h.title || "").slice(0, 40))}</span>
      ${h.podcast ? `<span class="dimtext">${esc(h.podcast)}</span>` : ""}
      ${h.doc_kind === "transcript" ? `<span class="dimtext">文字稿</span>` : ""}
      <span style="flex:1"></span>
      ${ts}
    </div>
    ${h.heading ? `<div class="kbhead2">${esc(h.heading)}</div>` : ""}
    <div class="kbtext">${esc(h.text || "")}</div>
    <div class="kbacts"><button class="ttslink" onclick="openEpisode('${encodeURIComponent(h.dir)}')">打开这一篇 →</button></div>
  </div>`;
}

/* ---- 「问你的库」：一轮轮接下去的对话流 ----
   两段式加载是这里的重点：检索很快，模型很慢。所以先把**出处**摆出来（用户马上
   能核对、能点时间戳回听），答案那一趟回来再填进去 —— 不用对着空白等几十秒。 */

function openAskPanel() {
  const p = $("askpanel");
  if (p) p.style.display = "block";
}

function closeAskPanel() {
  const p = $("askpanel");
  if (p) p.style.display = "none";
}

/** 新的一轮：先把问题与状态行摆出来，出处、答案、记忆随后各自填进自己的格子 */
function appendAskTurn(question) {
  const wrap = document.createElement("div");
  wrap.className = "askturn";
  wrap.innerHTML = `<div class="askq">${esc(question)}</div>
    <div class="askstate dimtext">正在检索…</div>
    <div class="asksrc"></div>
    <div class="askans"></div>
    <div class="askacts"></div>`;
  openAskPanel();
  $("askthread").appendChild(wrap);
  return wrap;
}

/** 出处：默认收起的 <details>，时间戳仍然可以点回听（复用 kbHitHTML） */
function askSourcesHTML(d) {
  const hits = (d && d.hits) || [];
  if (!hits.length) return "";
  return `<details class="kbsrc"><summary>出处（${hits.length} 条，时间戳可点回听）</summary>`
    + hits.map((h, i) => kbHitHTML(h, i)).join("") + `</details>`;
}

function renderAskAnswer(turn, d) {
  const ans = turn.querySelector(".askans");
  const acts = turn.querySelector(".askacts");
  if (d.error && !d.answer) {           // 失败只影响答案，已经拿到的出处留在原地
    ans.innerHTML = `<div class="askerr">✕ ${esc(d.error)}</div>`;
    acts.innerHTML = "";
    return;
  }
  // 「AI 记得的你」也收进折叠块：记忆跑偏时用户才需要去看，平时不必占着视线
  ans.innerHTML = `<div class="kbansbody">${mdLite(d.answer || "")}</div>` + memNoteHTML(d.memory_used);
  acts.innerHTML = `<div class="kbaacts"><button class="ttslink" data-dir="" onclick="rememberAnswer(this)">☆ 记住这个结论</button></div>`;
  // 跨很多集，没有单一 dir，data-dir 写成空串（后端接口的 dir 允许为空）
  wireAnswerBtn(turn, d.answer);
}

async function askLibrary() {
  const question = ($("url").value || "").trim();
  if (question.length < 2) return;
  $("url").value = "";
  updateComposerHint();
  autoGrow();
  const turn = appendAskTurn(question);
  const state = turn.querySelector(".askstate");
  const srcBox = turn.querySelector(".asksrc");

  // 第一段：检索（快）—— 回来就立刻把出处渲染出来
  try {
    const d = await (await fetch("/api/kb/search?q=" + encodeURIComponent(question) + "&k=6")).json();
    if (!d.error) srcBox.innerHTML = askSourcesHTML(d);
  } catch (e) { /* 检索失败不拦住问答：模型那一趟自己也会检索 */ }
  state.textContent = "正在回答…";

  // 第二段：让模型基于检索到的原文作答（慢，几秒到几十秒）
  try {
    const resp = await fetch("/api/kb/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, k: 6 }),
    });
    const d = await resp.json();
    renderAskAnswer(turn, d);
    // 检索那一趟失败/没命中时兜底：问答返回里也带着出处，别让「出处」整块空着
    if (!srcBox.innerHTML.trim()) srcBox.innerHTML = askSourcesHTML({ hits: d.sources });
    state.textContent = d.error && !d.answer ? "模型不可用，上面是检索到的出处" : "完成";
  } catch (e) {
    turn.querySelector(".askans").innerHTML = `<div class="askerr">✕ ${esc(String(e))}</div>`;
    state.textContent = "失败";
  }
}

/* ---- 知识库的维护入口（设置 → 记忆，默认收起）----
   界面里不再有知识库页面，但索引坏了必须有地方能修：状态行 + 重建索引 +
   索引进度轮询都收在 #kbadmin 这一块里，打开时才拉状态。 */

async function kbReindex(force) {
  if (force && !confirm("重建索引会重新切片并重算向量，可能要一会儿。继续？")) return;
  const resp = await fetch("/api/kb/reindex", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force: !!force }),
  });
  const d = await resp.json();
  if (!resp.ok) { toast("⚠ " + esc(d.error || "索引失败")); return; }
  toast("✦ 开始索引，完成后会自动刷新");
  setTimeout(() => { kbStatusCache = null; loadKbAdmin(); }, 1200);
}

async function loadKbAdmin() {
  const el = $("kb_setstate");
  if (!el) return;
  el.textContent = "正在读取状态…";
  const d = await kbStatus(true);
  if (!d) { el.textContent = "读取状态失败（后端不可用）"; return; }
  const mode = d.semantic ? "开" : "关";
  el.innerHTML = `${d.episodes_on_disk ?? 0} 集 · ${d.passages ?? 0} 条切片 · `
    + `${d.vectors ?? 0} 条向量 · 语义检索 ${mode}`
    + (!d.semantic && d.embed_error
      ? `<br><span class="dimtext">语义检索暂不可用：${esc(String(d.embed_error).slice(0, 120))}</span>` : "");
  // 索引在跑：接着轮询，跑到完为止（这一块就是原来知识库页面的那条轮询）
  if (d.indexing && d.indexing.state === "running") {
    el.innerHTML += ` <span class="fstate">正在索引 ${d.indexing.done}/${d.indexing.total}…</span>`;
    setTimeout(() => { kbStatusCache = null; loadKbAdmin(); }, 1500);
  }
}

/* ---------------- 记忆 ----------------
   每条记忆都会在跨集问答时被读进上下文（置顶的永远参与）。
   只写不猜：要么手写，要么显式点「记住这条」，程序不偷偷记。 */

/* kind 的中文标签：设置页的下拉、搜索命中的标签、记忆条目共用一张表，
   免得同一个 kind 在两处写成两种说法。 */
const MEM_KIND_LABELS = { preference: "偏好", fact: "事实", entity: "实体", decision: "决定", insight: "洞见" };
const memKindLabel = (k) => MEM_KIND_LABELS[k] || k || "记忆";

let memItems = [];              // 上一次拉到的记忆（筛选纯前端做，不再请求后端）
let memUnusedOnly = false;      // 只看「从未用过」的那些

async function loadMemory() {
  const sel = $("mem_kind");
  const d = await (await fetch("/api/memory")).json();
  if (sel && !sel.options.length) {
    sel.innerHTML = (d.kinds || []).map((k) => `<option value="${k}">${memKindLabel(k)}</option>`).join("");
  }
  memItems = d.items || [];
  renderMemory();
}

/** 一条记忆的用量。use_count=0 就是**从来没被哪次提问真正用上过**
    （这时 last_used_at 是 0 —— 那表示「没发生过」，不是 1970 年，所以只说「从未用过」）。
    垫底塞进 prompt 但没命中的那些不算，所以在 prompt 里的记忆不一定都「用过」。 */
function memUsageHTML(m) {
  // 后端还没带用量字段时（旧版本）干脆不显示，别把「缺字段」读成「从未用过」
  if (m.use_count === undefined && m.last_used_at === undefined) return "";
  const n = Number(m.use_count) || 0;
  if (n <= 0) {
    return `<span class="memuse unused" title="这条还没被任何一次提问真正用上过">从未用过</span>`;
  }
  const ts = Number(m.last_used_at) || 0;
  const days = ts ? Math.floor((Date.now() / 1000 - ts) / 86400) : 0;
  const ago = !ts ? "" : (days <= 0 ? " · 今天用过" : ` · 最近 ${days} 天前`);
  return `<span class="memuse">用过 ${n} 次${ago}</span>`;
}

function memRowHTML(m) {
  return `<div class="memitem ${m.pinned ? "pinned" : ""}" data-id="${m.id}">
      <span class="memkind">${esc(m.kind)}</span>
      <span class="memtext">${esc(m.text)}${memUsageHTML(m)}</span>
      <span class="memacts">
        <button class="ttslink" onclick="memPin(${m.id}, ${m.pinned ? 0 : 1})">${m.pinned ? "取消置顶" : "置顶"}</button>
        <button class="ttslink" onclick="memDelete(${m.id})">删除</button>
      </span>
    </div>`;
}

/** 只看从未用过的那些（再点一次恢复全部）—— 纯前端过滤，不新增接口 */
function toggleMemUnused() {
  memUnusedOnly = !memUnusedOnly;
  renderMemory();
}

function renderMemory() {
  const box = $("memlist");
  const isUnused = (m) => !(Number(m.use_count) > 0);
  const total = memItems.length;
  const unused = memItems.filter(isUnused).length;
  $("mem_state").textContent = total ? `${total} 条` : "还没有记忆";
  if (!total) {
    box.innerHTML = `<div class="viewempty">还没有记忆。上面填一条试试 —— 跨集问答时它会被读进上下文。</div>`;
    return;
  }
  const shown = memUnusedOnly ? memItems.filter(isUnused) : memItems;
  box.innerHTML = `<div class="memsum">
      <span class="dimtext">共 ${total} 条，其中 ${unused} 条从未用过</span>
      <button class="ttslink" id="memfilter" onclick="toggleMemUnused()">${memUnusedOnly ? "显示全部" : "只看从未用过的"}</button>
    </div>`
    + (shown.length ? shown.map(memRowHTML).join("")
      : `<div class="viewempty">没有「从未用过」的记忆 —— 每一条都被某次提问带上过。</div>`);
}

async function memAdd() {
  const text = ($("mem_text").value || "").trim();
  if (text.length < 2) { toast("⚠ 内容太短"); return; }
  const resp = await fetch("/api/memory", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, kind: $("mem_kind").value || "fact", pinned: $("mem_pin").checked }),
  });
  const d = await resp.json();
  if (!resp.ok) { toast("⚠ " + esc(d.error || "保存失败")); return; }
  $("mem_text").value = ""; $("mem_pin").checked = false;
  toast("✦ 记住了");
  loadMemory();
}

async function memPin(id, pinned) {
  const resp = await fetch(`/api/memory/${id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pinned: !!pinned }),
  });
  // 失败必须说出来：紧接着会重新拉一次列表，成功和失败看起来一模一样，
  // 用户只会以为「点了没反应」，然后再点一遍。
  if (!resp.ok) {
    const d = await resp.json().catch(() => ({}));
    toast("⚠ " + esc(d.error || "没改动"));
  }
  loadMemory();
}

async function memDelete(id) {
  const resp = await fetch(`/api/memory/${id}`, { method: "DELETE" });
  if (!resp.ok) {
    const d = await resp.json().catch(() => ({}));
    toast("⚠ 删除失败：" + esc(String(d.error || resp.status)));
  } else {
    toast("已删除");
  }
  loadMemory();
}

/* ---- 界面上的几个「记住」入口 ----
   记忆只在用户**显式**点击时写入（程序不偷偷记）：阅读页选中一段 →「记住这条」，
   回答下面 →「记住这个结论」。两处都走 saveMemory，错误提示也就一处。 */

/** 存一条记忆；失败已经提示过了，返回 false 让调用方别改成「已记住」。 */
async function saveMemory(payload) {
  try {
    const resp = await fetch("/api/memory", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await resp.json().catch(() => ({}));
    if (!resp.ok) { toast("⚠ " + esc(d.error || "保存失败")); return false; }
    return true;
  } catch (e) {
    toast("⚠ " + esc(String((e && e.message) || e)));
    return false;
  }
}

/** 点过就换成「已记住」并禁用：既给反馈，也顺手挡住重复提交。
    dataset.mem 存的是**存过的原文**，重复点同一段/同一条时直接跳过。 */
function markRemembered(btn, text, label) {
  if (!btn) return;
  btn.textContent = label;
  btn.disabled = true;
  btn.dataset.mem = text;
  btn.classList.add("done");
}

/** 记忆块：「AI 记得的你」。空数组时返回空串 —— 不显示空标题，
   否则用户会以为 AI 什么都没记住（跟「这次没用上记忆」是两回事）。

    默认**收起**：它解释的是「这次回答为什么长这样」，属于要看时再看的东西；
    常显等于把内部机制摆在台面上（阅读助手的回答、问你的库共用这一块）。 */
function memNoteHTML(list) {
  const items = (list || []).map((t) => String(t || "").trim()).filter(Boolean);
  if (!items.length) return "";
  return `<details class="amem"><summary class="amemhead">AI 记得的你（${items.length} 条）</summary>`
    + `<ul class="amemlist">${items.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></details>`;
}

/** 「记住这个结论」：正文先挂在按钮上（渲染过的 HTML 里捞不回原文），
    点击时直接取。data-dir 显式写空串表示这条不属于任何一集（知识库问答）。 */
function wireAnswerBtn(box, text) {
  const btn = box ? box.querySelector(".aacts button, .kbaacts button") : null;
  if (!btn) return;
  const body = String(text || "").trim();
  if (!body) { btn.remove(); return; }
  btn.memText = body;
}

async function rememberAnswer(btn) {
  const text = String((btn && btn.memText) || "").trim();
  if (!text || (btn.dataset.mem || "") === text) return;      // 已记住过就不重复存
  const dir = btn.hasAttribute("data-dir") ? btn.getAttribute("data-dir")
                                           : (curWorkdir || assistDir || "");
  const ok = await saveMemory({ text, kind: "insight", dir, source: "answer" });
  if (!ok) return;
  markRemembered(btn, text, "★ 已记住");
  toast("✦ 记住了这条结论");
}

/** 阅读页抽屉里的「☆ 记住这条」：存的是抽屉里显示的那段选文 */
function resetRememberSel() {
  const btn = $("amembtn");
  if (!btn) return;
  btn.textContent = "☆ 记住这条";
  btn.disabled = false;
  delete btn.dataset.mem;
  btn.classList.remove("done");
}

async function rememberSelection(btn) {
  const el = btn || $("amembtn");
  const text = (assistSelection || currentArticleSelection() || lastSelection || "").trim();
  if (!text) {
    toast("⚠ 先在文章里选一段文字，再点「记住这条」");
    return;
  }
  if (el && (el.dataset.mem || "") === text) return;           // 同一段点第二次不再存
  const ok = await saveMemory({ text, kind: "insight", dir: curWorkdir || assistDir || "",
                                source: "reader" });
  if (!ok) return;
  markRemembered(el, text, "★ 已记住");
  toast("✦ 记住了这段");
}

/* ---------------- 朗读（把文章念出来）----------------
   后端在 podcast_article/tts.py：清洗正文 → 分块 → 逐块调 TTS → 落盘。
   这里负责界面：生成时轮询进度，生成完把音频接到播放条上。
   有 ffmpeg 时后端会拼成整篇（单个 <audio>，可以拖进度）；没有就按块连播。 */

let ttsState = null;      // 服务器返回的状态
let ttsPoll = null;       // 生成中的轮询
let ttsChunkIdx = 0;      // 无合并文件时，当前播到第几块
let ttsRate = 1;

/** 设置页里那一组字段 → 提交给后端的对象（音色取当前后端对应的那个输入框） */
function ttsFormValues() {
  const provider = ($("t_provider") || {}).value || "off";
  const isMac = provider === "macos";
  return {
    provider,
    base_url: (($("t_base") || {}).value || "").trim(),
    model: (($("t_model") || {}).value || "").trim(),
    voice: ((isMac ? ($("t_voice_mac") || {}).value : ($("t_voice") || {}).value) || "").trim(),
    speed: Number((($("t_speed") || {}).value) || 1),
    chunk_chars: parseInt((($("t_chunk") || {}).value) || "700", 10) || 700,
  };
}

/** 换了后端就显示对应的输入框（音色含义不同，不能混用） */
function onTtsProviderChange(quiet) {
  const provider = ($("t_provider") || {}).value || "off";
  const isMac = provider === "macos", isOpenai = provider === "openai";
  const show = (id, on) => { const el = $(id); if (el) el.style.display = on ? "" : "none"; };
  show("t_macos_box", isMac);
  show("t_openai_box", isOpenai);
  show("t_key_box", isOpenai);
  if (!quiet) markDirty();          // 载入设置时不算改动
}

/** 试听一句：先保存设置再生成（否则试的是旧配置） */
async function ttsPreview(btn) {
  const out = $("t_testres"), audio = $("t_preview");
  btn.disabled = true;
  out.className = "vres"; out.textContent = "生成中…";
  try {
    await saveSettings({ quiet: true });
    const d = await (await fetch("/api/tts/preview", { method: "POST" })).json();
    if (!d.ok) { out.className = "vres bad"; out.textContent = "✕ " + (d.error || "失败"); return; }
    out.className = "vres ok";
    out.textContent = `✓ ${d.provider === "macos" ? "macOS 本地" : d.provider}`
      + (d.voice ? ` · ${d.voice}` : "") + (d.seconds ? ` · ${d.seconds} 秒` : "");
    audio.style.display = "";
    audio.src = d.url;
    audio.play().catch(() => {});
  } catch (e) {
    out.className = "vres bad"; out.textContent = "✕ " + String(e);
  } finally { btn.disabled = false; }
}

/** 阅读页的朗读条 */
function toggleTtsBar() {
  const bar = $("ttsbar");
  if (bar.style.display !== "none") {
    bar.style.display = "none";
    ttsStopAudio();
    return;
  }
  bar.style.display = "";
  syncTts(true);
}

function ttsStopAudio() {
  const a = $("ttsaudio");
  a.pause();
  clearInterval(ttsPoll);
  ttsPoll = null;
}

function ttsRenderStatus() {
  const st = ttsState || {};
  const bar = $("ttsbar"), a = $("ttsaudio");
  const state = st.state || "idle";
  const label = {
    idle: "还没生成", running: "生成中", ready: "可以听了", error: "出错了",
  }[state] || state;

  if (state === "error") {
    $("ttsstate").innerHTML = `<span class="ttserr">✕ ${esc(st.error || "生成失败")}</span>`;
  } else if (state === "running") {
    const total = st.total || 0;
    $("ttsstate").textContent = total
      ? `生成中 ${st.done || 0}/${total} 块${st.note ? " · " + st.note : ""}`
      : (st.note || "准备中…");
  } else if (state === "ready") {
    if (st.fresh === false) {
      $("ttsstate").textContent = "文章改过了，音频是旧的 —— 点「重新生成」";
    } else {
      $("ttsstate").textContent = `${st.provider === "macos" ? "macOS 本地" : st.provider}`
        + (st.voice ? ` · ${st.voice}` : "") + (st.chars ? ` · ${st.chars} 字` : "");
    }
  } else {
    $("ttsstate").textContent = st.enabled === false
      ? "还没启用朗读：设置 → 朗读"
      : "还没生成，点上面的「🔊 朗读」开始";
  }

  const total = st.total || (st.chunks || []).length || 0;
  const nChunks = (st.chunks || []).length;
  $("ttscount").textContent = state === "ready"
    ? (st.merged ? (st.seconds ? `${Math.round(st.seconds / 60)} 分钟` : "整篇")
                 : `第 ${ttsChunkIdx + 1}/${nChunks} 段`)     // 分段模式：直接说清在第几段
    : (total ? `${total} 块` : "");

  // 进度条：生成中显示已完成比例；播放时由 timeupdate 更新
  if (state === "running" && total) {
    $("ttsprog").style.width = Math.round((st.done || 0) / total * 100) + "%";
  } else if (state !== "ready") {
    $("ttsprog").style.width = "0%";
  }

  // 接上音频源：整篇优先，否则按块
  const merged = st.merged;
  const chunks = st.chunks || [];
  if (state === "ready" && merged && a.dataset.src !== merged) {
    a.dataset.mode = "merged";
    a.dataset.src = merged;
    a.src = merged;
    ttsChunkIdx = 0;
  } else if (state === "ready" && !merged && chunks.length && a.dataset.mode !== "chunks") {
    a.dataset.mode = "chunks";
    a.dataset.src = "";
    ttsChunkIdx = 0;
    a.src = chunks[0].url;
  }
  $("ttsplay").textContent = a.paused ? "▶" : "❚❚";
  return bar;
}

async function syncTts(openBar) {
  if (!curWorkdir) return;
  const dir = encodeURIComponent(curWorkdir);
  try {
    const resp = await fetch(`/api/tts/${dir}/status`);
    if (!resp.ok) return;
    ttsState = await resp.json();
  } catch (e) { return; }
  if (openBar) $("ttsbar").style.display = "";
  ttsRenderStatus();
}

async function ttsStart(force) {
  if (!curWorkdir) return;
  if (ttsState && ttsState.enabled === false) {
    toast("⚠ 还没启用朗读：设置 → 朗读，选一个语音后端（macOS 本地免费）");
    return;
  }
  const dir = encodeURIComponent(curWorkdir);
  $("ttsbar").style.display = "";
  $("ttsstate").textContent = "提交中…";
  try {
    const resp = await fetch(`/api/tts/${dir}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: !!force }),
    });
    const d = await resp.json();
    if (!resp.ok) {
      $("ttsstate").innerHTML = `<span class="ttserr">✕ ${esc(d.error || "启动失败")}</span>`;
      return;
    }
    ttsState = d.tts || ttsState;
    ttsRenderStatus();
    clearInterval(ttsPoll);
    ttsPoll = setInterval(async () => {
      await syncTts();
      const st = ttsState || {};
      if (st.state !== "running") {
        clearInterval(ttsPoll); ttsPoll = null;
        if (st.state === "ready") toast("✦ 朗读生成好了，点播放条听吧");
      }
    }, 1200);
  } catch (e) {
    $("ttsstate").innerHTML = `<span class="ttserr">✕ ${esc(String(e))}</span>`;
  }
}

function ttsToggle() {
  const a = $("ttsaudio");
  if (!a.src) { ttsStart(false); return; }
  if (a.paused) a.play().catch(() => {}); else a.pause();
}

function ttsSetRate() {
  ttsRate = Number($("ttsspeed").value) || 1;
  $("ttsaudio").playbackRate = ttsRate;
}

async function ttsDelete() {
  if (!curWorkdir) return;
  const dir = encodeURIComponent(curWorkdir);
  const resp = await fetch(`/api/tts/${dir}`, { method: "DELETE" });
  if (!resp.ok) { toast("⚠ 删除失败"); return; }
  const a = $("ttsaudio");
  a.pause(); a.removeAttribute("src"); a.dataset.src = ""; a.dataset.mode = "";
  ttsState = null;
  $("ttsbar").style.display = "none";
  toast("✦ 已删除这篇的朗读音频（文章不受影响）");
}

/* 播放：整篇就是一个 <audio>；按块时播完自动接下一块 */
function ttsWireAudio() {
  const a = $("ttsaudio");
  if (a.dataset.wired) return;
  a.dataset.wired = "1";
  a.addEventListener("play", () => { $("ttsplay").textContent = "❚❚"; });
  a.addEventListener("pause", () => { $("ttsplay").textContent = "▶"; });
  a.addEventListener("timeupdate", () => {
    const st = ttsState || {};
    if (a.dataset.mode === "merged") {
      const pct = a.duration ? (a.currentTime / a.duration) * 100 : 0;
      $("ttsprog").style.width = pct.toFixed(1) + "%";
      $("ttscount").textContent = `${fmtClock(a.currentTime)} / ${fmtClock(a.duration || 0)}`;
    } else {
      const chunks = st.chunks || [];
      const total = chunks.length || 1;
      const pct = a.duration ? (a.currentTime / a.duration) : 0;
      $("ttsprog").style.width = (((ttsChunkIdx + pct) / total) * 100).toFixed(1) + "%";
      $("ttscount").textContent = `第 ${ttsChunkIdx + 1}/${total} 段`;
    }
  });
  a.addEventListener("ended", () => {
    const st = ttsState || {}, chunks = st.chunks || [];
    if (a.dataset.mode === "chunks" && ttsChunkIdx + 1 < chunks.length) {
      ttsChunkIdx += 1;
      a.src = chunks[ttsChunkIdx].url;
      a.playbackRate = ttsRate;
      a.play().catch(() => {});
    } else {
      $("ttsprog").style.width = "100%";
    }
  });
  // 点进度条：整篇可以任意跳；分块只能跳当前块内（跨块要等它自己播过去）
  $("ttsprogwrap").addEventListener("click", (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    if (!a.duration) return;
    if (a.dataset.mode === "merged") a.currentTime = ratio * a.duration;
    else a.currentTime = Math.min(a.duration, (ratio * (ttsState.total || 1) - ttsChunkIdx) * a.duration);
  });
  ttsSetRate();
}

/* ---------------- 只读镜像（公网部署）----------------
   服务器上那台只负责「看」：生成 / 转写 / AI 助手都在 Mac 上跑（算力与密钥都留在家里）。
   启动时问一次 /api/config，然后收起输入框、悬浮球与发布入口，并写清原因 ——
   比让人点一个必然 503 的按钮诚实。 */
let serverConfig = { readonly: false, hint: "", assistant: true, publish: true };

function applyServerConfig(cfg) {
  serverConfig = Object.assign({ readonly: false, hint: "", assistant: true, publish: true }, cfg || {});
  const ro = serverConfig.readonly;
  const note = serverConfig.hint || "这是一台只读镜像：请在 Mac 上生成文章。";
  const url = $("url"), go = $("go");
  if (url) {
    url.disabled = ro;
    url.placeholder = ro ? "只读镜像：请在 Mac 上提交链接"
                         : "粘贴播客或视频链接…（一次可粘多条；连标题说明一起粘也没关系）";
  }
  if (go) { go.disabled = ro; go.textContent = ro ? "只读镜像" : "生成文章"; }
  if (ro && $("hint")) $("hint").textContent = "📖 " + note;
  if ($("nbtn")) { $("nbtn").disabled = ro; $("nbtn").title = ro ? note : ""; }
  if (!serverConfig.assistant) {          // 这台机器没有密钥，助手入口直接不出现
    $("fab").classList.remove("show");
    $("selbtn").classList.remove("show");
  }
  syncFab();
  updateComposerHint();     // 按钮文案与意图提示跟着模式一起回正
  return serverConfig;
}

async function loadServerConfig() {
  try {
    return applyServerConfig(await (await fetch("/api/config")).json());
  } catch (e) {
    return applyServerConfig(null);       // 拿不到就当普通本机模式，别把界面弄瘸
  }
}

/* ---------------- 从粘贴的文本里抠链接 ----------------
   分享按钮复制出来的内容基本都带着说明文字：

     【赫拉利警示：AI正在悄然接管人类世界。】https://www.bilibili.com/video/BV1aphc6NEb6?vd_source=…

   所以不能假设「整行就是一个链接」：先把链接本身挑出来，再修掉粘在它末尾的标点。
   服务端有一份同样的逻辑（podcast_article/links.py），两边都用，粘哪种形式都能跑。 */
const URL_RE = /https?:\/\/[^\s]+/gi;
// 一行里连着写了好几个链接时的分隔符（`https://a.com/2，https://a.com/3`）。
// 只有分隔符后面紧跟 http(s):// 时才拆：链接里本来就可能有逗号（`?ids=1,2`）。
const GLUED_RE = /[\s,，、;；]+(?=https?:\/\/)/i;
const TAIL_JUNK = "。，、；：！？…,.;:!?\"'“”‘’";
// 成对的右括号：链接里没有对应左括号时，它才算「粘上来的噪音」（/wiki/Foo_(bar) 要留着）
const CLOSERS = { ")": "(", "）": "（", "]": "[", "】": "【", "》": "《", "」": "「", "』": "『" };

function cleanUrl(u) {
  let s = u || "";
  while (s) {
    const last = s.slice(-1);
    const opener = CLOSERS[last];
    if (opener) {
      if (s.split(opener).length >= s.split(last).length) break;
      s = s.slice(0, -1); continue;
    }
    if (TAIL_JUNK.includes(last)) { s = s.slice(0, -1); continue; }
    break;
  }
  return s;
}

/** 抠出一段文本里的链接（按出现顺序、去重）。也认「整行就是本机路径」那种输入 */
function urlsIn(text) {
  const t = String(text || "");
  const out = [];
  const push = (u) => { if (u && out.indexOf(u) < 0) out.push(u); };
  (t.match(URL_RE) || []).forEach((u) => u.split(GLUED_RE).forEach((piece) => push(cleanUrl(piece))));
  // 本机文件路径（转写本地音频）只在整行就是路径时才算，标题里的「AI/人类」不算
  t.split("\n").forEach((line) => {
    const s = line.trim();
    if (/^~?\/[^\s]+$/.test(s)) push(s);
  });
  return out;
}

/** 首页提交：一条直接跑，多条自动改成「加入队列」 */
async function startRun(opts = {}) {
  const raw = opts.url !== undefined ? opts.url : $("url").value.trim();
  if (!raw) { $("url").focus(); return; }
  const many = urlsIn(raw);
  if (!opts.url && many.length > 1) { await enqueueLinks(many); return; }
  // 只把链接本身发给后端：粘贴过来的「【标题】https://…」里那串说明文字会让解析失败。
  // 本机路径不走这一步（路径可能带空格，抠出来会断成两截）。
  const url = many.find((u) => /^https?:\/\//i.test(u)) || raw;
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

/* ---------------- 一个输入框，两种意图 ----------------
   粘链接 → 生成文章（startRun）；打字提问 → 问你的库（askLibrary）。
   判据就一条：输入里有没有链接（复用 urlsIn，跟「一次粘多条」用的是同一套识别）。 */

/** 意图：link（生成文章）/ ask（问你的库）/ empty。 */
function composerIntent() {
  const raw = $("url").value || "";
  if (urlsIn(raw).length) return "link";
  return raw.replace(/\s+/g, "").length >= 2 ? "ask" : "empty";
}

/** 输入框下面那行意图提示：#go 文案之外，明确告诉用户「这次会发生什么」 */
function setUrlHint(kind, n) {
  const el = $("urlhint");
  if (!el) return;
  if (kind === "empty") { el.style.display = "none"; el.innerHTML = ""; return; }
  el.style.display = "";
  el.innerHTML = kind === "link"
    ? `🔗 将生成文章${n > 1 ? `（${n} 条链接）` : ""}`
    : `💬 问你的库${kbScaleText()}`;
}

const COMPOSER_HINT = "⌘/Ctrl + Enter 直接开始（批量时一行一条链接）· 生成完会直接进阅读页，Esc 返回 · 产物会缓存，重新生成文章不必重新转写";

function updateComposerHint() {
  if (serverConfig.readonly) { $("go").textContent = "只读镜像"; $("go").disabled = true; setUrlHint("empty"); return; }
  const n = urlsIn($("url").value).length;
  const btn = $("go");
  if (n > 1) {
    btn.textContent = `加入队列 (${n})`;
    $("hint").innerHTML = `检测到 ${n} 条链接 —— 点按钮会全部排队，后台依次跑完；不想排队就只留一条。`;
    setUrlHint("link", n);
    return;
  }
  $("hint").innerHTML = COMPOSER_HINT;
  if (n === 1) { btn.textContent = "生成文章"; setUrlHint("link", 1); return; }
  // 没有链接：≥2 个字就当提问（「问你的库」），否则什么也不做
  const ask = ($("url").value || "").replace(/\s+/g, "").length >= 2;
  btn.textContent = ask ? "问我的库" : "生成文章";
  setUrlHint(ask ? "ask" : "empty");
  // 规模是异步补上的：先说「问你的库」，拿到状态后再把「（12 集 · 5612 条切片）」接上
  if (ask && !kbStatusCache) {
    kbStatus().then(() => { if (composerIntent() === "ask") setUrlHint("ask"); });
  }
}

/** #go 与 Enter 的唯一入口：按意图分流 */
function goSubmit() {
  if (serverConfig.readonly) return;
  const intent = composerIntent();
  if (intent === "link") { startRun(); return; }
  if (intent === "ask") { askLibrary(); return; }
  $("url").focus();
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
    $("transcript").classList.remove("show"); $("result").classList.remove("withTranscript");
    $("tbtn").textContent = "查看文字稿";
    $("nbtn").disabled = false;
    $("result").dataset.fromLib = "";
    openReader();                                  // 文章写好了直接进阅读页，不再挤在日志下面
    if (d.workdir) pushRoute(d.workdir);
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
    el.classList.remove("show"); $("result").classList.remove("withTranscript");
    $("tbtn").textContent = "查看文字稿"; return;
  }
  if (!el.dataset.loaded && curWorkdir) {
    const resp = await fetch(`/api/file/${encodeURIComponent(curWorkdir)}/transcript.txt`);
    el.innerHTML = renderTranscript(await resp.text());
    el.dataset.loaded = "1";
  }
  el.classList.add("show"); $("result").classList.add("withTranscript");
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
async function openSettings(tab) {
  closeAssist();                 // 抽屉会盖住设置页
  // 阅读页是固定整屏的一层，盖在主界面之上；设置页在 #main 里，不先收掉阅读页就会
  // 「点了设置什么都不发生」。顺手把地址栏带回主页面。
  if ($("result").classList.contains("show")) closeResult();
  $("main").style.display = "none";
  $("settings").style.display = "block";
  window.scrollTo({ top: 0 });
  // 注：这里以前被误粘进了 showView() 的尾巴（用了未定义的 name），
  // 于是 openSettings() 一定抛错、设置页的页签也切不了。按本函数原本的样子恢复。
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
  if (name === "memory") loadMemory();      // 切到记忆页就把列表拉出来（含用量）
}

const markDirty = () => $("savebar").classList.add("dirty");
const clearDirty = () => $("savebar").classList.remove("dirty");

async function loadSettings() {
  const d = await (await fetch("/api/settings")).json();
  const p = d.profile || {}, g = d.generation || {}, s = d.storage || {};
  $("s_name").value = p.name || "";
  $("s_interests").value = p.interests || "";
  $("s_plang").value = p.language || "zh";
  const t = d.tts || {};
  if ($("t_provider")) {
    $("t_provider").value = t.provider || "off";
    $("t_base").value = t.base_url || "";
    $("t_model").value = t.model || "";
    const isMac = (t.provider || "off") === "macos";
    // macOS 与 OpenAI 兼容两种后端的「音色」是两个概念（系统音色名 vs 接口音色名），
    // 所以用两个输入框，按后端显示其中一个，保存时取当前显示的那个
    $("t_voice").value = isMac ? "" : (t.voice || "");
    $("t_voice_mac").value = isMac ? (t.voice || "") : "";
    $("t_speed").value = t.speed || 1;
    $("t_speed_val").textContent = Number(t.speed || 1).toFixed(2) + "×";
    $("t_chunk").value = t.chunk_chars || 700;
    const chips = $("t_voice_chips");
    if (chips) {
      const voices = d.tts_voices || [];
      chips.innerHTML = voices.length
        ? voices.map((v) => `<button type="button" class="vchip" onclick="$('t_voice_mac').value='${esc(v)}';markDirty()">${esc(v)}</button>`).join("")
        : `<span class="fhint">这台机器上没有可用的中文音色（macOS 的 say 只在本机可用）</span>`;
    }
    onTtsProviderChange(true);
    $("t_state").textContent = ["off", ""].includes(t.provider) ? "未启用" : "已启用";
    $("t_state").className = "fstate" + (["off", ""].includes(t.provider) ? "" : " ok");
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
    ["设置文件", s.settings_path],
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
  $("as_mode").value = as.length_mode || "concise";
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
      length_mode: $("as_mode").value,
    },
    tts: ttsFormValues(),
  };
  const resp = await fetch("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  const d = await resp.json();
  if (!resp.ok) { toast("⚠ " + esc(d.error || "保存失败")); return; }
  await loadSettings();
  toast("✦ 已保存");
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

/** 时间戳统一渲染成 <span>。
    它本来就不是链接：用 <a> 会额外带来一次「导航」—— 带 href="#" 会跳到 #，不带 href 时
    jsdom 也照旧把它当成空相对地址去 follow。而阅读页把地址栏当路由（#/a/<目录名>），
    这种导航会被判成「离开了这一篇」，刚打开的文章整页收起来（UI 测试抓到过）。
    span 没有默认动作，点击交给文档上的委托监听（见 playFromTs）；键盘用 role/tabindex。
      dir:    点了跳哪一集（默认用当前打开的那篇）
      range:  这是时间区间（[00:06:36-00:06:49]），说明里写清跳的是起点
      from:   区间起点文本，用来写提示
      inline: 所在卡片整块可点（搜索结果）——要在自己身上挡住冒泡并自己播放，
              否则会顺带把文章打开。 */
function TS_HTML(sec, label, opts) {
  const o = opts || {};
  const dir = o.dir ? ` data-dir="${esc(o.dir)}"` : "";
  const act = o.inline ? ` onclick="event.stopPropagation();playFromTs(this)"` : "";
  const title = o.range ? `跳到音频此处（这一段的起点 ${esc(o.from || "")}）` : "跳到音频此处";
  return `<span class="ts" data-sec="${sec}"${dir} role="button" tabindex="0"` +
         ` title="${title}"${act}>${label}</span>`;
}

/* 时间戳的形态：单个 [00:06:36]、区间 [00:06:36-00:06:49]、以及模型偶尔漏写小时的 [09:24]。
   只认第一种的话，整篇都是区间的文章（引用跨了十几秒时模型就这么写）一个可点的链接都没有。
   区间按起点跳转；规则与 podcast_article/timestamps.py 一致。 */
const TS_RE = /[\[［【]\s*(\d{1,2}:\d{2}(?::\d{2})?)(?:\s*[-–—−~～至到]\s*(\d{1,2}:\d{2}(?::\d{2})?))?\s*[\]］】]/g;

/** `[时:分:秒]` / `[分:秒]` → 秒数 */
const tsToSec = (ts) => (ts || "").split(":").reduce((a, b) => a * 60 + Number(b || 0), 0);

/** 把已经转义过的 HTML 里的时间戳换成可点元素 */
function linkifyTs(html, opts) {
  const o = opts || {};
  return String(html).replace(TS_RE, (all, start, end) =>
    TS_HTML(tsToSec(start), all, { dir: o.dir, range: !!end, from: start }));
}

/** 文字稿里的时间戳也做成可点 */
function renderTranscript(text) {
  return linkifyTs(esc(text));
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

function closeResult(opts) {
  // keepRoute：由浏览器后退（地址栏已经变成 #/）触发时不要再推一条历史，否则会顶掉后退
  const keepRoute = !!(opts && opts.keepRoute);
  const el = $("result");
  const fromLib = el.dataset.fromLib === "1";
  el.classList.remove("show", "withTranscript");
  el.dataset.fromLib = "";
  document.body.classList.remove("reading");
  if (!keepRoute) pushRoute("");          // 地址栏回到主页面
  $("rdprogbar").style.transform = "scaleX(0)";
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
  if ($("ttsbar")) { ttsStopAudio(); $("ttsbar").style.display = "none"; }
  audioDir = null;
  pa().pause();
  pa().removeAttribute("src");
  syncFab();
  // 阅读页是整屏浮层，关掉后主页面还停在原来那一屏：从库里点开的就回到那张卡片，
  // 从生成流程进来的就回到输入框。
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
/** 窄屏（手机）：顶栏放不下「累计 1.28 元 · 640k tokens」这么长的字，
    只留金额，明细仍然在 title 里（点开设置 → 存储与用量也能看全） */
const isNarrow = () => window.innerWidth <= 600;

function renderUsagePill() {
  const el = $("usagepill");
  const t = libUsageTotal;
  if (!t || !t.episodes) { el.textContent = ""; el.title = ""; return; }
  el.textContent = isNarrow()
    ? `≈${fmtCost(t.cost_cny)}`
    : `累计 ${fmtCost(t.cost_cny)} · ${fmtTokens(t.total_tokens)} tokens`;
  el.title = `${t.episodes} 篇 · ${t.calls} 次模型调用\n` +
    `输入 ${t.input_tokens.toLocaleString()} tokens（缓存命中 ${t.hit_tokens.toLocaleString()}，命中率 ${t.cache_hit_rate}%）\n` +
    `输出 ${t.out_tokens.toLocaleString()} tokens\n点开看分模型明细`;
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
  // 视频类封面是 16:9，铺满即可；播客类封面是方形（节目 logo），
  // 用 1:1 的容器完整显示 —— 裁成 16:9 会把 logo 的上下切掉（小宇宙那张就是个圆环）。
  const squareCover = ["xiaoyuzhou", "rss", "file", ""].includes(it.source || "");
  const cover = it.has_cover
    ? `<div class="cover${squareCover ? " sq" : ""}">
         <img src="/api/cover/${encodeURIComponent(it.dir)}" alt="" loading="lazy">
       </div>`
    : "";
  return `
  <div class="ep${it.has_cover ? " hascover" : ""}" data-dir="${dir}" onclick="openEpisode('${encodeURIComponent(it.dir)}')">
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
    ${cover}
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

/* ---------------- 阅读页（独立整屏页面）----------------
   文章不再内嵌在主页面里跟着一起滚（原来顺序是：输入框 → 进度日志 → 文章 → 文章库），
   而是单独占满整屏的一层：
     · 打开时写进地址栏 #/a/<目录名> —— 刷新、收藏、前进后退都能回到同一篇
     · 「返回」（或 Esc、浏览器后退）回到文章库，主页面停在原来那一屏
   地址栏是唯一事实来源：它变了（hashchange）就按它把界面调成对应状态。
*/

const ROUTE_RE = /^#\/a\/(.+)$/;

/** 地址栏里指的是哪一篇（空字符串 = 主页面） */
function routeDir() {
  const m = ROUTE_RE.exec(location.hash || "");
  if (!m) return "";
  try { return decodeURIComponent(m[1]); } catch (e) { return ""; }
}

/** 把当前状态写进地址栏。
    这里用「给 location.hash 赋值」而不是 history.pushState：两者同样会留下一条历史记录
    （浏览器后退键照样能用），但 hash 赋值在任何环境下都真的把 URL 改掉、事件里读到的
    也是新地址；而 pushState 在 jsdom 里会异步补发一次 popstate，触发时 location.hash
    读到的是空的 —— UI 测试里会把刚打开的阅读页误关掉。赋值引发的 hashchange 由
    applyRoute 兜住，它是幂等的。 */
function pushRoute(dir) {
  const want = dir ? "#/a/" + encodeURIComponent(dir) : "#/";
  if (location.hash === want) return;
  location.hash = want;
}

/** 亮出阅读页这一层（内容由 showArticle 填好） */
function openReader() {
  const el = $("result");
  el.classList.add("show");
  document.body.classList.add("reading");   // 阅读页自己滚，底下的主页面别跟着动
  el.scrollTop = 0;
  updateReadProgress();
}

/** 顶栏底部那条阅读进度线 */
function updateReadProgress() {
  const el = $("result"), bar = $("rdprogbar");
  if (!el || !bar) return;
  const max = el.scrollHeight - el.clientHeight;
  const p = max > 8 ? el.scrollTop / max : 0;
  bar.style.transform = "scaleX(" + Math.min(1, Math.max(0, p)).toFixed(4) + ")";
}

/** 地址栏 → 界面：刷新、前进后退、手改 hash 都走这里。幂等，重复调用没有副作用。 */
async function applyRoute() {
  const dir = routeDir();
  const open = $("result").classList.contains("show");
  if (dir) {
    if (!open || curWorkdir !== dir) await showArticle(dir, { route: false });
  } else if (open) {
    closeResult({ keepRoute: true });   // 后退键关掉文章，地址栏已经是对的了，不要再推一条
  }
}

async function openEpisode(dirEnc) {
  if (window.__dragged) return;   // 刚才是拖拽，不要顺手打开文章
  await showArticle(decodeURIComponent(dirEnc), { fromLib: true });
}

/**
 * 打开一篇文章（内容 + 阅读页）。
 *   fromLib: 关掉之后回到文章库（false = 回到输入框，比如刚生成完）
 *   route:   是否把这次打开写进地址栏（从地址栏进来的那次不能再写，否则后退会失灵）
 */
async function showArticle(dir, opts) {
  const o = opts || {};
  const fromLib = o.fromLib !== false, route = o.route !== false;
  const dirEnc = encodeURIComponent(dir);
  const metaResp = await fetch(`/api/file/${dirEnc}/meta.json`);
  if (!metaResp.ok) {
    // 记录已经不在了（在别处删掉、或地址栏是个旧链接）：别把用户留在空白页上
    toast("⚠ 这篇的记录已经不在本地了");
    if ($("result").classList.contains("show")) closeResult({ keepRoute: true });
    if (route) pushRoute("");
    return;
  }
  const meta = await metaResp.json();
  curWorkdir = dir; curUrl = meta.url || null;
  $("result").dataset.fromLib = fromLib ? "1" : "";   // 关闭时回到历史库而不是回到输入框
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
    $("transcript").classList.remove("show"); $("result").classList.remove("withTranscript");
    $("tbtn").textContent = "查看文字稿";
    $("nbtn").disabled = false;
  }
  openReader();
  if (route) pushRoute(dir);
  // 打开即从「未读」推进到「在读」，并把这一集的累计花费显示出来
  const item = libItems.find((i) => i.dir === dir);
  curUsage = (item && item.usage) || null;
  renderCostPill();
  syncReadButton();
  syncReadDone();
  autoMarkReading(dir);
  // 换了一篇文章：助手里的上下文跟着换，并恢复这一篇上次的对话
  resetAssist();
  if (assistOpen) restoreAssistThread();
  syncFab();
  // 朗读：换文章就把播放条收起来并清掉上下文（避免上一篇的音频接着响）
  ttsStopAudio();
  if ($("ttsbar")) {
    $("ttsbar").style.display = "none";
    const a = $("ttsaudio");
    a.removeAttribute("src"); a.dataset.src = ""; a.dataset.mode = "";
    ttsWireAudio();
  }
  ttsChunkIdx = 0;
  syncTts(false);
}

// 注：输入框的 Enter / input 处理统一放在文件末尾的初始化段（见 autoGrow 的注释）
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !$("go").disabled) { e.preventDefault(); goSubmit(); }
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
// 时间戳是 <span role="button" tabindex="0">（不是链接），键盘回听得自己接
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const ts = e.target && e.target.closest ? e.target.closest(".ts") : null;
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

function searchCardHTML(it) {
  const hits = (it.hits || []).map((h) => {
    const field = h.field === "transcript" ? "文字稿" : h.field === "title" ? "标题" : "正文";
    const ts = h.ts
      // 这里必须内联处理：stopPropagation 会连 document 上的委托监听一起挡掉，
      // 所以不能只写 stopPropagation 把播放交给全局委托（那样点了不会出声）。
      ? TS_HTML(tsToSec(h.ts), `[${esc(h.ts)}]`, { dir: it.dir, inline: true })
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
    desc: "一行一条，从聊天记录里直接粘一串也可以 —— 带标题、序号、说明文字都没关系，只挑里面的链接。会按顺序依次生成。",
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
   形态是**会话**：一次提问之后可以一直追问，上下文接着上一轮。
   依据（原文片段 + 网络结果）默认收在每条回答下方，不占视野；需要核对时展开。
   时间戳依然可点回听 —— 那是这个功能相对通用聊天机器人的核心差别。 */

let assistOpen = false, assistES = null, assistAskId = null;
let assistSelection = "", assistDir = null;
let assistThread = "";            // 当前会话 id（同一段选文内持续追问）
let assistTurns = [];             // [{role:"user"|"assistant", content}]，发给后端做上下文
let assistEnabled = true;
let assistBusy = false;

function fabShouldShow() {
  // 有文章在读、设置里没关掉助手、且抽屉没开着时才出现。
  // 把「抽屉开着时不显示」放在这里而不是只靠 CSS：一处判断，测试也好断言
  // （CSS 那句 body.assistopen .fab 保留，作为双保险）。
  return $("result").classList.contains("show") && assistEnabled && !assistOpen
    && serverConfig.assistant !== false;
}

function syncFab() {
  $("fab").classList.toggle("show", fabShouldShow());
  document.body.classList.toggle("assistopen", assistOpen);
  document.body.classList.toggle("withplayer", $("player").classList.contains("show"));
}

function openAssist(preset) {
  const el = $("assist");
  if (el.classList.contains("show")) {
    if (preset && preset.selection && preset.selection !== assistSelection) {
      // 在抽屉已开的情况下选了**另一段**文字：按新选段开一段新对话
      startAssistThread(preset.selection, { keepOpen: true });
    }
    return;
  }
  assistOpen = true;
  assistDir = curWorkdir;
  el.classList.add("show");
  syncFab();
  if (preset && preset.selection !== undefined) setAssistSelection(preset.selection || "");
  restoreAssistThread();                 // 恢复上次的对话（同一集、同一段选文）
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
  else openAssist({ selection: currentArticleSelection() || lastSelection });
}

/** 当前对话针对的选文。传空串表示清掉（之后只按问题回答）。
    界面上它显示为会话的第一条用户消息，所以这里只管状态。 */
function setAssistSelection(text) {
  assistSelection = (text || "").trim();
  if (!assistSelection) lastSelection = "";
  resetRememberSel();            // 换了选文，「已记住」要退回可点状态
}

/** 开一段新对话：清空消息流与上下文 */
function startAssistThread(selection, opts = {}) {
  stopAssistStream();
  assistThread = "";
  assistTurns = [];
  assistSelection = (selection === undefined ? assistSelection : (selection || "")).trim();
  if (opts.keepOpen && !assistOpen) { /* 调用方负责打开 */ }
  setAssistSelection(assistSelection);
  $("amessages").innerHTML = "";
  $("aintro").style.display = "block";
  $("aq").value = "";
  $("agobtn").disabled = false;
  assistBusy = false;
}

function newAssistThread() {
  startAssistThread(currentArticleSelection() || "");
  $("aq").focus();
}

/** 界面重开时把上次那段对话恢复出来 */
async function restoreAssistThread() {
  if (!curWorkdir) return;
  try {
    const d = await (await fetch("/api/qa?dir=" + encodeURIComponent(curWorkdir))).json();
    const thread = d.last_thread || "";
    if (!thread) { startAssistThread(assistSelection); return; }
    const t = await (await fetch(`/api/qa?dir=${encodeURIComponent(curWorkdir)}&thread=${encodeURIComponent(thread)}`)).json();
    const turns = t.turns || [];
    if (!turns.length) { startAssistThread(assistSelection); return; }
    assistThread = thread;
    assistSelection = turns[0].selection || assistSelection;
    assistTurns = [];
    $("amessages").innerHTML = "";
    $("aintro").style.display = "none";
    for (const turn of turns) {
      if (turn.question) { appendUserBubble(turn.selection || "", turn.question); assistTurns.push({ role: "user", content: turn.question }); }
      appendAiBubble(turn.answer || "", { passages: turn.passages, web: turn.web, error: turn.error });
      assistTurns.push({ role: "assistant", content: turn.answer || "" });
    }
    scrollAssist();
  } catch (e) { startAssistThread(assistSelection); }
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

/* ---- 选中文字 → 浮出「深挖这段」 ---- */
let selBtnTimer = null;
let lastSelection = "";

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
  selBtnTimer = setTimeout(positionSelBtn, 130);
}

/** 点浮出按钮：把选中的文字带进抽屉并按新选段开一段对话 */
function askSelection() {
  const text = lastSelection || currentArticleSelection();
  $("selbtn").classList.remove("show");
  if (!text) return;
  if (!assistOpen) { assistOpen = true; assistDir = curWorkdir; $("assist").classList.add("show"); syncFab(); }
  startAssistThread(text);
  askAI();
}

/* ---- 消息气泡 ---- */
function scrollAssist() {
  const body = $("abody");
  body.scrollTop = body.scrollHeight;
}

function appendUserBubble(selection, question) {
  const wrap = doc_el("div", "amsg user");
  if (selection) {
    const q = doc_el("div", "aqtext");
    q.textContent = selection;
    wrap.appendChild(q);
  }
  if (question) {
    const q = doc_el("div", "aq");
    q.textContent = question;
    wrap.appendChild(q);
  }
  $("amessages").appendChild(wrap);
  $("aintro").style.display = "none";
  scrollAssist();
  return wrap;
}

/* 出处标签：模型会在句末标「（原文未提及）」「（据网络资料）」。
   用户要求**不要在正文里看到它们**（和搜索数据一样收起来），所以：
   渲染前剥掉，计数放进折叠的「依据」那一行。 */
const TAG_RE = /[（(](原文未提及|据网络资料)[）)]/g;

function splitTags(md) {
  const counts = { untagged: 0, web: 0 };
  const text = String(md || "").replace(TAG_RE, (_m, kind) => {
    if (kind === "据网络资料") counts.web += 1; else counts.untagged += 1;
    return "";
  })
    // 标签多在句末，剥掉后可能留下多余空格或空格贴标点
    .replace(/[ \t]+([。，、；：！？])/g, "$1")
    .replace(/[ \t]{2,}/g, " ");
  return { text, counts };
}

function appendAiBubble(md, sources) {
  const wrap = doc_el("div", "amsg ai");
  const body = doc_el("div", "aanswer");
  const split = splitTags(md);
  body.dataset.untagged = split.counts.untagged;
  body.dataset.webtag = split.counts.web;
  body.innerHTML = mdLite(split.text);
  wrap.appendChild(body);
  $("amessages").appendChild(wrap);
  $("aintro").style.display = "none";
  if (sources) attachSources(wrap, sources);
  attachAnswerActions(wrap, md);        // 恢复历史对话时也要能「记住这个结论」
  scrollAssist();
  return wrap;
}

/** 依据默认收起来：一行摘要 + <details>，要核对时再展开 */
function attachSources(bubble, src) {
  const old = bubble.querySelector(".asrc");
  if (old) old.remove();
  // 「AI 记得的你」单独一块、默认收起：它不在「依据」里面（要看时各看各的）
  attachMemoryNote(bubble, src && src.memory);
  const passages = (src && src.passages) || [];
  const web = src && src.web;
  const bits = [];
  if (passages.length) bits.push(`${passages.length} 段原文`);
  if (web && web.ok && (web.results || []).length) bits.push(`${web.results.length} 条网络结果`);
  // 正文里那些出处标签被剥掉了，计数挪到这里 —— 正文保持干净，核对时再展开
  const answerEl = bubble.querySelector(".aanswer") || { dataset: {} };
  const untagged = Number(answerEl.dataset.untagged || 0);
  const webTag = Number(answerEl.dataset.webtag || 0);
  if (untagged + webTag > 0) {
    const detail = [];
    if (untagged) detail.push(`原文未提及 ${untagged}`);
    if (webTag) detail.push(`据网络资料 ${webTag}`);
    // 措辞刻意写成「模型标注」：这是模型的自我标注、不是穷尽核对，
    // 所以「没显示」不能被读成「全都来自本集原文」
    bits.push(`模型标注非原文内容 ${untagged + webTag} 处（${detail.join(" · ")}）`);
  }
  if (!bits.length) return;

  const box = doc_el("details", "asrc");
  const sum = doc_el("summary", "");
  sum.textContent = `依据：${bits.join(" · ")}`;
  box.appendChild(sum);

  if (passages.length) {
    const list = doc_el("div", "apassages");
    list.innerHTML = passages.map((p) => `
      <div class="apass">
        ${p.ts ? TS_HTML(tsToSec(p.ts), `[${esc(p.ts)}]`, { dir: assistDir || curWorkdir || "" }) : ""}
        <span class="aptext">${esc(p.text || "")}</span>
      </div>`).join("");
    box.appendChild(list);
  } else {
    const none = doc_el("div", "anoweb");
    none.textContent = "这一集的文字稿里没有和这段明显相关的段落。";
    box.appendChild(none);
  }

  if (web) {
    if (web.ok && (web.results || []).length) {
      const wl = doc_el("div", "awebwrap");
      wl.innerHTML = web.results.map((r) => `
        <a class="aweb" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">
          <span class="awt">${esc(r.title || r.url)}</span>
          <span class="awu">${esc(r.url)}</span>
        </a>`).join("");
      box.appendChild(wl);
    } else {
      const none = doc_el("div", "anoweb");
      none.textContent = `这次没联网成功（${web.error || "原因未知"}）—— 解读只基于播客原文。`;
      box.appendChild(none);
    }
  }
  bubble.appendChild(box);
}

function doc_el(tag, cls) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  return el;
}

/** 「AI 记得的你」：这次回答参考了他自己存下的哪几条（sources.memory）。
    空数组就不渲染这一块，也不留空标题；非空时是一个默认收起的折叠块。 */
function attachMemoryNote(bubble, list) {
  const old = bubble.querySelector(".amem");
  if (old) old.remove();
  const html = memNoteHTML(list);
  if (!html) return;
  bubble.insertAdjacentHTML("beforeend", html);
}

/** 回答下面那行动作：「☆ 记住这个结论」。流式过程中不挂（正文还没成形）。 */
function attachAnswerActions(bubble, answer) {
  const old = bubble.querySelector(".aacts");
  if (old) old.remove();
  const body = answerPlain(answer);
  if (!body) return;
  const row = doc_el("div", "aacts");
  row.innerHTML = `<button class="ttslink" onclick="rememberAnswer(this)"
      title="把这条结论存进记忆库，以后提问会自动带上">☆ 记住这个结论</button>`;
  bubble.appendChild(row);                 // 每次都追加到末尾，确保在依据下面
  row.querySelector("button").memText = body;
}

/** 回答正文里能被存下来的那份文本：和界面显示的一致（剥掉模型自我标注的出处标签） */
const answerPlain = (md) => splitTags(md || "").text.trim();

/* ---- 提问 ---- */
async function askAI() {
  if (!curWorkdir) { toast("⚠ 先打开一篇文章"); return; }
  if (assistBusy) return;
  const question = ($("aq").value || "").trim();
  const selection = assistSelection || currentArticleSelection();
  if (!selection && !question) {
    toast("⚠ 先在文章里选一段文字，或者写一个问题");
    $("aq").focus();
    return;
  }
  assistDir = curWorkdir;
  assistSelection = selection;
  appendUserBubble(assistTurns.length ? "" : selection, question || "（就这段展开讲讲）");
  $("aq").value = "";
  $("aq").style.height = "auto";
  assistBusy = true;
  $("agobtn").disabled = true;

  // 等第一条 delta 之前先给一个「正在输入」的气泡，替代原来的阶段文字
  const bubble = appendAiBubble("", null);
  bubble.querySelector(".aanswer").classList.add("streaming", "pending");
  assistStreamBubble = bubble;

  const payload = {
    dir: curWorkdir, selection, question,
    web: $("aweb_toggle").checked,
    mode: $("adetail").checked ? "detail" : "concise",
    thread: assistThread,
    history: assistTurns.slice(-12),
  };
  try {
    const resp = await fetch("/api/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await resp.json();
    if (!resp.ok) throw new Error(d.error || "提问失败");
    assistAskId = d.id;
    connectAssistStream(d.id, bubble, question, selection);
  } catch (e) {
    bubble.querySelector(".aanswer").classList.remove("streaming", "pending");
    bubble.querySelector(".aanswer").innerHTML = `<p class="aerr">✕ ${esc(String(e.message || e))}</p>`;
    assistBusy = false; $("agobtn").disabled = false;
  }
}

let assistStreamBubble = null;

function connectAssistStream(id, bubble, question, selection) {
  stopAssistStream();
  const view = bubble.querySelector(".aanswer");
  let text = "";
  let sources = null;
  const seen = (t) => {
    text += t;
    view.classList.remove("pending");
    const split = splitTags(text);
    view.dataset.untagged = split.counts.untagged;
    view.dataset.webtag = split.counts.web;
    view.innerHTML = mdLite(split.text);
    scrollAssist();
  };
  const finish = (d) => {
    assistBusy = false;
    $("agobtn").disabled = false;
    view.classList.remove("streaming", "pending");
    const answer = text || d.answer || "";
    const split = splitTags(answer);
    view.dataset.untagged = split.counts.untagged;
    view.dataset.webtag = split.counts.web;
    view.innerHTML = mdLite(split.text);
    if (!answer && d.error) view.innerHTML = `<p class="aerr">✕ ${esc(d.error)}</p>`;
    if (d.sources) sources = d.sources;
    if (sources) attachSources(bubble, sources);
    attachAnswerActions(bubble, answer);      // 回答成形后才挂「记住这个结论」
    if (d.thread) assistThread = d.thread;          // 首轮由后端生成，之后一直沿用
    if (answer) {
      assistTurns.push({ role: "user", content: question || "（就这段展开讲讲）" });
      assistTurns.push({ role: "assistant", content: answer });
    }
    scrollAssist();
  };

  if (typeof EventSource !== "undefined") {
    assistES = new EventSource("/api/ask/" + encodeURIComponent(id) + "/stream");
    assistES.addEventListener("sources", (e) => { sources = JSON.parse(e.data); });
    assistES.addEventListener("delta", (e) => seen(JSON.parse(e.data).text || ""));
    assistES.addEventListener("done", (e) => { stopAssistStream(); finish(JSON.parse(e.data)); });
    assistES.addEventListener("error", (e) => {
      if (e && e.data) { stopAssistStream(); finish(Object.assign({ error: "提问失败" }, JSON.parse(e.data))); }
    });
    assistES.onerror = () => { if (assistES) { stopAssistStream(); pollAssist(id, text, finish, seen); } };
  } else {
    pollAssist(id, text, finish, seen);
  }
}

/** SSE 不可用时的兜底：轮询同一个提问任务，增量靠已渲染文本的长度推算 */
function pollAssist(id, text, finish, onDelta) {
  let shown = text.length;
  const timer = setInterval(async () => {
    try {
      const d = await (await fetch("/api/ask/" + encodeURIComponent(id))).json();
      const full = d.answer || "";
      if (full.length > shown) { onDelta(full.slice(shown)); shown = full.length; }
      if (d.status !== "running") { clearInterval(timer); finish(d); }
    } catch (e) {
      clearInterval(timer);
      finish({ answer: text, error: String(e) });
    }
  }, 900);
}

function stopAssistStream() {
  if (assistES) { assistES.close(); assistES = null; }
}

/** 极简 markdown → HTML（会话里够用：段落 / 粗体 / 行内码 / 引用 / 列表 / 链接 / 时间戳） */
function mdLite(md) {
  if (!md) return "";
  const inline = (s) => linkifyTs(
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'),
    { dir: assistDir || curWorkdir || "" });
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

/** 换文章时把助手里上一集的痕迹清掉（否则会拿旧片段回答新文章的问题） */
function resetAssist() {
  stopAssistStream();
  assistAskId = null;
  startAssistThread("");
  assistTurns = [];
  assistThread = "";
  assistBusy = false;
  $("agobtn").disabled = false;
  resetRememberSel();
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

/* ---------------- 账号会话 ---------------- */
let currentAccount = null;
let redirectingAfterAuthFailure = false;

function clearLocalAppState() {
  try { localStorage.clear(); } catch (_) {}
  try { sessionStorage.clear(); } catch (_) {}
  currentAccount = null;
  libItems = []; libCats = []; libAssign = {}; libStatus = {};
  mcpServers = []; mcpTools = {}; mcpPresets = [];
  const grid = $("libgrid"); if (grid) grid.replaceChildren();
  const results = $("results"); if (results) results.replaceChildren();
}

function expireSession() {
  if (redirectingAfterAuthFailure) return;
  redirectingAfterAuthFailure = true;
  clearLocalAppState();
  location.assign("/login");
}

function fmtQuotaDuration(seconds) {
  if (seconds == null) return "不限";
  const value = Number(seconds) || 0;
  const hours = Math.floor(value / 3600), minutes = Math.round((value % 3600) / 60);
  return hours ? `${hours} 小时${minutes ? ` ${minutes} 分` : ""}` : `${minutes} 分钟`;
}

function renderAccount(account) {
  currentAccount = account;
  const trigger = $("account-menu");
  trigger.hidden = false;
  $("account-name").textContent = account.username;
  $("account-panel-name").textContent = account.username;
  $("account-role").textContent = account.role === "admin" ? "管理员账号" : "个人账号";
  const quota = account.quota || {}, asr = quota.asr || {}, llm = quota.llm || {};
  const llmLimit = llm.limit_cny == null ? "不限" : `¥${Number(llm.limit_cny).toFixed(2)}`;
  $("account-quota").innerHTML = `<div><span>本月转写</span><strong>${fmtQuotaDuration(asr.used_seconds)} / ${fmtQuotaDuration(asr.limit_seconds)}</strong></div><div><span>本月 AI 写作</span><strong>¥${Number(llm.used_cny || 0).toFixed(2)} / ${llmLimit}</strong></div>`;
  const admin = account.role === "admin";
  [document.querySelector('[data-tab="keys"]'), document.querySelector('[data-tab="mcp"]'), $("pane-keys"), $("pane-mcp")]
    .filter(Boolean).forEach((node) => { node.hidden = !admin; });
  document.querySelectorAll('[data-tab="keys"], [data-tab="mcp"]').forEach((node) => { node.style.display = admin ? "" : "none"; });
}

async function csrfPost(path, payload) {
  const csrfResponse = await fetch("/api/auth/csrf", { credentials: "same-origin" });
  const csrf = await csrfResponse.json();
  return fetch(path, { method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf.csrf_token || "" },
    body: JSON.stringify(payload || {}) });
}

function initAccountControls() {
  const trigger = $("account-menu");
  trigger.addEventListener("click", () => {
    const panel = $("account-panel"); panel.hidden = !panel.hidden;
    trigger.setAttribute("aria-expanded", String(!panel.hidden));
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".account-wrap")) {
      $("account-panel").hidden = true; trigger.setAttribute("aria-expanded", "false");
    }
  });
  $("account-logout").addEventListener("click", async () => {
    try { await csrfPost("/api/auth/logout"); } finally { expireSession(); }
  });
  $("account-password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget, error = $("account-password-error"), button = form.querySelector("button[type=submit]");
    error.textContent = ""; button.disabled = true;
    try {
      const response = await csrfPost("/api/auth/password", {
        current_password: $("account-current-password").value,
        new_password: $("account-new-password").value,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || "无法更改密码，请检查当前密码和新密码长度。");
      form.reset(); $("account-panel").hidden = true; trigger.setAttribute("aria-expanded", "false"); toast("密码已更新");
    } catch (failure) { error.textContent = failure.message || "无法更改密码，请重试。"; }
    finally { button.disabled = false; }
  });
}

async function initializeAuthenticatedApp() {
  let response;
  try { response = await fetch("/api/auth/me", { credentials: "same-origin" }); }
  catch (_) { return expireSession(); }
  if (!response.ok) return expireSession();
  const identity = await response.json();
  if (!identity.authenticated) return expireSession();
  renderAccount(identity);
  initAccountControls();

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const result = await nativeFetch(...args);
    const requestUrl = String(args[0]?.url || args[0] || "");
    if (result.status === 401 && requestUrl.includes("/api/") && !requestUrl.includes("/api/auth/me")) expireSession();
    return result;
  };

  loadServerConfig();
  loadLibrary().then(() => { if (routeDir()) applyRoute(); });
  if (identity.role === "admin") loadMcp();
  loadSettings(); loadQueue(); loadFeeds(); pollCurrentJob();
  updateComposerHint(); autoGrow(); syncFab();
  $("result").addEventListener("scroll", updateReadProgress, { passive: true });
  window.addEventListener("resize", updateReadProgress);
  window.addEventListener("resize", renderUsagePill);
  window.addEventListener("hashchange", applyRoute); window.addEventListener("popstate", applyRoute);
  $("aq").addEventListener("input", () => {
    const el = $("aq"); el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 110) + "px";
  });
  $("aq").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); askAI(); }
  });
  setInterval(() => { if (activeView === "queue") loadQueue(); }, 5000);
  setInterval(() => loadLibrary(), 30000);
}

/* ---------------- 初始化 ---------------- */
// 首页输入框是 textarea：<input type="text"> 按规范会**丢掉换行**，
// 一次粘多条链接会被粘成一条（实测被 UI 测试抓到），所以必须用多行控件。
$("url").addEventListener("input", () => { updateComposerHint(); autoGrow(); });
$("url").addEventListener("keydown", (e) => {
  // Enter 和按钮走同一条路：有链接就生成文章，是问题就问你的库（Shift+Enter 才换行）
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); goSubmit(); }
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

initializeAuthenticatedApp();
