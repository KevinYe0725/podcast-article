/**
 * 导出与用量显示的前端测试。
 *
 * 导出走真实接口（fetch 真的去拿文件），但「点击下载」在 jsdom 里没有实现，
 * 所以把 HTMLAnchorElement.click 打桩成记录 href —— 这样既验证了 URL 正确，
 * 又不会触发 jsdom 的导航告警。
 */
const { boot, report, sleep, until } = require("./harness");
const fs = require("fs");
const path = require("path");

const OUT = process.env.PA_OUTPUT_DIR;
const DIR = "__UI测试单集";        // seed.js 造的那一集

(async () => {
  if (!OUT) { console.log("缺少 PA_OUTPUT_DIR，必须通过 run.sh 运行"); process.exit(2); }

  // 给这一集补一份用量记录：flash 空闲档 100 万未命中输入 = 1.0 元，50 万输出 = 2.0 元 → 3.0 元
  const usageFile = path.join(OUT, DIR, "usage.json");
  fs.writeFileSync(usageFile, JSON.stringify({
    model: "deepseek-flash", calls: 8,
    hit_tokens: 200000, miss_tokens: 1000000, out_tokens: 500000,
    peak: { hit: 0, miss: 0, out: 0 },
    off: { hit: 200000, miss: 1000000, out: 500000 },
    elapsed_s: 180.0,
  }));

  const out = {}, fails = [];
  const clicked = [];
  const { window, doc, $, BASE } = await boot({
    beforeParse(w) {
      w.HTMLAnchorElement.prototype.click = function () { clicked.push(this.getAttribute("href")); };
    },
  });

  // ---------- 1) 导出 URL 的形态
  await window.openEpisode(encodeURIComponent(DIR));
  await sleep(1000);
  out.打开的文章 = $("article").textContent.trim().slice(0, 12);
  if (!out.打开的文章) fails.push("没能打开测试文章，后面的导出断言没意义");

  const enc = encodeURIComponent(DIR);
  out.URL_md = window.exportUrl("md");
  out.URL_html = window.exportUrl("html");
  out.URL_txt = window.exportUrl("txt");
  out.URL_整库 = window.bundleUrl();
  if (out.URL_md !== `/api/export/${enc}?fmt=md`) fails.push(`md 导出地址不对：${out.URL_md}`);
  if (out.URL_html !== `/api/export/${enc}?fmt=html`) fails.push(`html 导出地址不对：${out.URL_html}`);
  if (!out.URL_整库.startsWith("/api/export?")) fails.push(`整库地址不对：${out.URL_整库}`);

  // ---------- 2) 三种格式的真实响应
  const grab = async (fmt) => {
    const resp = await fetch(BASE + window.exportUrl(fmt));
    return { status: resp.status, cd: resp.headers.get("content-disposition") || "", text: await resp.text() };
  };
  const md = await grab("md"), html = await grab("html"), txt = await grab("txt");
  out.md_状态 = md.status;
  out.md_附件头 = md.cd.slice(0, 40);
  out.md_有元信息块 = md.text.startsWith("---") && md.text.includes("title:");
  out.md_含正文 = md.text.includes("缝在鱼身上");
  out.html_状态 = html.status;
  out.html_是完整文档 = html.text.includes("<!DOCTYPE html>") && html.text.includes("<style>");
  out.html_无外部引用 = !html.text.includes("/static/");
  out.txt_状态 = txt.status;
  out.txt_内容 = txt.text.trim().slice(0, 24);

  for (const [k, v] of Object.entries({ md: md.status, html: html.status, txt: txt.status })) {
    if (v !== 200) fails.push(`${k} 导出应返回 200，实际 ${v}`);
  }
  if (!md.cd.includes("UTF-8''")) fails.push(`Markdown 的下载文件名应按 RFC 5987 编码，实际「${md.cd}」`);
  if (!out.md_有元信息块) fails.push("Markdown 应以 YAML front matter 开头并含 title");
  if (!out.html_是完整文档) fails.push("HTML 导出应是带 <style> 的完整文档");
  if (!out.html_无外部引用) fails.push("HTML 导出必须是自包含的，不该引用 /static/");
  if (!out.txt_内容.includes("00:00:01")) fails.push(`txt 导出应是带时间戳的文字稿，实际「${out.txt_内容}」`);

  // ---------- 3) 菜单交互 + 下载被触发
  $("expbtn").click();
  await sleep(200);
  out.菜单已展开 = !$("exportmenu").hidden;
  out.菜单项 = [...doc.querySelectorAll("#exportmenu .mi")].map((m) => m.textContent.trim());
  if (!out.菜单已展开) fails.push("点「导出 ▾」应展开菜单");
  if (out.菜单项.length < 3) fails.push(`导出菜单项太少：${JSON.stringify(out.菜单项)}`);

  clicked.length = 0;
  [...doc.querySelectorAll("#exportmenu .mi")].find((m) => m.dataset.fmt === "md").click();
  await sleep(300);
  out.点Markdown_下载地址 = [...clicked];   // 必须拷贝：clicked 后面还会被 zipbtn 改写
  out.点Markdown_菜单已收起 = $("exportmenu").hidden;
  out.点Markdown_提示 = $("toast").textContent.trim().slice(0, 16);
  if (clicked[0] !== out.URL_md) fails.push(`点 Markdown 应下载 ${out.URL_md}，实际 ${clicked[0]}`);
  if (!out.点Markdown_菜单已收起) fails.push("点完菜单项应收起菜单");
  if (!$("toast").textContent.trim()) fails.push("点导出后应有提示");

  // Esc 也能收起菜单（分层退出）
  $("expbtn").click();
  await sleep(150);
  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await sleep(150);
  out.Esc收起菜单 = $("exportmenu").hidden;
  if (!out.Esc收起菜单) fails.push("Esc 应收起导出菜单");

  // ---------- 4) 整库打包：弹窗确认 → 触发下载
  clicked.length = 0;
  $("zipbtn").click();
  await sleep(300);
  out.打包_弹窗标题 = $("modaltitle").textContent;
  out.打包_有文字稿开关 = !!$("ziptrans");
  if (!/打包/.test(out.打包_弹窗标题)) fails.push(`「导出全部」应弹出打包确认框，实际标题「${out.打包_弹窗标题}」`);
  $("modalok").click();
  await sleep(500);
  out.打包_下载地址 = [...clicked];
  if (!(clicked[0] || "").startsWith("/api/export?")) fails.push(`打包下载地址不对：${clicked[0]}`);
  if (!/transcript=[01]/.test(clicked[0] || "")) fails.push(`打包地址应带 transcript 参数，实际 ${clicked[0]}`);

  // 真的有文章可打包（不能是 400）
  const zipResp = await fetch(BASE + clicked[0]);
  out.打包_真实状态 = zipResp.status;
  out.打包_类型 = zipResp.headers.get("content-type") || "";
  out.打包_集数头 = zipResp.headers.get("x-episodes");
  if (zipResp.status !== 200) fails.push(`整库打包应返回 200，实际 ${zipResp.status}`);

  // ---------- 5) 每篇的花费显示
  await window.loadLibrary();
  await sleep(400);
  out.工具条_费用胶囊 = $("costpill").textContent.trim();
  out.工具条_费用提示 = ($("costpill").title || "").split("\n")[0];
  if (!out.工具条_费用胶囊.includes("元")) fails.push(`工具条应显示费用，实际「${out.工具条_费用胶囊}」`);
  // 100 万未命中输入 × 1.125 元/M + 50 万输出 × 4.5 元/M = 3.375，界面显示 3.38 元。
  if (!/3\.38\s*元/.test(out.工具条_费用胶囊)) {
    fails.push(`费用应为 3.38 元（1.125 输入 + 2.25 输出），实际「${out.工具条_费用胶囊}」`);
  }
  if (!out.工具条_费用提示.includes("缓存命中")) fails.push("费用提示里应说明缓存命中量");

  const card = [...doc.querySelectorAll("#libgrid .ep")].find((c) => c.dataset.dir === DIR);
  out.卡片文案 = card ? card.textContent.replace(/\s+/g, " ").trim() : "(没找到卡片)";
  if (!card || !card.textContent.includes("元")) fails.push("文章卡片上也应显示这一集的累计花费");

  // ---------- 6) 顶栏累计 + 设置页用量面板
  out.顶栏用量 = $("usagepill").textContent.trim();
  if (!out.顶栏用量.includes("累计") || !out.顶栏用量.includes("元")) {
    fails.push(`顶栏应显示累计用量与费用，实际「${out.顶栏用量}」`);
  }

  await window.openSettings("storage");
  await until(() => ($("usagebox").textContent || "").includes("累计费用"), 8000, 150);
  out.用量面板 = $("usagebox").textContent.replace(/\s+/g, " ").trim();
  out.用量状态标 = $("usage_state").textContent.trim();
  out.存储行 = $("storagerows").textContent.replace(/\s+/g, " ").trim().slice(0, 60);
  if (!out.用量面板.includes("累计费用（估算）")) fails.push("设置页用量面板缺少累计费用");
  if (!out.用量面板.includes("缓存命中")) fails.push("设置页用量面板缺少缓存命中统计");
  if (!/篇有记录/.test(out.用量状态标)) fails.push(`用量状态标文案不对：「${out.用量状态标}」`);

  fs.rmSync(usageFile, { force: true });   // 收尾：别把用量留给别的测试
  report("导出与用量全部通过", out, fails);
})();
