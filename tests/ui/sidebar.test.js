const { boot, report, sleep } = require("./harness");
const BASE = process.env.PA_BASE || "http://127.0.0.1:8787";
const fs = require("fs");
const DIR = "__侧边栏测试单集";   // 用自己的目录，别动 seed.js 造的共享单集
const PATH = require("path").join(process.env.PA_OUTPUT_DIR || "/tmp/pa-out", DIR);

(async () => {
  fs.rmSync(PATH, { recursive: true, force: true });
  fs.mkdirSync(PATH, { recursive: true });
  fs.writeFileSync(PATH + "/meta.json", JSON.stringify({ title: "侧边栏测试", podcast: "测试" }));
  fs.writeFileSync(PATH + "/article.md", "# t\n\n正文");
  // 另建两个分类，让侧边栏有多行（并记录 id，便于只清理自己建的）
  const created = [];
  for (const n of ["AI 技术", "读书笔记"]) {
    const r = await (await fetch(BASE + "/api/categories", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: n }) })).json();
    if (r.category) created.push(r.category.id);
  }
  const cats = (await (await fetch(BASE + "/api/categories")).json()).categories;
  const aiCat = cats.find((c) => c.name === "AI 技术");

  const out = {}, fails = [];
  const { window, doc, $ } = await boot({
    beforeParse(w) {
      w.__under = null;
      w.document.elementFromPoint = () => w.__under;
    },
  });

  out.侧边栏行 = [...doc.querySelectorAll("#catnav .folder")].map((f) => f.textContent.replace(/\s+/g, " ").trim());
  out.有文件夹图标 = doc.querySelectorAll("#catnav .folder .ficon").length >= 4;
  out.分组分隔线 = !!doc.querySelector("#catnav .sidegap");
  out.主区标题 = $("libtitle").textContent;
  out.主区计数 = $("libcount").textContent;
  if (out.侧边栏行.length < 4) fails.push("侧边栏行数不足: " + out.侧边栏行.length);
  if (!/AI 技术/.test(out.侧边栏行.join())) fails.push("分类未出现在侧边栏");

  // 点文件夹筛选
  const aiRow = [...doc.querySelectorAll("#catnav .folder")].find((f) => f.textContent.includes("AI 技术"));
  aiRow.click(); await sleep(400);
  out.筛选后_标题 = $("libtitle").textContent;
  out.筛选后_高亮行 = (doc.querySelector("#catnav .folder.on") || { textContent: "" }).textContent.trim();
  out.筛选后_卡片数 = doc.querySelectorAll("#libgrid .ep").length;
  if (out.筛选后_标题 !== "AI 技术") fails.push("主区标题未跟随文件夹");
  if (!out.筛选后_高亮行.includes("AI 技术")) fails.push("选中行未高亮");

  // 回到全部
  [...doc.querySelectorAll("#catnav .folder")].find((f) => f.textContent.includes("全部文章")).click();
  await sleep(400);
  out.回到全部_卡片数 = doc.querySelectorAll("#libgrid .ep").length;
  if (out.回到全部_卡片数 < 1) fails.push("回到全部后没有卡片");

  // 拖拽到侧边栏文件夹
  const card = [...doc.querySelectorAll("#libgrid .ep")].find((c) => c.dataset.dir === DIR);
  const pd = (t, x, y, el) => (el || doc).dispatchEvent(new window.MouseEvent(t, { bubbles: true, clientX: x, clientY: y, button: 0 }));
  window.__under = card;
  pd("pointerdown", 100, 100, card);
  window.__under = aiRow;
  pd("pointermove", 150, 130);
  await sleep(150);
  out.拖拽_高亮侧边栏 = aiRow.classList.contains("drop");
  if (!out.拖拽_高亮侧边栏) fails.push("拖到侧边栏文件夹未高亮");
  pd("pointerup", 150, 130);
  await sleep(1500);
  const lib1 = await (await fetch(BASE + "/api/library")).json();
  out.拖拽后归属 = lib1.assignments[DIR] === aiCat.id ? "AI 技术" : (lib1.assignments[DIR] || "未分类");
  if (out.拖拽后归属 !== "AI 技术") fails.push("拖到侧边栏未归类: " + out.拖拽后归属);

  // "全部文章" 不是投放目标
  const allRow = [...doc.querySelectorAll("#catnav .folder")].find((f) => f.textContent.includes("全部文章"));
  window.__under = card;
  pd("pointerdown", 100, 100, card);
  window.__under = allRow;
  pd("pointermove", 150, 130);
  out.全部_不高亮 = !allRow.classList.contains("drop");
  pd("pointerup", 150, 130);
  await sleep(800);
  const lib2 = await (await fetch(BASE + "/api/library")).json();
  out.全部_归属未变 = lib2.assignments[DIR] === aiCat.id;
  if (!out.全部_不高亮 || !out.全部_归属未变) fails.push("『全部文章』不应作为投放目标");

  // 清理：只删本次测试创建的分类，绝不碰用户已有分类
  fs.rmSync(PATH, { recursive: true, force: true });
  for (const c of (await (await fetch(BASE + "/api/categories")).json()).categories) {
    if (created.includes(c.id)) await fetch(BASE + "/api/categories/" + c.id, { method: "DELETE" });
  }
  await sleep(300);

  // ---- 卡片封面 ----
  // seed.js 造的那集（__UI测试单集）带 cover.png，本测试自己造的那集没有 —— 正好对照
  window.setCat("all");
  await window.loadLibrary();
  await sleep(500);
  const covered = [...doc.querySelectorAll("#libgrid .ep")].find((c) => c.dataset.dir === "__UI测试单集");
  out.封面_卡片有图 = !!covered.querySelector(".cover img");
  out.封面_地址 = covered.querySelector(".cover img") ? covered.querySelector(".cover img").getAttribute("src") : "";
  out.封面_懒加载 = covered.querySelector(".cover img") ? covered.querySelector(".cover img").getAttribute("loading") : "";
  out.封面_卡片带hascover类 = covered.classList.contains("hascover");
  // seed 出来的那集 source 为空 → 按播客方形处理
  out.封面_方形容器 = covered.querySelector(".cover") ? covered.querySelector(".cover").classList.contains("sq") : null;
  if (!out.封面_卡片有图) fails.push("有封面的卡片应当渲染 .cover img");
  if (out.封面_地址 !== "/api/cover/" + encodeURIComponent("__UI测试单集")) {
    fails.push(`封面地址不对：${out.封面_地址}`);
  }
  if (out.封面_懒加载 !== "lazy") fails.push("封面图应当 lazy 加载（列表里一次渲染十几张）");
  if (!out.封面_卡片带hascover类) fails.push("有封面的卡片应带 hascover 类（标题不再给右上角留白）");
  // 无封面的卡片不能因此崩掉，也不该渲染空容器
  const bare = [...doc.querySelectorAll("#libgrid .ep")].find((c) => c.dataset.dir === DIR);
  if (bare) {
    out.封面_无封面的卡片 = bare.querySelector(".cover") ? "有容器（不该）" : "没有容器 ✓";
    if (bare.querySelector(".cover")) fails.push("没有封面时不该渲染空的封面容器");
  }
  // 封面接口真的能取到图
  const cresp = await fetch(`${BASE}/api/cover/__UI测试单集`);
  out.封面_接口状态 = cresp.status;
  out.封面_接口类型 = cresp.headers.get("content-type") || "";
  if (cresp.status !== 200) fails.push(`/api/cover 应返回 200，实际 ${cresp.status}`);
  if (!/^image\//.test(out.封面_接口类型)) fails.push(`封面 MIME 不对：${out.封面_接口类型}`);

  report("侧边栏文件夹全部通过", out, fails);
})();
