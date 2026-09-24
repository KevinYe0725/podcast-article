/**
 * 一个输入框，两种意图（产品定位变了：知识库服务于 AI，不是人的入口）。
 *
 *   粘链接 → 生成文章（原来的 startRun）
 *   打字   → 问你的库（#askpanel：先出出处、再出答案，像对话一样一轮轮往下接）
 *
 * 界面里不再有「知识库」这个页面 —— 检索、索引、实体都不该出现在用户面前，
 * 但索引坏了得能修，所以「维护」收在设置 → 记忆的折叠区里。
 *
 * 硬约束（跟 memory.test.js 一致）：
 *   · 不真问模型：/api/kb/ask 全打桩；
 *   · 不真跑生成：/api/run 一律返回失败，别在测试里起任务；
 *   · 不真写记忆：/api/memory 打桩，并断言发出去的 body；
 *   · 不真碰真书库：/api/kb/status 与 /api/kb/search 都打桩（规模写死在 12 集 / 5612 条）。
 *
 * 为了断言「依据先出现、答案后出现」这种时序，这里用**可手动驱动的假 fetch**：
 * 检索与问答两个应答都攥在测试手里，测试决定什么时候放行。
 */
const { boot, until, report, sleep } = require("./harness");

const QUESTION = "这些播客里关于开篇都说了什么？";
const LINK = "https://example.com/episode/ui-asklibrary";
const MEM = "用户偏好：开篇用具体场景切入";
const KB_ANSWER = "结论：开篇一律用具体场景切入，再补背景。";
const HIT_TITLE = "命中测试这一篇";

(async () => {
  const out = {}, fails = [];
  const check = (name, cond, msg) => { out[name] = cond; if (!cond) fails.push(msg); };

  const calls = [];        // 前端发出的所有请求（按发生顺序）
  const memPosts = [];     // 记忆写入请求
  const gate = {};         // 手动驱动的应答：gate.search / gate.ask 由测试决定何时放行
  let manual = false;      // 打开后，检索与问答的 fetch 都挂着，等测试放行

  const store = {
    status: {
      db: "(测试)", size_bytes: 1048576, docs: 12, passages: 5612, vectors: 5612,
      entities: 40, memory: 3, episodes_on_disk: 12, episodes_indexed: 12,
      semantic: true, embed_error: "", indexing: { state: "idle", done: 0, total: 0, error: "" },
    },
    search: {
      count: 1, mode: "lexical", memory: [],
      hits: [{ dir: "__UI测试单集", doc_kind: "article", heading: "第一节", kind: "body",
               podcast: "测试台", start_sec: 607, text: "这是资料里的一段话。", title: HIT_TITLE }],
    },
    ask: { answer: "", sources: [] },
  };

  const respObj = (obj, ok = true) => ({
    ok, status: ok ? 200 : 400,
    headers: { get: () => "application/json" },
    json: async () => obj,
    text: async () => JSON.stringify(obj),
  });
  const fakeResp = (obj, ok = true) => Promise.resolve(respObj(obj, ok));
  /** 「先不发」：把应答挂住，返回前先把放行函数交给测试（用来控制先后顺序） */
  const hold = (key, obj) => new Promise((resolve) => { gate[key] = () => resolve(respObj(obj)); });

  const { window, doc, $, click } = await boot({
    beforeParse(w) {
      const realFetch = w.fetch;
      w.fetch = (u, o) => {
        const url = String(u);
        const method = String((o && o.method) || "GET").toUpperCase();
        calls.push({
          url, method, body: (o && o.body) || null,
          csrf: new Headers((o && o.headers) || {}).get("X-CSRF-Token") || "",
        });
        if (url.includes("/api/kb/status")) return fakeResp(store.status);
        if (url.includes("/api/kb/search")) return manual ? hold("search", store.search) : fakeResp(store.search);
        if (url.includes("/api/kb/ask")) return manual ? hold("ask", store.ask) : fakeResp(store.ask);
        if (url.includes("/api/kb/reindex")) return fakeResp({ state: "running", indexing: store.status.indexing });
        if (url.includes("/api/run")) return fakeResp({ error: "测试打桩：不真的跑" }, false);
        if (url.includes("/api/memory")) {
          if (method === "POST") { memPosts.push({ url, method, body: JSON.parse(o.body) }); return fakeResp({ id: 1 }); }
          return fakeResp({ items: [], kinds: ["preference", "fact", "entity", "decision", "insight"] });
        }
        return realFetch(u, o);
      };
      // 「重建索引」有一步确认（重建会重算切片），打桩成「用户点了确定」
      w.confirm = () => true;
    },
  });

  const hint = () => ($("urlhint").textContent || "").replace(/\s+/g, " ").trim();
  const btnText = () => $("go").textContent.trim();
  const type = async (text) => {
    $("url").value = text;
    $("url").dispatchEvent(new window.Event("input", { bubbles: true }));
    await sleep(150);
  };
  const turns = () => [...$("askpanel").querySelectorAll(".askturn")];

  // ---------- 0) 导航里不再有「知识库」这个页面
  const navText = (doc.querySelector(".sidenav") || { textContent: "" }).textContent.replace(/\s+/g, " ").trim();
  out.导航_按钮不存在 = !$("navkb");
  out.导航_视图不存在 = !$("kbview");
  out.导航_文案 = navText;
  check("导航_没有知识库按钮", out.导航_按钮不存在, "侧边栏不该再有「知识库」入口（#navkb）");
  check("导航_没有知识库视图", out.导航_视图不存在, "「知识库」整页 DOM（#kbview）该删掉 —— 它不是用户的入口");
  check("导航_文案里没有知识库", !/知识库/.test(navText), `导航里还留着「知识库」字样：「${navText}」`);

  // ---------- 1) 空输入：不提示意图，按钮是「生成文章」
  out.空_按钮 = btnText();
  out.空_提示隐藏 = $("urlhint").style.display === "none" && hint() === "";
  check("空_按钮是生成文章", out.空_按钮 === "生成文章", `空输入时按钮应是「生成文章」，实际「${out.空_按钮}」`);
  check("空_不提示意图", out.空_提示隐藏, "空输入时不该显示意图提示");

  // ---------- 2) 粘链接 → 生成文章（绝不能走问答）
  await type(LINK);
  out.链接_提示 = hint();
  out.链接_按钮 = btnText();
  check("链接_提示将生成文章", /将生成文章/.test(out.链接_提示), `粘链接时 #urlhint 应写「🔗 将生成文章」，实际「${out.链接_提示}」`);
  check("链接_按钮是生成文章", out.链接_按钮 === "生成文章", `粘链接时按钮应是「生成文章」，实际「${out.链接_按钮}」`);
  const beforeLink = calls.length;
  click($("go"));
  await sleep(400);
  const linkCalls = calls.slice(beforeLink);
  out.链接_发出的请求 = linkCalls.map((c) => `${c.method} ${c.url}`);
  check("链接_走的是生成文章", linkCalls.some((c) => c.url.includes("/api/run")), `粘链接点按钮应发 /api/run，实际 ${JSON.stringify(out.链接_发出的请求)}`);
  const runRequest = linkCalls.find((c) => c.method === "POST" && c.url.includes("/api/run"));
  out.链接_携带CSRF令牌 = !!runRequest?.csrf;
  check("链接_携带CSRF令牌", out.链接_携带CSRF令牌, "生成文章的 POST /api/run 必须携带 X-CSRF-Token");
  check("链接_不问库", !linkCalls.some((c) => c.url.includes("/api/kb/ask")), "粘链接点按钮不该发 /api/kb/ask");

  // ---------- 3) 打字提问 → 问你的库
  store.ask = { answer: KB_ANSWER, mode: "lexical", memory_used: [MEM], sources: store.search.hits };
  await type(QUESTION);
  out.提问_提示 = hint();
  out.提问_按钮 = btnText();
  check("提问_提示问你的库", /💬 ?问你的库/.test(out.提问_提示), `打字时应提示「💬 问你的库」，实际「${out.提问_提示}」`);
  check("提问_按钮问我的库", out.提问_按钮 === "问我的库", `打字时按钮应写「问我的库」，实际「${out.提问_按钮}」`);
  // 规模是异步从 /api/kb/status 补上的（拿不到就只说「问你的库」）
  await until(() => /12 集/.test(hint()), 4000);
  out.提问_提示带规模 = hint();
  check("提问_提示带规模", /12 集/.test(out.提问_提示带规模) && /5612 条切片/.test(out.提问_提示带规模),
    `提示里应带上书库规模（12 集 · 5612 条切片），实际「${out.提问_提示带规模}」`);

  // 点按钮：先 GET /api/kb/search（快）→ 把出处摆出来 → 再 POST /api/kb/ask（慢）
  manual = true;
  const beforeAsk = calls.length;
  click($("go"));
  const gotSearch = await until(() => gate.search, 5000);
  const firstKb = calls.slice(beforeAsk).filter((c) => c.url.includes("/api/kb/"));
  out.提问_首发请求 = firstKb.map((c) => `${c.method} ${c.url}`);
  check("提问_先发检索", gotSearch && firstKb.length === 1 && firstKb[0].method === "GET" && firstKb[0].url.includes("/api/kb/search"),
    `点「问我的库」应先发一次 GET /api/kb/search，实际 ${JSON.stringify(out.提问_首发请求)}`);
  out.提问_面板可见 = $("askpanel").style.display !== "none";
  check("提问_面板出现", out.提问_面板可见, "提问后应出现 #askpanel");
  out.提问_问题行 = (turns()[0] ? turns()[0].querySelector(".askq") : { textContent: "" }).textContent.trim();
  out.提问_状态行 = (turns()[0] ? turns()[0].querySelector(".askstate") : { textContent: "" }).textContent.trim();
  out.提问_输入框已清空 = $("url").value === "";
  check("提问_问题渲染出来", out.提问_问题行 === QUESTION, `面板里应先把问题写出来，实际「${out.提问_问题行}」`);
  check("提问_状态是检索中", /检索/.test(out.提问_状态行), `检索时应写状态「正在检索…」，实际「${out.提问_状态行}」`);
  check("提问_输入框已清空", out.提问_输入框已清空, "提问后首页输入框应清空（问题已经进了对话流）");
  out.检索未回_无依据 = !turns()[0].querySelector(".kbhit") && !turns()[0].querySelector(".asksrc").textContent.trim();
  check("检索未回_无依据", out.检索未回_无依据, "检索还没回来时不该凭空出现出处");

  // 放行检索：立刻出依据（状态切到「正在回答…」），此时答案还不该出现
  gate.search();
  const gotSources = await until(() => turns()[0] && turns()[0].querySelector(".kbhit"), 5000);
  await sleep(100);
  const turn = turns()[0];
  const srcDetails = turn.querySelector("details");
  out.依据_已出现 = gotSources;
  out.依据_summary = srcDetails ? srcDetails.querySelector("summary").textContent.trim() : "(没有)";
  out.依据_默认收起 = !!srcDetails && srcDetails.open === false;
  out.依据_时间戳 = srcDetails && srcDetails.querySelector("a.ts")
    ? `${srcDetails.querySelector("a.ts").dataset.sec}|${srcDetails.querySelector("a.ts").dataset.dir}` : "(没有)";
  out.依据_状态行 = turn.querySelector(".askstate").textContent.trim();
  out.依据_答案还没出现 = !/结论：开篇/.test(turn.textContent);
  check("依据_先出现在面板里", gotSources, "检索回来后应立刻把出处渲染到 #askpanel（别让用户对着空白等模型）");
  check("依据_summary文案", /出处（1 条，时间戳可点回听）/.test(out.依据_summary), `summary 应写「出处（N 条，时间戳可点回听）」，实际「${out.依据_summary}」`);
  check("依据_默认收起", out.依据_默认收起, "出处要默认收起（默认展开会把答案挤到看不见）");
  check("依据_时间戳可回听", out.依据_时间戳 === "607|__UI测试单集", `出处里的时间戳要能点回听（data-sec / data-dir），实际 ${out.依据_时间戳}`);
  check("依据_状态切到正在回答", /正在回答/.test(out.依据_状态行), `出处出来后状态应写「正在回答…」，实际「${out.依据_状态行}」`);
  check("两段式_答案后到", out.依据_答案还没出现, "出处渲染出来时答案还不该出现（模型那一趟更慢）");

  // 再放行问答
  const gotAsk = await until(() => gate.ask, 5000);
  check("提问_依据之后才发问答", gotAsk, "出处渲染完之后才应该发 POST /api/kb/ask");
  const afterAsk = calls.slice(beforeAsk).filter((c) => c.url.includes("/api/kb/"));
  out.提问_请求顺序 = afterAsk.map((c) => `${c.method} ${c.url}`);
  const iSearch = afterAsk.findIndex((c) => c.url.includes("/api/kb/search"));
  const iAsk = afterAsk.findIndex((c) => c.method === "POST" && c.url.includes("/api/kb/ask"));
  check("提问_先检索后问答", iSearch === 0 && iAsk === 1,
    `应「先 GET /api/kb/search，再 POST /api/kb/ask」，实际 ${JSON.stringify(out.提问_请求顺序)}`);
  gate.ask();
  await until(() => /结论：开篇/.test(turn.querySelector(".askans").textContent), 5000);
  await sleep(150);

  out.答案_渲染 = turn.querySelector(".askans").textContent.trim().slice(0, 40);
  check("答案_渲染进面板", /结论：开篇/.test(turn.querySelector(".askans").textContent), `答案应渲染进 #askpanel 的 .askans，实际「${out.答案_渲染}」`);
  const follows = turn.querySelector(".asksrc").compareDocumentPosition(turn.querySelector(".askans"));
  out.答案_在依据之下 = (follows & window.Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
  check("答案_在依据之下", out.答案_在依据之下, "一轮里的顺序是 问题 → 状态 → 出处 → 答案");

  // memory_used → 默认收起的「AI 记得的你」
  const memDetails = turn.querySelector(".amem");
  out.记忆_是折叠块 = !!memDetails && memDetails.tagName === "DETAILS";
  out.记忆_默认收起 = !!memDetails && memDetails.open === false;
  out.记忆_summary = memDetails ? memDetails.querySelector("summary").textContent.trim() : "(没有)";
  out.记忆_条目 = memDetails ? [...memDetails.querySelectorAll("li")].map((li) => li.textContent.trim()) : [];
  check("记忆_是折叠块且默认收起", out.记忆_是折叠块 && out.记忆_默认收起,
    "memory_used 非空时要出现默认收起的「AI 记得的你」（内部机制不该摆在台面上）");
  check("记忆_summary带条数", /AI 记得的你（1 条）/.test(out.记忆_summary), `summary 应写「AI 记得的你（N 条）」，实际「${out.记忆_summary}」`);
  check("记忆_逐条列出", out.记忆_条目.join("|") === MEM, `应逐条列出这次参考的记忆，实际 ${JSON.stringify(out.记忆_条目)}`);

  // 「☆ 记住这个结论」：存回答正文，dir 为空串（跨集问答没有单一 dir）
  const ansBtn = turn.querySelector(".kbaacts button");
  out.答案_按钮文案 = ansBtn ? ansBtn.textContent.trim() : "(没有)";
  check("答案_有记住这个结论", !!ansBtn && /记住这个结论/.test(out.答案_按钮文案), `答案下方应有「☆ 记住这个结论」，实际「${out.答案_按钮文案}」`);
  click(ansBtn);
  await until(() => memPosts.length > 0, 4000);
  await sleep(150);
  const kbBody = (memPosts[0] || {}).body || {};
  out.答案_请求 = { url: (memPosts[0] || {}).url, method: (memPosts[0] || {}).method, body: kbBody };
  check("答案_存的是回答正文", kbBody.text === KB_ANSWER, `应存回答正文，实际「${kbBody.text}」`);
  check("答案_kind_dir_source", kbBody.kind === "insight" && kbBody.dir === "" && kbBody.source === "answer",
    `跨集问答没有单一 dir，应传空串；实际 dir=「${kbBody.dir}」source=${kbBody.source} kind=${kbBody.kind}`);

  // ---------- 4) 连续提问：往下追加，不覆盖上一轮
  manual = false;
  store.ask = { answer: "第二个问题的答案。", mode: "lexical", memory_used: [], sources: [] };
  await type("再问一个书库问题");
  click($("go"));
  await until(() => /第二个问题的答案/.test($("askpanel").textContent), 6000);
  await sleep(150);
  const all = turns();
  out.连续_轮数 = all.length;
  out.连续_上一轮还在 = /结论：开篇/.test(all[0].textContent);
  check("连续_追加不覆盖", all.length === 2 && out.连续_上一轮还在,
    `第二轮要追加在下面（上一轮不能消失），实际 ${all.length} 轮`);
  out.连续_第二轮无记忆块 = !all[1].querySelector(".amem");
  out.连续_第二轮无空标题 = !/AI 记得的你/.test(all[1].textContent);
  check("连续_空记忆_无块", out.连续_第二轮无记忆块, "memory_used 为空时不该渲染「AI 记得的你」");
  check("连续_空记忆_无空标题", out.连续_第二轮无空标题, "memory_used 为空时不该留下空标题");

  // 关闭按钮：收起面板（不是清空对话）
  const closeBtn = $("askpanel").querySelector(".askhead button");
  out.面板_有关闭按钮 = !!closeBtn;
  check("面板_有关闭按钮", out.面板_有关闭按钮, "#askpanel 头部应有收起按钮");
  if (closeBtn) {
    click(closeBtn);
    await sleep(120);
    out.面板_收起后隐藏 = $("askpanel").style.display === "none";
    check("面板_能收起", out.面板_收起后隐藏, "点收起后 #askpanel 应隐藏");
    await type("第三个问题");
    click($("go"));
    await until(() => turns().length === 3, 6000);
    out.面板_再问又出现 = $("askpanel").style.display !== "none" && turns().length === 3;
    check("面板_再问又出现", out.面板_再问又出现, "收起后再提问应重新展开面板，并且之前的对话还在");
    out.面板_收起不清空 = /结论：开篇/.test($("askpanel").textContent);
    check("面板_收起不清空", out.面板_收起不清空, "收起面板不该把已有的对话清掉");
  }

  // ---------- 5) 设置 → 记忆：知识库（AI 的底座）折叠区
  await window.openSettings("memory");
  await sleep(250);
  const admin = $("kbadmin");
  out.设置_有折叠区 = !!admin && admin.tagName === "DETAILS";
  out.设置_默认收起 = !!admin && admin.open === false;
  out.设置_summary = admin ? (admin.querySelector("summary").textContent || "").trim() : "(没有)";
  check("设置_有折叠区且默认收起", out.设置_有折叠区 && out.设置_默认收起,
    "设置 → 记忆 里应有一个默认收起的「知识库」折叠区（索引坏了得能修）");
  check("设置_summary说明是底座", /知识库/.test(out.设置_summary) && /不用管/.test(out.设置_summary),
    `summary 应写清「知识库（AI 用的底座，日常不用管）」，实际「${out.设置_summary}」`);
  if (admin) {
    admin.open = true;
    admin.dispatchEvent(new window.Event("toggle"));
    await until(() => /5612 条切片/.test($("kb_setstate").textContent), 5000);
    await sleep(120);
    out.设置_状态行 = $("kb_setstate").textContent.replace(/\s+/g, " ").trim();
    check("设置_状态行", /12 集/.test(out.设置_状态行) && /5612 条切片/.test(out.设置_状态行)
      && /5612 条向量/.test(out.设置_状态行) && /语义检索 开/.test(out.设置_状态行),
      `展开后应显示「N 集 · N 条切片 · N 条向量 · 语义检索 开/关」，实际「${out.设置_状态行}」`);

    const beforeIdx = calls.length;
    const idxBtn = [...admin.querySelectorAll("button")].find((b) => /重建索引/.test(b.textContent));
    out.设置_有重建按钮 = !!idxBtn;
    check("设置_有重建按钮", out.设置_有重建按钮, "折叠区里应有「重建索引」按钮");
    if (idxBtn) {
      click(idxBtn);
      await until(() => calls.slice(beforeIdx).some((c) => c.url.includes("/api/kb/reindex")), 4000);
      const re = calls.slice(beforeIdx).find((c) => c.url.includes("/api/kb/reindex"));
      out.设置_重建请求 = re ? { method: re.method, url: re.url, body: re.body } : "(没有)";
      check("设置_重建发请求", !!re && re.method === "POST" && /"force":\s*true/.test(String(re.body)),
        `点「重建索引」应 POST /api/kb/reindex 且 force=true，实际 ${JSON.stringify(out.设置_重建请求)}`);
    }
  }

  report("一个输入框两种意图（生成文章 / 问你的库）通过", out, fails);
})();
