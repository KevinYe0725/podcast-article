const { JSDOM } = require("jsdom");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const BASE = process.env.PA_BASE || "http://127.0.0.1:8787";
(async () => {
  const html = await (await fetch(BASE + "/")).text();
  const scrolled = [];
  const dom = new JSDOM(html, {
    url: BASE, runScripts: "dangerously", pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(u.startsWith("http") ? u : BASE + u, o);
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () { scrolled.push(this.id || "(anon)"); };
    },
  });
  const { window } = dom, doc = window.document;
  const $ = (id) => doc.getElementById(id);
  const esc = () => doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  const out = {}; const fails = [];

  await sleep(6000);
  const card = doc.querySelector("#libgrid .ep");
  if (!card) { console.log("没有历史卡片，无法测试"); process.exit(1); }

  // 打开一篇文章
  await window.openEpisode(card.getAttribute("onclick").match(/'([^']+)'/)[1]);
  await sleep(1200);
  out.打开后_显示 = $("result").classList.contains("show");
  out.打开后_来源标记 = $("result").dataset.fromLib;
  out.关闭按钮存在 = !!doc.querySelector(".rhead .iconbtn.small");
  out.正文长度 = $("article").textContent.trim().length;
  if (!out.打开后_显示) fails.push("打开文章后未显示结果区");
  if (!out.关闭按钮存在) fails.push("结果区没有关闭按钮");

  // 点关闭
  scrolled.length = 0;
  window.closeResult();
  await sleep(300);
  out.关闭后_隐藏 = !$("result").classList.contains("show");
  out.关闭后_正文已清空 = $("article").innerHTML === "";
  out.关闭后_滚动到 = scrolled.join(",") || "(无)";
  if (!out.关闭后_隐藏) fails.push("点关闭后结果区仍显示");
  if (out.关闭后_正文已清空 !== true) fails.push("关闭后正文未清空");
  if (!scrolled.includes("lib")) fails.push("关闭后没有回到历史库");

  // Esc 关闭
  await window.openEpisode(card.getAttribute("onclick").match(/'([^']+)'/)[1]);
  await sleep(1000);
  esc(); await sleep(200);
  out.Esc关闭 = !$("result").classList.contains("show");
  if (!out.Esc关闭) fails.push("Esc 无法关闭文章");

  // Esc 分层：文字稿打开时先收文字稿，文章仍显示
  await window.openEpisode(card.getAttribute("onclick").match(/'([^']+)'/)[1]);
  await sleep(1000);
  await window.toggleTranscript();
  await sleep(300);
  out.文字稿已开 = $("result").classList.contains("withTranscript");
  esc(); await sleep(200);
  out.Esc一次_文字稿收 = !$("result").classList.contains("withTranscript");
  out.Esc一次_文章还在 = $("result").classList.contains("show");
  esc(); await sleep(200);
  out.Esc两次_文章关 = !$("result").classList.contains("show");
  if (!out.Esc一次_文字稿收 || !out.Esc一次_文章还在) fails.push("Esc 分层行为不对");
  if (!out.Esc两次_文章关) fails.push("第二次 Esc 没有关掉文章");

  console.log(JSON.stringify(out, null, 1));
  console.log(fails.length ? "✕ 失败: " + fails.join(" / ") : "✓ 关闭交互全部通过");
  process.exit(fails.length ? 1 : 0);
})();
