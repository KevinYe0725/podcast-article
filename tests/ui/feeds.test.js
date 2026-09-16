/**
 * 订阅管理的前端交互测试。
 *
 * 不联网：订阅数据直接写进 PA_FEEDS_FILE（临时目录），只测「渲染 / 切换自动生成 /
 * 取消订阅」这些不发起抓取的路径。真正的抓取在 pytest 里用假 _fetch 测。
 */
const { boot, report, sleep, until } = require("./harness");
const fs = require("fs");

const FEEDS_FILE = process.env.PA_FEEDS_FILE;

const now = () => Date.now() / 1000;
const mkFeed = (n, over = {}) => ({
  id: "f" + n,
  url: `http://127.0.0.1:9/feed-${n}.xml`,
  title: `测试节目 ${n}`,
  auto: true,
  backfill: 0,
  added_at: now() - 86400,
  last_checked: now() - 7200,
  last_error: "",
  seen: ["g1", "g2", "g3"],
  ...over,
});

const readFeeds = () => {
  try { return JSON.parse(fs.readFileSync(FEEDS_FILE, "utf8")); } catch (e) { return { feeds: [] }; }
};
const writeFeeds = (feeds) => fs.writeFileSync(FEEDS_FILE, JSON.stringify({ feeds }, null, 2));

(async () => {
  if (!FEEDS_FILE) { console.log("缺少 PA_FEEDS_FILE，必须通过 run.sh 运行"); process.exit(2); }

  writeFeeds([
    mkFeed(1),                                            // 自动生成 + 上次检查 2 小时前
    mkFeed(2, { auto: false, last_error: "抓取超时" }),      // 仅发现 + 有错误
  ]);

  const out = {}, fails = [];
  const { window, doc, $ } = await boot();

  const rows = () => [...doc.querySelectorAll("#feedlist .frow")];

  window.showView("feeds");
  await window.loadFeeds();
  await until(() => rows().length === 2, 6000, 150);

  out.订阅视图可见 = $("feedsview").style.display !== "none";
  out.文章库已隐藏 = $("lib").style.display === "none";
  out.侧边栏计数 = $("navfcount").textContent.trim();
  out.订阅行数 = rows().length;
  out.标题 = rows().map((r) => r.querySelector(".ftitle").textContent.replace(/\s+/g, " ").trim());
  out.链接 = rows().map((r) => r.querySelector(".furl").textContent.trim());
  out.元信息 = rows().map((r) => r.querySelector(".fmeta").textContent.trim());
  out.标签 = rows().map((r) => r.querySelector(".ftag").textContent.trim());
  out.错误文案 = rows().map((r) => {
    const e = r.querySelector(".ferr");
    return e ? e.textContent.trim() : "";
  });
  out.自动开关 = rows().map((r) => r.querySelector(".fswitch input").checked);

  if (!out.订阅视图可见 || !out.文章库已隐藏) fails.push("切到订阅视图时不应还显示文章库");
  if (out.订阅行数 !== 2) fails.push(`应渲染 2 条订阅，实际 ${out.订阅行数}`);
  if (out.侧边栏计数 !== "2") fails.push(`侧边栏订阅计数应为 2，实际「${out.侧边栏计数}」`);
  if (!out.标题[0].includes("测试节目 1")) fails.push(`第 1 条标题不对：${out.标题[0]}`);
  if (!out.链接[1].includes("feed-2.xml")) fails.push(`第 2 条链接不对：${out.链接[1]}`);
  if (!out.元信息[0].includes("已知 3 集")) fails.push(`元信息里应显示已知集数，实际「${out.元信息[0]}」`);
  if (!out.元信息[0].includes("小时前")) fails.push(`上次检查应显示相对时间（2 小时前），实际「${out.元信息[0]}」`);
  if (out.标签[0] !== "自动生成" || out.标签[1] !== "仅发现新单集") fails.push(`状态标签不对：${JSON.stringify(out.标签)}`);
  if (!out.错误文案[1].includes("抓取超时")) fails.push(`抓取失败的订阅应显示错误，实际「${out.错误文案[1]}」`);
  if (out.自动开关[0] !== true || out.自动开关[1] !== false) fails.push(`自动开关初值不对：${JSON.stringify(out.自动开关)}`);

  // ---------- 切换「自动生成」：写回服务端 + 标签跟着变
  const cb = rows()[0].querySelector(".fswitch input");
  cb.checked = false;
  cb.dispatchEvent(new window.Event("change", { bubbles: true }));
  await until(() => (readFeeds().feeds.find((f) => f.id === "f1") || {}).auto === false, 6000, 150);
  await sleep(400);
  out.切换后_服务端 = readFeeds().feeds.map((f) => ({ id: f.id, auto: f.auto }));
  out.切换后_标签 = rows().map((r) => r.querySelector(".ftag").textContent.trim());
  if (out.切换后_服务端[0].auto !== false) fails.push("取消勾选后应把 auto=false 写回服务端");
  if (out.切换后_标签[0] !== "仅发现新单集") fails.push(`取消勾选后标签应变，实际「${out.切换后_标签[0]}」`);

  // ---------- 取消订阅：同风格弹窗 → 确认 → 列表与服务端都少一条
  rows()[1].querySelector(".facts .btn").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(300);
  out.弹窗出现 = $("modal").classList.contains("show");
  out.弹窗标题 = $("modaltitle").textContent;
  out.弹窗按钮样式 = $("modalok").className;
  if (!out.弹窗出现) fails.push("点删除应弹出同风格模态框");
  if (!/danger/.test(out.弹窗按钮样式)) fails.push("取消订阅是破坏性操作，确认按钮应为危险样式");

  $("modalok").click();
  await until(() => rows().length === 1, 6000, 150);
  out.删除后_行数 = rows().length;
  out.删除后_服务端条数 = readFeeds().feeds.length;
  out.删除后_剩余标题 = rows().map((r) => r.querySelector(".ftitle").textContent.replace(/\s+/g, " ").trim());
  if (out.删除后_行数 !== 1) fails.push(`取消订阅后应剩 1 行，实际 ${out.删除后_行数}`);
  if (out.删除后_服务端条数 !== 1) fails.push(`取消订阅后 feeds.json 应剩 1 条，实际 ${out.删除后_服务端条数}`);

  // ---------- 空状态
  writeFeeds([]);
  await window.loadFeeds();
  await sleep(200);
  out.空订阅_行数 = rows().length;
  out.空订阅_提示可见 = $("empty-feeds").style.display !== "none";
  if (out.空订阅_提示可见 !== true) fails.push("没有订阅时应显示空状态提示");

  // ---------- 回到文章库视图
  window.showView("lib");
  await sleep(200);
  out.回文章库_可见 = $("lib").style.display !== "none";
  out.回文章库_订阅隐藏 = $("feedsview").style.display === "none";
  if (!out.回文章库_可见 || !out.回文章库_订阅隐藏) fails.push("切回文章库时视图没恢复");

  writeFeeds([]);
  report("订阅管理全部通过", out, fails);
})();
