/**
 * 页面之间的导航。
 *
 * 回归背景：设置页是把整个 #main 藏起来、再把 #settings 放出来的，而侧边栏的分类 /
 * 队列 / 订阅 / 搜索只切换 #main **内部**的子视图 —— 用户在设置页点侧边栏任何入口都
 * 毫无反应，等于被关在设置页里。所有进入主界面的入口都必须先把 #main 放出来。
 */
const { boot, report, sleep, until } = require("./harness");

(async () => {
  const out = {}, fails = [];
  const { window, doc, $, click, esc } = await boot();

  const mainVisible = () => $("main").style.display !== "none";
  const settingsVisible = () => $("settings").style.display === "block";

  async function openSettings() {
    window.openSettings();
    await until(settingsVisible, 4000);
    await sleep(400);
  }
  const sidebarRow = (text) =>
    [...doc.querySelectorAll("#catnav .folder, #navqueue, #navfeeds")]
      .find((f) => f.textContent.includes(text));

  // ---------- 1) 打开设置
  await openSettings();
  out.打开设置_设置可见 = settingsVisible();
  out.打开设置_主界面隐藏 = !mainVisible();
  out.打开设置_标题 = doc.querySelector("#settings h1").textContent.trim();
  if (!out.打开设置_设置可见) fails.push("openSettings() 之后设置页没显示");
  if (!out.打开设置_主界面隐藏) fails.push("打开设置时应把主界面收起来");

  // ---------- 2) 设置页里点「全部文章」
  click(sidebarRow("全部文章"));
  await sleep(400);
  out.点全部文章_主界面可见 = mainVisible();
  out.点全部文章_设置已收 = !settingsVisible();
  out.点全部文章_文章库可见 = $("lib").style.display !== "none";
  out.点全部文章_标题 = $("libtitle").textContent.trim();
  if (!out.点全部文章_主界面可见) fails.push("在设置页点「全部文章」没能回到主界面（#main 仍是 none）");
  if (!out.点全部文章_设置已收) fails.push("回到主界面后设置页应该收起来");
  if (!out.点全部文章_文章库可见) fails.push("回到主界面后文章库应可见");

  // ---------- 3) 设置页里点「批量队列」
  await openSettings();
  click($("navqueue"));
  await sleep(400);
  out.点队列_主界面可见 = mainVisible();
  out.点队列_队列视图可见 = $("queueview").style.display !== "none";
  out.点队列_文章库已隐藏 = $("lib").style.display === "none";
  if (!out.点队列_主界面可见 || !out.点队列_队列视图可见) fails.push("在设置页点「批量队列」没能进入队列页");

  // ---------- 4) 设置页里点「订阅」
  await openSettings();
  click($("navfeeds"));
  await sleep(500);
  out.点订阅_主界面可见 = mainVisible();
  out.点订阅_订阅视图可见 = $("feedsview").style.display !== "none";
  if (!out.点订阅_主界面可见 || !out.点订阅_订阅视图可见) fails.push("在设置页点「订阅」没能进入订阅页");

  // ---------- 5) 设置页里用搜索框
  await openSettings();
  $("q").value = "测试";
  $("q").dispatchEvent(new window.Event("input", { bubbles: true }));
  await until(() => $("libtitle").textContent.startsWith("搜索"), 4000);
  await sleep(200);
  out.点搜索_主界面可见 = mainVisible();
  out.点搜索_标题 = $("libtitle").textContent.trim();
  if (!out.点搜索_主界面可见) fails.push("在设置页用搜索框没能回到主界面");
  if (!out.点搜索_标题.startsWith("搜索")) fails.push(`搜索后主区标题应为「搜索…」，实际「${out.点搜索_标题}」`);

  // ---------- 6) 设置页里点品牌名 → 回到文章库
  await openSettings();
  click(doc.querySelector(".appbrand"));
  await sleep(500);
  out.点品牌_主界面可见 = mainVisible();
  out.点品牌_标题 = $("libtitle").textContent.trim();
  if (!out.点品牌_主界面可见) fails.push("在设置页点品牌名没能回到主界面");
  if (out.点品牌_标题 !== "全部文章") fails.push(`点品牌名应回到「全部文章」，实际「${out.点品牌_标题}」`);

  // ---------- 7) Esc 从设置页退出仍然有效
  await openSettings();
  esc();
  await sleep(300);
  out.Esc_主界面可见 = mainVisible();
  out.Esc_设置已收 = !settingsVisible();
  if (!out.Esc_主界面可见 || !out.Esc_设置已收) fails.push("Esc 应能从设置页退回主界面");

  // ---------- 8) 返回按钮仍然有效
  await openSettings();
  click([...doc.querySelectorAll("#settings .btn")].find((b) => b.textContent.includes("返回")));
  await sleep(400);
  out.返回按钮_主界面可见 = mainVisible();
  if (!out.返回按钮_主界面可见) fails.push("设置页的「← 返回」按钮没能回到主界面");

  report("页面导航全部通过", out, fails);
})();
