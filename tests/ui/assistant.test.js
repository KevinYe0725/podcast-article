/**
 * AI 阅读助手的前端交互测试。
 *
 * 关键约束：**绝不能真的发起一次提问**（那会真的调用大模型、花钱且慢）。
 * 做法是在页面里把两处出口换掉：
 *   - window.fetch 对 /api/ask 开头的请求返回假的响应（其余请求照常打到真实服务）
 *   - window.EventSource 换成可手动驱动的假实现，由测试决定什么时候推 sources / delta / done
 * 这样流的渲染逻辑仍然是被真实执行的那份代码。
 */
const { boot, report, sleep, until } = require("./harness");
const fs = require("fs");
const path = require("path");

const DIR = "__UI测试单集";        // seed.js 造的那一集

(async () => {
  const out = {}, fails = [];
  const asked = [];               // 记录前端发起的提问请求

  const { window, doc, $, click, esc } = await boot({
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
        if (url.includes("/api/ask")) {
          asked.push({ url, body: o && o.body ? JSON.parse(o.body) : null });
          return fakeResp({ id: "fakeask1", web: true });
        }
        return realFetch(u, o);
      };

      // 可手动驱动的 EventSource
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

      // jsdom 的 Range 没有真实布局，getBoundingClientRect 全 0，浮出按钮会以为没选区
      w.Range.prototype.getBoundingClientRect = () => ({
        width: 120, height: 16, top: 220, left: 120, right: 240, bottom: 236,
      });
    },
  });

  const fab = () => $("fab");
  const selbtn = () => $("selbtn");
  const drawer = () => $("assist");

  // ---------- 1) 没打开文章时不显示悬浮球
  out.初始_悬浮球隐藏 = !fab().classList.contains("show");
  if (!out.初始_悬浮球隐藏) fails.push("没有打开文章时不该显示悬浮球");

  await window.openEpisode(encodeURIComponent(DIR));
  await sleep(900);
  out.打开文章_悬浮球出现 = fab().classList.contains("show");
  out.悬浮球文案 = fab().textContent.trim();
  if (!out.打开文章_悬浮球出现) fails.push("打开文章后应出现悬浮球");

  // ---------- 2) 选中正文里的文字 → 浮出「深挖这段」
  const para = [...doc.querySelectorAll("#article p")].find((p) => p.textContent.trim().length > 10);
  if (!para) { console.log("文章里没有可用段落"); process.exit(1); }
  const range = doc.createRange();
  range.selectNodeContents(para);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  doc.dispatchEvent(new window.MouseEvent("mouseup", { bubbles: true }));
  await sleep(400);

  out.选中后_浮出按钮 = selbtn().classList.contains("show");
  out.选中后_按钮文案 = selbtn().textContent.trim();
  out.选中后_按钮位置 = `${selbtn().style.top} / ${selbtn().style.left}`;
  if (!out.选中后_浮出按钮) fails.push("在正文里选中文字后应浮出「深挖这段」按钮");

  // ---------- 3) 点它 → 开抽屉、带上选中的原文、发出提问
  click(selbtn());
  await until(() => drawer().classList.contains("show"), 4000);
  await sleep(300);

  out.抽屉出现 = drawer().classList.contains("show");
  out.抽屉里_选中原文 = $("aqtext").textContent.trim().slice(0, 40);
  out.抽屉里_有选中块 = $("aquote").style.display !== "none";
  out.悬浮球在抽屉打开时隐藏 = !fab().classList.contains("show");
  out.浮出按钮已收起 = !selbtn().classList.contains("show");
  if (!out.抽屉出现) fails.push("点「深挖这段」应打开右侧抽屉");
  if (!out.抽屉里_选中原文) fails.push("抽屉里应展示选中的原文");
  if (!out.悬浮球在抽屉打开时隐藏) fails.push("抽屉打开时悬浮球应让位");

  // ---------- 4) 提问请求带上了正确的参数
  await until(() => asked.length > 0, 4000);
  out.提问请求数 = asked.length;
  const body = asked[0] ? asked[0].body : {};
  out.提问参数 = { dir: body.dir, 有选文: !!body.selection, web: body.web, mode: body.mode };
  // 篇幅默认必须是简洁档（用户反馈：旧默认 914 字里只有一段是回答所问的）
  if (body.mode !== "concise") fails.push(`默认篇幅应为 concise，实际 ${body.mode}`);
  out.篇幅开关_存在 = !!$("adetail");
  if (!out.篇幅开关_存在) fails.push("抽屉里应有「详细」开关");
  // 勾上「详细」后再问，要带上 detail
  $("adetail").checked = true;
  window.askAI();
  await sleep(600);
  out.勾选详细后的mode = asked[asked.length - 1]["body"] && asked[asked.length - 1].body.mode;
  $("adetail").checked = false;
  if (out.勾选详细后的mode !== "detail") {
    fails.push(`勾选「详细」后应传 detail，实际 ${out.勾选详细后的mode}`);
  }
  if (!asked.length) fails.push("点「深挖这段」应发出 /api/ask 请求");
  if (body.dir !== DIR) fails.push(`提问请求的 dir 应为 ${DIR}，实际 ${body.dir}`);
  if (!body.selection) fails.push("提问请求应带上选中的文字");

  // ---------- 5) 服务端推 sources → 依据区块渲染出来
  const stream = window.__streams[window.__streams.length - 1];
  out.已建立SSE = !!stream && /\/api\/ask\/fakeask1\/stream/.test(stream.url);
  if (!out.已建立SSE) fails.push("应建立 SSE 连接订阅解读流");

  stream.emit("sources", {
    passages: [
      { ts: "00:10:07", start: 607, text: "这是原文里的第一段。" },
      { ts: "00:12:30", start: 750, text: "这是原文里的第二段。" },
    ],
    web: { ok: true, provider: "bing", results: [
      { title: "参考文章标题", url: "https://example.com/a", snippet: "这是摘要" },
    ]},
  });
  await sleep(300);

  out.依据_可见 = $("asources").style.display !== "none";
  out.依据_片段数 = doc.querySelectorAll("#apassages .apass").length;
  out.依据_标签 = $("apcount").textContent.trim();
  out.依据_时间戳 = [...doc.querySelectorAll("#apassages .ts")].map((a) => a.textContent.trim());
  out.依据_时间戳秒数 = [...doc.querySelectorAll("#apassages .ts")].map((a) => a.dataset.sec);
  out.网络_条数 = doc.querySelectorAll("#aweb .aweb").length;
  out.网络_标题 = doc.querySelector("#aweb .awt") ? doc.querySelector("#aweb .awt").textContent.trim() : "";
  out.网络_链接 = doc.querySelector("#aweb .aweb") ? doc.querySelector("#aweb .aweb").getAttribute("href") : "";
  out.网络_新窗口打开 = doc.querySelector("#aweb .aweb") ? doc.querySelector("#aweb .aweb").getAttribute("target") : "";
  if (!out.依据_可见) fails.push("收到 sources 后应显示「原文依据」区块");
  if (out.依据_片段数 !== 2) fails.push(`原文依据应渲染 2 段，实际 ${out.依据_片段数}`);
  if (out.依据_时间戳.join() !== "[00:10:07],[00:12:30]") fails.push(`时间戳渲染不对：${out.依据_时间戳}`);
  if (out.依据_时间戳秒数.join() !== "607,750") fails.push(`时间戳秒数不对：${out.依据_时间戳秒数}`);
  if (out.网络_条数 !== 1) fails.push(`网络结果应渲染 1 条，实际 ${out.网络_条数}`);
  if (out.网络_链接 !== "https://example.com/a") fails.push(`网络结果链接不对：${out.网络_链接}`);
  if (out.网络_新窗口打开 !== "_blank") fails.push("网络结果应在新窗口打开");

  // ---------- 6) 逐段推 delta → 正文流式追加
  stream.emit("delta", { text: "第一段解读。" });
  await sleep(180);
  out.流式_中途1 = $("aanswer").textContent.trim();
  out.流式_光标 = $("aanswer").classList.contains("streaming");
  stream.emit("delta", { text: "\n\n第二段，引用原话：\n> 这是原话 [00:10:07]\n\n**还可以往哪追**\n- 一个具体方向" });
  await sleep(180);
  out.流式_中途2长度 = $("aanswer").textContent.trim().length;
  if (!/第一段解读。/.test(out.流式_中途1)) fails.push(`第一次 delta 应立即渲染，实际「${out.流式_中途1}」`);
  if (!out.流式_光标) fails.push("流式过程中应有 streaming 光标");
  if (!(out.流式_中途2长度 > out.流式_中途1.length)) fails.push("后续 delta 应追加而不是覆盖");

  stream.emit("done", {
    status: "done", answer: $("aanswer").textContent, error: "",
    sources: { passages: [], web: null },
  });
  await sleep(400);

  out.完成后_光标消失 = !$("aanswer").classList.contains("streaming");
  out.完成后_引用块 = doc.querySelectorAll("#aanswer blockquote").length;
  out.完成后_粗体 = doc.querySelectorAll("#aanswer strong").length;
  out.完成后_列表项 = doc.querySelectorAll("#aanswer li").length;
  out.完成后_解读里的时间戳 = [...doc.querySelectorAll("#aanswer .ts")].map((a) => a.textContent.trim());
  out.完成后_按钮恢复 = !$("agobtn").disabled;
  out.完成后_状态文案 = $("astatus").textContent.trim();
  if (!out.完成后_光标消失) fails.push("结束后应去掉流式光标");
  if (out.完成后_引用块 !== 1) fails.push(`解读里的 > 引用应渲染成 blockquote，实际 ${out.完成后_引用块}`);
  if (out.完成后_粗体 < 1) fails.push("解读里的 **粗体** 应渲染成 strong");
  if (out.完成后_列表项 !== 1) fails.push(`解读里的 - 列表应渲染成 li，实际 ${out.完成后_列表项}`);
  if (out.完成后_解读里的时间戳.join() !== "[00:10:07]") fails.push(`解读里的时间戳应被识别，实际 ${out.完成后_解读里的时间戳}`);
  if (!out.完成后_按钮恢复) fails.push("结束后「深挖」按钮应恢复可用");

  // ---------- 7) 解读里的时间戳点了能回听（复用文章那套播放器）
  const ts = doc.querySelector("#aanswer .ts");
  ts.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(400);
  out.解读时间戳_播放条出现 = $("player").classList.contains("show");
  out.解读时间戳_音频地址 = ($("paudio").getAttribute("src") || "").includes(encodeURIComponent(DIR)) ? "指向本集" : "(没指向本集)";
  out.解读时间戳_悬浮球上移 = doc.body.classList.contains("withplayer");
  if (!out.解读时间戳_播放条出现) fails.push("点解读里的时间戳应能回听音频");
  if (out.解读时间戳_音频地址 !== "指向本集") fails.push("音频应指向当前这一集");
  if (!out.解读时间戳_悬浮球上移) fails.push("播放条升起时悬浮球应上移（body.withplayer）");
  window.closePlayer();

  // ---------- 8) 历史问答面板
  click($("ahistbtn"));
  await sleep(600);
  out.历史面板可见 = $("ahistory").style.display !== "none";
  out.历史文案 = $("ahcount").textContent.trim();
  if (!out.历史面板可见) fails.push("点历史按钮应展开历史问答面板");

  // ---------- 9) Esc 关闭抽屉（且不影响文章）
  esc();
  await sleep(300);
  out.Esc_抽屉已关 = !drawer().classList.contains("show");
  out.Esc_文章还在 = $("result").classList.contains("show");
  out.Esc_悬浮球回来了 = fab().classList.contains("show");
  if (!out.Esc_抽屉已关) fails.push("Esc 应关闭抽屉");
  if (!out.Esc_文章还在) fails.push("Esc 关抽屉时不该把文章也关掉");
  if (!out.Esc_悬浮球回来了) fails.push("抽屉关闭后悬浮球应回来");

  // ---------- 10) 只用悬浮球提问（没有选文，直接问问题）
  click(fab());
  await sleep(400);
  out.直接提问_抽屉打开 = drawer().classList.contains("show");
  out.直接提问_无选文时隐藏引用块 = $("aquote").style.display === "none";
  asked.length = 0;
  $("aq").value = "他提到的 SGLang 是什么？";
  click($("agobtn"));
  await until(() => asked.length > 0, 4000);
  const body2 = asked.length ? asked[0].body : {};
  out.直接提问_参数 = { question: body2.question, 无选文: !body2.selection };
  if (!asked.length) fails.push("抽屉里直接提问也应发出请求");
  if (body2.question !== "他提到的 SGLang 是什么？") fails.push("提问内容应原样带上");

  // 空提问 + 空选文时应给出提示而不是发请求
  // 注意：先把抽屉里显示的选文清掉（界面上就是引用块右上角那个「清除」），
  // 否则「深挖」会针对仍然显示着的选文再问一次 —— 那是设计行为，不是这次的用例。
  window.setAssistSelection("");
  await sleep(150);
  out.清除选文_引用块已隐藏 = $("aquote").style.display === "none";
  if (!out.清除选文_引用块已隐藏) fails.push("清除选文后引用块应隐藏");
  window.__streams.length = 0;
  asked.length = 0;
  $("aq").value = "";
  click($("agobtn"));
  await sleep(300);
  out.空提问_未发请求 = asked.length === 0;
  out.空提问_有提示 = $("toast").textContent.trim().slice(0, 20);
  if (!out.空提问_未发请求) fails.push("既没选文又没提问时不该发请求");
  if (!$("toast").textContent.trim()) fails.push("空提问时应给出提示");

  // ---------- 11) 关掉文章 → 抽屉与悬浮球一起收
  window.closeAssist();
  await sleep(200);
  window.closeResult();
  await sleep(300);
  out.关文章_悬浮球隐藏 = !fab().classList.contains("show");
  out.关文章_抽屉关闭 = !drawer().classList.contains("show");
  if (!out.关文章_悬浮球隐藏) fails.push("关掉文章后悬浮球应隐藏");
  if (!out.关文章_抽屉关闭) fails.push("关掉文章后抽屉应关闭");

  // ---------- 12) 设置里有「阅读助手」页签
  await window.openSettings("assist");
  await sleep(700);
  out.设置_助手页可见 = $("pane-assist").style.display !== "none";
  out.设置_搜索服务 = doc.querySelector("#as_providers").textContent.replace(/\s+/g, " ").trim().slice(0, 80);
  out.设置_有联网开关 = !!$("as_web") && !!$("as_enabled");
  out.设置_服务状态 = $("as_state").textContent.trim();
  if (!out.设置_助手页可见) fails.push("设置里应有「阅读助手」页签");
  if (!out.设置_有联网开关) fails.push("设置里应有助手开关与联网默认值开关");
  if (!/bing|Bing|当前/.test(out.设置_服务状态 + out.设置_搜索服务)) {
    fails.push(`设置里应显示联网搜索服务的状态，实际「${out.设置_搜索服务}」`);
  }
  window.closeSettings();
  await sleep(200);

  report("阅读助手全部通过", out, fails);
})();
