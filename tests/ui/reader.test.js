/**
 * 阅读页（独立整屏页面）。
 *
 * 「打开文章 = 换一个页面」这件事的骨架，断言的是结构与路由，不是像素：
 *   · #result 在 DOM 上就不在 #main 里（不是主页面中部的一段，而是一层独立页面）
 *   · 打开后地址栏变成 #/a/<目录名>，顶栏元信息与正文都填好，body 挂上 reading
 *   · 点返回（或 Esc）关掉，地址栏回到 #/，主页面还在原处
 *   · 地址栏被改回 #/ 时界面跟着收起（等价于浏览器后退）
 *   · 带 hash 直接打开（刷新 / 收藏 / 分享链接）能还原同一篇
 */
const { boot, until, report, BASE } = require("./harness");

(async () => {
  const { window, doc, $ } = await boot();
  const out = {}, fails = [];

  const card = doc.querySelector("#libgrid .ep");
  if (!card) { console.log("没有历史卡片，无法测试"); process.exit(1); }
  const dirEnc = card.getAttribute("onclick").match(/'([^']+)'/)[1];
  const want = "#/a/" + dirEnc;

  out.结构_不在主页面内 = !$("main").contains($("result"));
  out.结构_有独立顶栏 = !!doc.querySelector("#result .rdtop");
  out.结构_有阅读进度线 = !!$("rdprogbar");
  out.初始_未显示 = !$("result").classList.contains("show");
  out.初始_地址栏 = window.location.hash || "(空)";
  if (!out.结构_不在主页面内) fails.push("#result 还嵌在 #main 里，应该独立成一层");
  if (!out.结构_有独立顶栏) fails.push("阅读页没有自己的顶栏");
  if (!out.结构_有阅读进度线) fails.push("阅读页没有阅读进度线");
  if (!out.初始_未显示) fails.push("没打开文章时阅读页就显示着");

  // 样式：整屏固定层 + 自己滚动 + 顶栏吸附 + 定的栏宽（jsdom 能把样式表算出来）
  const cs = (el, prop) => window.getComputedStyle(el).getPropertyValue(prop);
  out.样式_position = cs($("result"), "position");
  out.样式_overflow_y = cs($("result"), "overflow-y");
  out.样式_z_index = cs($("result"), "z-index");
  out.样式_顶栏 = cs(doc.querySelector(".rdtop"), "position");
  out.样式_正文容器栏宽 = cs(doc.querySelector(".rdbody"), "max-width");
  out.样式_正文字号 = cs($("article"), "font-size");
  if (out.样式_position !== "fixed") fails.push("阅读页不是固定整屏层（position=" + out.样式_position + "）");
  if (out.样式_overflow_y !== "auto") fails.push("阅读页没有自己滚动（overflow-y=" + out.样式_overflow_y + "）");
  if (out.样式_顶栏 !== "sticky") fails.push("阅读页顶栏没有吸附在顶部");
  if (out.样式_正文容器栏宽 !== "728px") fails.push("正文栏宽上限不对：" + out.样式_正文容器栏宽);

  // 从卡片打开
  await window.openEpisode(dirEnc);
  await until(() => $("result").classList.contains("show"));
  out.打开后_地址栏 = window.location.hash;
  out.打开后_正文长度 = $("article").textContent.trim().length;
  out.打开后_顶栏有标题 = $("rmeta").textContent.trim().length > 0;
  out.打开后_锁住主页面滚动 = doc.body.classList.contains("reading");
  if (out.打开后_地址栏 !== want) fails.push(`地址栏没变成 ${want}（实际 ${out.打开后_地址栏}）`);
  if (!out.打开后_正文长度) fails.push("打开后正文是空的");
  if (!out.打开后_顶栏有标题) fails.push("阅读页顶栏没有节目/标题");
  if (!out.打开后_锁住主页面滚动) fails.push("阅读页打开时没有锁住底下的主页面滚动");

  // 返回按钮
  const back = doc.querySelector(".rdtop .rdback");
  back.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await until(() => !$("result").classList.contains("show"));
  out.返回后_地址栏 = window.location.hash;
  out.返回后_主页面可用 = $("main").style.display !== "none" && !!$("url");
  out.返回后_解锁滚动 = !doc.body.classList.contains("reading");
  if ($("result").classList.contains("show")) fails.push("点返回没有关掉阅读页");
  if (out.返回后_地址栏 !== "#/") fails.push(`返回后地址栏没回到 #/（实际 ${out.返回后_地址栏}）`);
  if (!out.返回后_主页面可用) fails.push("返回后主页面没露出来");
  if (!out.返回后_解锁滚动) fails.push("返回后没有解锁主页面滚动");

  // 地址栏被改回 #/（浏览器后退）→ 界面跟着收起
  await window.openEpisode(dirEnc);
  await until(() => $("result").classList.contains("show"));
  window.location.hash = "#/";
  out.后退_界面跟着收起 = await until(() => !$("result").classList.contains("show"), 3000);
  if (!out.后退_界面跟着收起) fails.push("地址栏回到 #/ 后阅读页没收起（hashchange 没接上）");

  // 深链接：直接带着 #/a/<目录> 打开页面（刷新 / 收藏 / 分享）
  const deep = await boot({ url: BASE + "/" + want });
  const deepOk = await until(() => deep.$("result").classList.contains("show"), 8000);
  out.深链接_自动打开 = deepOk;
  out.深链接_地址栏 = deep.window.location.hash;
  out.深链接_正文长度 = deep.$("article").textContent.trim().length;
  out.深链接_顶栏标题 = deep.$("rmeta").textContent.replace(/\s+/g, " ").trim().slice(0, 40);
  if (!deepOk) fails.push("带 hash 打开时没有还原阅读页");
  if (!out.深链接_正文长度) fails.push("深链接打开后正文是空的");
  if (out.深链接_地址栏 !== want) fails.push(`深链接打开后地址栏被改写（实际 ${out.深链接_地址栏}）`);
  deep.window.close();

  report("阅读页（独立整屏）通过", out, fails);
})();
