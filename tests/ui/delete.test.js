const { JSDOM } = require("jsdom");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const BASE = process.env.PA_BASE || "http://127.0.0.1:8787";
const fs = require("fs");
const PATH = require("path").join(process.env.PA_OUTPUT_DIR || "/tmp/pa-out", "__前端删除测试");

(async () => {
  // 造一条真实记录（绝不碰用户的真实数据）
  fs.rmSync(PATH, { recursive: true, force: true });
  fs.mkdirSync(PATH, { recursive: true });
  fs.writeFileSync(PATH + "/meta.json", JSON.stringify({ title: "前端删除测试", podcast: "测试" }));
  fs.writeFileSync(PATH + "/article.md", "# 测试文章\n\n内容");
  fs.writeFileSync(PATH + "/audio.m4a", Buffer.alloc(220 * 1024));
  fs.writeFileSync(PATH + "/transcript.txt", "文字稿");

  const html = await (await fetch(BASE + "/")).text();
  const out = {}, fails = [];
  const dom = new JSDOM(html, {
    url: BASE, runScripts: "dangerously", pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(u.startsWith("http") ? u : BASE + u, o);
      w.scrollTo = () => {}; w.Element.prototype.scrollIntoView = function () {};
    },
  });
  const { window } = dom, doc = window.document;
  const $ = (id) => doc.getElementById(id);
  await sleep(6000);

  const card = [...doc.querySelectorAll("#libgrid .ep")].find((c) => c.dataset.dir === "__前端删除测试");
  out.测试卡片存在 = !!card;
  out.卡片有删除按钮 = !!card.querySelector(".kill");
  if (!card || !out.卡片有删除按钮) { console.log("测试卡片未出现"); process.exit(1); }

  // 1) 打开删除弹窗
  card.querySelector(".kill").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(300);
  out.弹窗标题 = $("modaltitle").textContent;
  out.选项 = [...doc.querySelectorAll('.opt .ot')].map((e) => e.textContent);
  out.选项说明 = [...doc.querySelectorAll('.opt .od')].map((e) => e.textContent.trim());
  out.默认选中 = doc.querySelector('input[name="delscope"]:checked').value;
  out.按钮为危险样式 = /danger/.test($("modalok").className);
  if (out.选项.length !== 2) fails.push("删除选项数不对");
  if (out.默认选中 !== "all") fails.push("默认未选中『删除整条记录』");
  if (!/MB|KB/.test(out.选项说明[0])) fails.push("选项未显示可释放空间");

  // 2) 选择「只删文章」并确认
  doc.querySelector('input[name="delscope"][value="article"]').checked = true;
  $("modalok").click();
  await sleep(1500);
  out.只删文章_文章已删 = !fs.existsSync(PATH + "/article.md");
  out.只删文章_音频保留 = fs.existsSync(PATH + "/audio.m4a");
  out.只删文章_提示 = $("toast").textContent.trim().slice(0, 30);
  if (!out.只删文章_文章已删 || !out.只删文章_音频保留) fails.push("只删文章行为不对");

  // 3) 再删整条记录
  await sleep(800);
  const card2 = [...doc.querySelectorAll("#libgrid .ep")].find((c) => c.dataset.dir === "__前端删除测试");
  card2.querySelector(".kill").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(300);
  out.无文章时_该选项被禁用 = doc.querySelector('input[name="delscope"][value="article"]').disabled;
  if (!out.无文章时_该选项被禁用) fails.push("没有文章时『只删文章』应禁用");
  $("modalok").click();
  await sleep(1800);
  out.整条删除_目录已移除 = !fs.existsSync(PATH);
  out.整条删除_提示 = $("toast").textContent.trim().slice(0, 34);
  out.列表已刷新 = ![...doc.querySelectorAll("#libgrid .ep")].some((c) => c.dataset.dir === "__前端删除测试");
  if (!out.整条删除_目录已移除) fails.push("整条记录未删除");
  if (!out.列表已刷新) fails.push("列表未刷新");

  fs.rmSync(PATH, { recursive: true, force: true });
  console.log(JSON.stringify(out, null, 1));
  console.log(fails.length ? "✕ 失败: " + fails.join(" / ") : "✓ 历史文章删除功能全部通过");
  process.exit(fails.length ? 1 : 0);
})();
