/**
 * 「知识库 + 记忆」在界面上的四个入口。
 *
 * 后端接口早就做好了，这里测的是**接线**：
 *   1) 阅读页抽屉里的「☆ 记住这条」→ POST /api/memory（选中的那段文字）
 *   2) 回答下面的「☆ 记住这个结论」+ sources.memory 非空时的「AI 记得的你」
 *   3) 知识库问答：memory_used 非空时的「AI 记得的你」+「记住这个结论」
 *   4) 知识库检索：memory 非空时结果列表上方的「你记过的（N 条）」
 *
 * 两条硬约束：
 *   · **绝不能真的写记忆**（这是用户真实的记忆库），所以 /api/memory 一律打桩，
 *     顺便断言发出去的 url / method / body（text、kind、dir、source 一个都不能错）。
 *   · **绝不能真的问模型**，所以 /api/ask 与 /api/kb/ask 都返回假 id，
 *     EventSource 换成可手动驱动的假实现，由测试决定什么时候推 sources / delta / done。
 */
const { boot, until, report, sleep } = require("./harness");

const DIR = "__UI测试单集";                     // seed.js 造的那一集
const MEM1 = "用户偏好：开篇用具体场景切入";
const MEM2 = "他在做强化学习基础设施";
const ANSWER = "结论：这个选择是被逼出来的。（原文未提及）";   // 存下来时出处标签要被剥掉
const ANSWER_PLAIN = "结论：这个选择是被逼出来的。";
const KB_ANSWER = "书里提到的做法有两种：先定场景，再补背景。";

(async () => {
  const out = {}, fails = [];
  const check = (name, cond, msg) => { out[name] = cond; if (!cond) fails.push(msg); };

  const memPosts = [];            // 前端发起的记忆写入请求
  const asks = [];                // 前端发起的提问请求
  const store = { failMem: false, failError: "内容太短", search: null, kbAsk: null, memList: null };

  const { window, doc, $, click } = await boot({
    beforeParse(w) {
      const realFetch = w.fetch;
      const fakeResp = (obj, ok = true) => Promise.resolve({
        ok, status: ok ? 200 : 400,
        headers: { get: () => "application/json" },
        json: async () => obj,
        text: async () => JSON.stringify(obj),
      });

      w.fetch = (u, o) => {
        const url = String(u);
        const method = String((o && o.method) || "GET").toUpperCase();
        const body = o && o.body ? JSON.parse(o.body) : null;
        if (url.includes("/api/memory")) {
          if (method === "POST") {
            memPosts.push({ url, method, body });
            if (store.failMem) return fakeResp({ error: store.failError }, false);
            return fakeResp(Object.assign({ id: memPosts.length, pinned: false }, body));
          }
          // 记忆列表 / 置顶 / 删除：默认给空，别碰真实记忆库（用量那组用例会塞 store.memList）
          return fakeResp(store.memList
            || { items: [], kinds: ["preference", "fact", "entity", "decision", "insight"] });
        }
        if (url.includes("/api/kb/ask")) return fakeResp(store.kbAsk || { answer: "", sources: [] });
        if (url.includes("/api/kb/search")) return fakeResp(store.search || { hits: [], memory: [], mode: "lexical" });
        if (url.includes("/api/ask")) {
          asks.push({ url, method, body });
          return fakeResp({ id: "fakeask1", web: false });
        }
        return realFetch(u, o);
      };

      // 可手动驱动的 EventSource（阅读助手的 SSE）
      w.__streams = [];
      w.EventSource = class {
        constructor(url) {
          this.url = url; this.closed = false; this.ls = {};
          w.__streams.push(this);
        }
        addEventListener(t, fn) { (this.ls[t] = this.ls[t] || []).push(fn); }
        close() { this.closed = true; }
        emit(t, data) { (this.ls[t] || []).forEach((fn) => fn({ data: JSON.stringify(data) })); }
      };

      // jsdom 没有真实布局，range 的 rect 全 0，浮出按钮会以为没选区
      w.Range.prototype.getBoundingClientRect = () => ({
        width: 120, height: 16, top: 220, left: 120, right: 240, bottom: 236,
      });
    },
  });

  const lastStream = () => window.__streams[window.__streams.length - 1];
  const aiBubbles = () => [...doc.querySelectorAll("#amessages .amsg.ai")];
  const lastBubble = () => aiBubbles()[aiBubbles().length - 1];

  // ---------- 1) 阅读页：选中一段 → 抽屉 →「☆ 记住这条」
  await window.openEpisode(encodeURIComponent(DIR));
  await until(() => $("result").classList.contains("show"), 8000);
  await sleep(900);

  const para = [...doc.querySelectorAll("#article p")].find((p) => p.textContent.trim().length > 10);
  if (!para) { console.log("文章里没有可用段落"); process.exit(1); }
  const paraText = para.textContent.trim();
  const range = doc.createRange();
  range.selectNodeContents(para);
  const sel = window.getSelection();
  sel.removeAllRanges(); sel.addRange(range);
  doc.dispatchEvent(new window.MouseEvent("mouseup", { bubbles: true }));
  await sleep(400);
  click($("selbtn"));                        // 「深挖这段」→ 开抽屉、把选文带进去
  await until(() => $("assist").classList.contains("show"), 5000);
  await sleep(600);

  out.抽屉_有记住这条按钮 = !!$("amembtn");
  out.抽屉_按钮文案 = ($("amembtn") || { textContent: "" }).textContent.trim();
  check("抽屉_有记住这条按钮", out.抽屉_有记住这条按钮 && /记住这条/.test(out.抽屉_按钮文案),
    "阅读助手抽屉里应有「☆ 记住这条」按钮");

  // ---------- (a) 请求体：text / kind / dir / source 必须是接口约定的那套
  memPosts.length = 0;
  click($("amembtn"));
  await until(() => memPosts.length > 0, 4000);
  await sleep(150);
  const p = memPosts[0] || {};
  const b = p.body || {};
  out.a_请求 = { url: p.url, method: p.method, body: b };
  check("a_记住这条_请求", p.url === "/api/memory" && p.method === "POST",
    `「记住这条」应 POST /api/memory，实际 ${p.method} ${p.url}`);
  check("a_记住这条_text是选中的那段", b.text === paraText,
    `存的应是选中的那段文字，实际「${String(b.text).slice(0, 40)}」`);
  check("a_记住这条_kind", b.kind === "insight", `kind 应为 insight，实际 ${b.kind}`);
  check("a_记住这条_dir是本集", b.dir === DIR, `dir 应为本集 ${DIR}，实际 ${b.dir}`);
  check("a_记住这条_source", b.source === "reader", `source 应为 reader，实际 ${b.source}`);
  check("a_记住这条_有反馈", /记住/.test($("toast").textContent), "存完应给一句轻量反馈（toast）");

  // ---------- (d) 点过变「已记住」，重复点不再 POST
  const memBtn = $("amembtn");
  out.d_记住这条_已记住文案 = memBtn.textContent.trim();
  out.d_记住这条_已禁用 = memBtn.disabled;
  check("d_记住这条_变成已记住", /已记住/.test(out.d_记住这条_已记住文案) && out.d_记住这条_已禁用,
    `点过后应变成已记住并禁用，实际「${out.d_记住这条_已记住文案}」disabled=${memBtn.disabled}`);
  const beforeDup = memPosts.length;
  click(memBtn); click(memBtn);
  await sleep(300);
  check("d_记住这条_不重复POST", memPosts.length === beforeDup,
    `重复点「已记住」不该再发 POST（${beforeDup} → ${memPosts.length}）`);

  // ---------- (b) 回答下方：sources.memory →「AI 记得的你」+「记住这个结论」
  const stream = lastStream();
  const sourcesWithMem = {
    passages: [{ ts: "00:10:07", start: 607, text: "这是原文里的第一段。" }],
    web: { ok: false, provider: "", results: [], error: "没有相关结果" },
    memory: [MEM1, MEM2],
  };
  stream.emit("sources", sourcesWithMem);
  await sleep(200);
  check("b_流式中_不铺记忆块", doc.querySelectorAll("#amessages .amem").length === 0,
    "记忆块不该在流式过程中就出现（跟依据一样，等回答结束）");
  stream.emit("delta", { text: ANSWER });
  await sleep(150);
  stream.emit("done", { status: "done", answer: ANSWER, error: "", sources: sourcesWithMem, thread: "t1" });
  await until(() => doc.querySelector("#amessages .amem"), 4000);
  await sleep(150);

  const bubble = lastBubble();
  const memBox = bubble.querySelector(".amem");
  out.b_记忆块存在 = !!memBox;
  out.b_记忆块标题 = memBox ? memBox.querySelector(".amemhead").textContent.trim() : "(没有)";
  out.b_记忆块条目 = memBox ? [...memBox.querySelectorAll("li")].map((li) => li.textContent.trim()) : [];
  out.b_记忆块不在折叠区 = !!memBox && !memBox.closest("details.asrc");
  check("b_记忆块存在", out.b_记忆块存在, "sources.memory 非空时回答下面应出现「AI 记得的你」");
  check("b_记忆块标题", /AI 记得的你/.test(out.b_记忆块标题), `标题文案不对：「${out.b_记忆块标题}」`);
  check("b_记忆块条目", out.b_记忆块条目.join("|") === [MEM1, MEM2].join("|"),
    `应逐条列出这几条记忆，实际 ${JSON.stringify(out.b_记忆块条目)}`);
  check("b_记忆块可见", out.b_记忆块不在折叠区, "记忆块要一眼看见，不能塞进折叠的「依据」里");

  // 「☆ 记住这个结论」：存回答正文（出处标签剥掉）、带 dir、source=answer
  const ansBtn = bubble.querySelector(".aacts button");
  out.b_结论按钮文案 = ansBtn ? ansBtn.textContent.trim() : "(没有)";
  check("b_有记住这个结论按钮", !!ansBtn && /记住这个结论/.test(out.b_结论按钮文案),
    `回答下面应有「☆ 记住这个结论」，实际「${out.b_结论按钮文案}」`);
  memPosts.length = 0;
  click(ansBtn);
  await until(() => memPosts.length > 0, 4000);
  await sleep(150);
  const ab = (memPosts[0] || {}).body || {};
  out.b_结论请求 = { url: (memPosts[0] || {}).url, method: (memPosts[0] || {}).method, body: ab };
  check("b_结论_text是回答正文", ab.text === ANSWER_PLAIN,
    `应存回答正文（正文里看不到的出处标签要剥掉），实际「${ab.text}」`);
  check("b_结论_kind_dir_source", ab.kind === "insight" && ab.dir === DIR && ab.source === "answer",
    `kind/dir/source 不对：${ab.kind} / ${ab.dir} / ${ab.source}`);
  check("b_结论_已记住", /已记住/.test(ansBtn.textContent) && ansBtn.disabled,
    `点过后应变成已记住，实际「${ansBtn.textContent}」`);
  const beforeAns = memPosts.length;
  click(ansBtn); click(ansBtn);
  await sleep(250);
  check("d_结论_不重复POST", memPosts.length === beforeAns,
    `重复点「已记住」不该再发 POST（${beforeAns} → ${memPosts.length}）`);

  // ---------- (b2) sources.memory 为空 → 这一块**不该**出现
  window.newAssistThread();
  await sleep(200);
  $("aq").value = "再问一个";
  const beforeStreams = window.__streams.length;
  click($("agobtn"));
  await until(() => window.__streams.length > beforeStreams, 5000);
  await sleep(200);
  const stream2 = lastStream();
  const noMemSources = { passages: [], web: null, memory: [] };
  stream2.emit("sources", noMemSources);
  stream2.emit("delta", { text: "第二个问题的回答。" });
  stream2.emit("done", { status: "done", answer: "第二个问题的回答。", error: "", sources: noMemSources, thread: "t1" });
  await until(() => lastBubble() && lastBubble().querySelector(".aacts button"), 4000);
  await sleep(150);
  out.b_空记忆_无块 = !doc.querySelector("#amessages .amem");
  out.b_空记忆_无空标题 = !/AI 记得的你/.test($("amessages").textContent);
  check("b_空记忆_无块", out.b_空记忆_无块, "memory 为空数组时不该渲染「AI 记得的你」这一块");
  check("b_空记忆_无空标题", out.b_空记忆_无空标题, "memory 为空时不该留下空标题");

  // ---------- 失败：后端返回 {"error": "..."} → 提示错误，且不能变成「已记住」
  store.failMem = true;
  const ansBtn2 = lastBubble().querySelector(".aacts button");
  click(ansBtn2);
  await sleep(400);
  out.失败_提示了错误 = /内容太短/.test($("toast").textContent);
  out.失败_文案仍是可点 = ansBtn2.textContent.trim();
  check("失败_提示了错误", out.失败_提示了错误,
    `保存失败时应提示后端返回的 error，实际 toast「${$("toast").textContent.trim()}」`);
  check("失败_没被标成已记住", !/已记住/.test(out.失败_文案仍是可点) && !ansBtn2.disabled,
    "保存失败后按钮不该变成已记住（否则用户以为存上了）");
  store.failMem = false;

  // ---------- (c) 知识库检索：memory 非空 → 结果列表上方「你记过的（N 条）」
  const HIT_TITLE = "命中测试这一篇";
  store.search = {
    count: 1, mode: "lexical",
    hits: [{ dir: DIR, doc_kind: "article", heading: "", kind: "body", podcast: "测试台",
             start_sec: null, text: "这是资料里的一段话。", title: HIT_TITLE }],
    memory: [
      { id: 7, text: MEM1, kind: "preference", pinned: true, source_dir: "" },
      { id: 8, text: MEM2, kind: "fact", pinned: false, source_dir: DIR },
    ],
  };
  window.showView("kb");
  await sleep(300);
  out.知识库_界面可见 = $("kbview").style.display === "block";
  $("kbs").value = "开篇";
  click(doc.querySelector(".kbsb button"));
  await until(() => $("kbhits").querySelector(".kbmem"), 5000);
  await sleep(150);

  const hitsBox = $("kbhits");
  const memBoxKb = hitsBox.querySelector(".kbmem");
  out.c_记忆命中块存在 = !!memBoxKb;
  out.c_记忆命中_标题 = memBoxKb ? memBoxKb.querySelector(".kbmemhead").textContent.trim() : "(没有)";
  out.c_记忆命中_条目 = memBoxKb ? [...memBoxKb.querySelectorAll(".memitem")].map((d) => ({
    标签: (d.querySelector(".memkind") || {}).textContent,
    文本: (d.querySelector(".memtext") || {}).textContent,
    置顶: /置顶/.test(d.textContent),
  })) : [];
  check("c_记忆命中块存在", out.c_记忆命中块存在, "检索返回 memory 非空时结果上方应出现「你记过的」");
  check("c_记忆命中_标题含条数", /你记过的/.test(out.c_记忆命中_标题) && /2 条/.test(out.c_记忆命中_标题),
    `标题应写成「你记过的（2 条）」，实际「${out.c_记忆命中_标题}」`);
  check("c_记忆命中_逐条显示文本", out.c_记忆命中_条目.map((x) => x.文本).join("|") === [MEM1, MEM2].join("|"),
    `应逐条显示记忆文本，实际 ${JSON.stringify(out.c_记忆命中_条目)}`);
  check("c_记忆命中_kind中文标签", out.c_记忆命中_条目[0] && out.c_记忆命中_条目[0].标签 === "偏好"
    && out.c_记忆命中_条目[1] && out.c_记忆命中_条目[1].标签 === "事实",
    `kind 要显示中文标签，实际 ${JSON.stringify(out.c_记忆命中_条目.map((x) => x.标签))}`);
  check("c_记忆命中_置顶标记", out.c_记忆命中_条目[0] && out.c_记忆命中_条目[0].置顶 === true
    && out.c_记忆命中_条目[1] && out.c_记忆命中_条目[1].置顶 === false,
    "置顶的那条要有「置顶」标记，没置顶的不能有");
  const flat = hitsBox.textContent;
  check("c_记忆命中_在结果上方",
    !!memBoxKb && hitsBox.firstElementChild === memBoxKb
    && flat.indexOf("你记过的") < flat.indexOf(HIT_TITLE),
    "「你记过的」必须排在检索结果列表**上方**");

  // 空数组 → 这一块不显示
  store.search = Object.assign({}, store.search, { memory: [] });
  click(doc.querySelector(".kbsb button"));
  await until(() => hitsBox.querySelector(".kbhit") && !hitsBox.querySelector(".kbmem"), 5000);
  out.c_空记忆_无块 = !hitsBox.querySelector(".kbmem") && !!hitsBox.querySelector(".kbhit");
  check("c_空记忆_无块", out.c_空记忆_无块, "memory 为空数组时不该显示「你记过的」这一块");

  // ---------- 知识库问答：memory_used →「AI 记得的你」+「记住这个结论」（dir 为空）
  store.kbAsk = {
    answer: KB_ANSWER, mode: "lexical", memory_used: [MEM1],
    sources: [{ dir: DIR, doc_kind: "article", heading: "", kind: "body", podcast: "测试台",
                start_sec: 607, text: "出处片段。", title: HIT_TITLE }],
  };
  $("kbq").value = "这些播客里关于开篇都说了什么？";
  click($("kbgobtn"));
  await until(() => $("kbanswer").querySelector(".kbaacts button"), 6000);
  await sleep(200);
  const kbAns = $("kbanswer");
  out.kb问答_记忆块 = kbAns.querySelector(".amem")
    ? [...kbAns.querySelectorAll(".amem li")].map((li) => li.textContent.trim()) : [];
  check("kb问答_memory_used_列出记忆", out.kb问答_记忆块.join("|") === MEM1,
    `memory_used 非空时回答下方应列出「AI 记得的你」，实际 ${JSON.stringify(out.kb问答_记忆块)}`);
  const kbBtn = kbAns.querySelector(".kbaacts button");
  out.kb问答_结论按钮 = kbBtn ? kbBtn.textContent.trim() : "(没有)";
  check("kb问答_有记住这个结论", !!kbBtn && /记住这个结论/.test(out.kb问答_结论按钮),
    `知识库回答下方应有「☆ 记住这个结论」，实际「${out.kb问答_结论按钮}」`);
  memPosts.length = 0;
  click(kbBtn);
  await until(() => memPosts.length > 0, 4000);
  await sleep(150);
  const kbBody = (memPosts[0] || {}).body || {};
  out.kb问答_请求 = { url: (memPosts[0] || {}).url, method: (memPosts[0] || {}).method, body: kbBody };
  check("kb问答_存的是回答正文", kbBody.text === KB_ANSWER, `应存回答正文，实际「${kbBody.text}」`);
  check("kb问答_dir为空_source为answer", kbBody.dir === "" && kbBody.source === "answer" && kbBody.kind === "insight",
    `知识库问答没有单一 dir，应传空串；实际 dir=「${kbBody.dir}」source=${kbBody.source} kind=${kbBody.kind}`);
  const beforeKbDup = memPosts.length;
  click(kbBtn);
  await sleep(250);
  check("d_kb结论_不重复POST", memPosts.length === beforeKbDup, "知识库的「记住这个结论」重复点也不该再发 POST");

  // 没有记忆参与时不留空标题
  store.kbAsk = { answer: "第二个问题的答案。", mode: "lexical", memory_used: [], sources: [] };
  $("kbq").value = "再问一个书库问题";
  click($("kbgobtn"));
  await until(() => /第二个问题的答案/.test($("kbanswer").textContent), 6000);
  await sleep(150);
  out.kb问答_空记忆_无块 = !$("kbanswer").querySelector(".amem");
  check("kb问答_空记忆_无块", out.kb问答_空记忆_无块, "memory_used 为空时不该显示「AI 记得的你」");

  // ---------- 设置页·记忆：每条显示用量，从没用过的那条要标出来
  const usedText = "常用的一条：开篇要具体场景";
  const unusedText = "从没用过的一条：某个记岔了的偏好";
  store.memList = {
    kinds: ["preference", "fact", "entity", "decision", "insight"],
    items: [
      { id: 11, text: usedText, kind: "preference", pinned: true, source_dir: "",
        use_count: 4, last_used_at: Math.floor(Date.now() / 1000) - 3 * 86400 },
      { id: 12, text: unusedText, kind: "fact", pinned: false, source_dir: "",
        use_count: 0, last_used_at: 0 },
    ],
  };
  await window.openSettings("memory");          // 切到「记忆」页签 → 拉列表并渲染
  const rowsReady = await until(() => doc.querySelectorAll("#memlist .memitem").length === 2, 6000);
  await sleep(150);
  const memRows = [...doc.querySelectorAll("#memlist .memitem")];
  out.用量_行数 = memRows.length;
  check("用量_列表渲染", rowsReady && memRows.length === 2,
    `记忆页应渲染 2 条，实际 ${memRows.length} 条`);

  const usageOf = (row) => {
    const el = row ? row.querySelector(".memuse") : null;
    return el ? { 文案: el.textContent.trim(), 样式: el.className } : null;
  };
  out.用量_有次数的那条 = usageOf(memRows[0]);
  out.用量_从未用过的那条 = usageOf(memRows[1]);
  check("用量_显示用过几次和多久前",
    out.用量_有次数的那条 && /用过 4 次/.test(out.用量_有次数的那条.文案)
    && /3 天前/.test(out.用量_有次数的那条.文案),
    `用过的应写「用过 N 次 · 最近 M 天前」，实际 ${JSON.stringify(out.用量_有次数的那条)}`);
  // (a) use_count=0 → 「从未用过」（而不是 1970 年）
  check("用量_从未用过", out.用量_从未用过的那条 && out.用量_从未用过的那条.文案 === "从未用过"
    && /unused/.test(out.用量_从未用过的那条.样式),
    `use_count=0 的那条应显示「从未用过」并用弱化样式，实际 ${JSON.stringify(out.用量_从未用过的那条)}`);
  out.用量_没有1970 = !/1970/.test($("memlist").textContent);
  check("用量_没有1970", out.用量_没有1970,
    "last_used_at=0 是「没发生过」，不该渲染成 1970 年");

  // 汇总 + 筛选开关
  out.汇总_文案 = ((doc.querySelector("#memlist .memsum") || { textContent: "" }).textContent || "")
    .replace(/\s+/g, " ").trim();
  check("汇总_共几条其中几条从未用过", /共 2 条/.test(out.汇总_文案) && /1 条从未用过/.test(out.汇总_文案),
    `汇总应写「共 N 条，其中 M 条从未用过」，实际「${out.汇总_文案}」`);
  out.筛选_按钮文案 = ($("memfilter") || { textContent: "" }).textContent.trim();
  check("筛选_有开关", !!$("memfilter"), "列表上方应有一个「只看从未用过的」开关");

  // (b) 点筛选 → 只剩从未用过的那些
  click($("memfilter"));
  await sleep(200);
  const onlyUnused = [...doc.querySelectorAll("#memlist .memitem")];
  out.筛选后_条目 = onlyUnused.map((r) => r.textContent.replace(/\s+/g, " ").trim().slice(0, 30));
  out.筛选后_按钮文案 = ($("memfilter") || { textContent: "" }).textContent.trim();
  check("筛选_只剩从未用过的",
    onlyUnused.length === 1 && /从未用过/.test(onlyUnused[0].textContent)
    && onlyUnused[0].textContent.includes(unusedText),
    `点筛选后应只剩从未用过的那条，实际 ${JSON.stringify(out.筛选后_条目)}`);
  check("筛选_按钮变成恢复全部", /显示全部/.test(out.筛选后_按钮文案),
    `筛选生效后按钮应提示可恢复，实际「${out.筛选后_按钮文案}」`);

  // 再点一次 → 恢复全部
  click($("memfilter"));
  await sleep(200);
  out.筛选_再点恢复 = doc.querySelectorAll("#memlist .memitem").length === 2;
  check("筛选_再点恢复", out.筛选_再点恢复, "再点一次开关应恢复显示全部");

  out.全程_没有真实写入记忆 = memPosts.every((p) => p.url === "/api/memory");
  report("记忆入口（阅读页 / 回答 / 知识库 / 设置页用量）通过", out, fails);
})();
