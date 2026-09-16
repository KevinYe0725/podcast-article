/**
 * 阅读状态：侧边栏智能列表 / 卡片快捷标记 / 卡片状态菜单 / 文章工具条状态菜单 /
 * 打开自动转「在读」 / 拖到侧边栏改状态。
 *
 * 自造三集数据（只用 PA_OUTPUT_DIR 的临时目录）：
 *   甲：卡片上做「✓ 已读」「状态标签菜单」
 *   乙：工具条 #readbtn 与 #readmenu
 *   丙：打开自动转「在读」与拖拽改状态
 */
const fs = require("fs");
const path = require("path");
const { boot, until, report, sleep, BASE } = require("./harness");

const A = "__状态测试甲";
const B = "__状态测试乙";
const C = "__状态测试丙";
const OUT = process.env.PA_OUTPUT_DIR;
const files = {};

function checkEnv() {
  const missing = ["PA_OUTPUT_DIR", "PA_LIBRARY_FILE", "PA_QUEUE_FILE", "PA_FEEDS_FILE"]
    .filter((k) => !process.env[k]);
  if (missing.length) {
    console.error("✕ 缺少测试环境变量：" + missing.join("、")
      + "\n  请通过 tests/ui/run.sh 运行（它会把这些指到临时目录），否则可能碰真实数据。");
    process.exit(2);
  }
}

function writeEpisode(name, title) {
  const dir = path.join(OUT, name);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "meta.json"),
    JSON.stringify({ title, podcast: "状态台", url: "https://example.com/s-" + name }));
  fs.writeFileSync(path.join(dir, "article.md"), `# ${title}\n\n正文段落，用来打开文章。\n`);
  fs.writeFileSync(path.join(dir, "transcript.txt"), "[00:00:03] 一句文字稿\n");
  files[name] = dir;
}

const setStatus = (dir, status) => fetch(BASE + "/api/status", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ dir, status }),
}).then((r) => r.json());

const library = () => fetch(BASE + "/api/library").then((r) => r.json());

(async () => {
  checkEnv();
  const out = {}, fails = [];
  try {
    writeEpisode(A, "状态测试甲");
    writeEpisode(B, "状态测试乙");
    writeEpisode(C, "状态测试丙");

    const { window, doc, $ } = await boot({
      beforeParse(w) {
        w.__under = null;
        w.document.elementFromPoint = () => w.__under;
      },
    });

    const card = (dir) => doc.querySelector(`#libgrid .ep[data-dir="${dir}"]`);
    const statusRow = (s) => [...doc.querySelectorAll("#catnav .folder")].find((f) => f.dataset.drop === s && f.dataset.dropkind === "status");
    const click = (el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

    // 1) 侧边栏四行状态
    const rows = [...doc.querySelectorAll("#catnav .folder")].map((f) => f.textContent.replace(/\s+/g, " ").trim());
    out.侧边栏行 = rows;
    out.状态行圆点数 = doc.querySelectorAll("#catnav .folder .fdot").length;
    out.状态行 = ["unread", "reading", "read", "later"].map((s) => {
      const r = statusRow(s);
      return r ? r.querySelector(".fname").textContent.trim() : "(缺失)";
    });
    if (out.状态行圆点数 < 4) fails.push(`期望侧边栏 ≥4 个状态行圆点 .fdot，实际 ${out.状态行圆点数} 个`);
    if (!out.状态行.join("|").includes("未读")) fails.push(`侧边栏状态行里没有「未读」，实际 ${JSON.stringify(out.状态行)}`);
    if (!out.状态行.join("|").includes("稍后读")) fails.push(`侧边栏状态行里没有「稍后读」，实际 ${JSON.stringify(out.状态行)}`);

    // 2) 服务端标记为已读 → 卡片上应显示「已读」
    await setStatus(A, "read");
    await window.loadLibrary();
    await sleep(250);
    const cardA1 = card(A);
    out.接口标记后_卡片文案 = cardA1 ? cardA1.textContent.replace(/\s+/g, " ").trim().slice(0, 70) : "(卡片不存在)";
    if (!cardA1) fails.push(`标记后 #libgrid 里找不到「${A}」的卡片`);
    else if (!/已读/.test(cardA1.textContent)) fails.push(`期望卡片上出现「已读」，实际「${out.接口标记后_卡片文案}」`);

    // 3) 卡片右上角「✓ 已读」快捷按钮
    await setStatus(A, "");                       // 先退回未读
    await window.loadLibrary();
    await sleep(250);
    const acts = doc.querySelector(`#libgrid .ep[data-dir="${A}"] .cardacts`);
    out.卡片快捷按钮 = [...acts.querySelectorAll(".mini-act")].map((b) => b.textContent.trim());
    const quickRead = acts.querySelector(".mini-act");
    click(quickRead);
    const becameRead = await until(() => {
      const c = card(A);
      return c && /已读/.test(c.textContent);
    }, 4000);
    await sleep(150);
    out.点快捷已读_卡片文案 = card(A).textContent.replace(/\s+/g, " ").trim().slice(0, 70);
    out.点快捷已读_结果区是否打开 = $("result").classList.contains("show") ? "打开了（不应该）" : "没打开";
    out.点快捷已读_服务端状态 = (await library()).status[A] || "unread";
    if (!becameRead) fails.push(`点「✓ 已读」后卡片应变成已读，实际「${out.点快捷已读_卡片文案}」`);
    if ($("result").classList.contains("show")) fails.push("点卡片上的快捷按钮顺带打开了文章（#result 有 show）");
    if (out.点快捷已读_服务端状态 !== "read") fails.push(`点「✓ 已读」后服务端状态期望 read，实际 ${out.点快捷已读_服务端状态}`);

    // 4) 卡片状态标签 → .catmenu → 稍后读
    const label = doc.querySelector(`#libgrid .ep[data-dir="${A}"] .catlabel`);
    out.卡片状态标签 = label.textContent.replace(/\s+/g, " ").trim();
    click(label);
    await sleep(200);
    const menu = doc.querySelector(".catmenu");
    out.状态菜单出现 = !!menu;
    out.状态菜单项 = menu ? [...menu.querySelectorAll(".mi")].map((m) => m.textContent.replace(/\s+/g, " ").trim()) : [];
    if (!menu) fails.push("点卡片状态标签没有弹出 .catmenu");
    else {
      const later = [...menu.querySelectorAll(".mi")].find((m) => m.dataset.st === "later");
      if (!later) fails.push(`.catmenu 里没有「稍后读」项，实际 ${JSON.stringify(out.状态菜单项)}`);
      else {
        click(later);
        const okLater = await until(async () => (await library()).status[A] === "later", 4000);
        await sleep(200);
        out.选稍后读_服务端状态 = (await library()).status[A] || "unread";
        out.选稍后读_卡片文案 = card(A).textContent.replace(/\s+/g, " ").trim().slice(0, 70);
        out.选稍后读_菜单已收起 = !doc.querySelector(".catmenu");
        if (!okLater || out.选稍后读_服务端状态 !== "later") {
          fails.push(`在 .catmenu 里选「稍后读」后期望服务端状态 later，实际 ${out.选稍后读_服务端状态}`);
        }
        if (!/稍后读/.test(out.选稍后读_卡片文案)) fails.push(`选「稍后读」后卡片没显示「稍后读」，实际「${out.选稍后读_卡片文案}」`);
        if (!out.选稍后读_菜单已收起) fails.push("选完后 .catmenu 没有收起");
      }
    }

    // 5) 文章工具条 #readbtn 与 #readmenu
    await setStatus(B, "later");
    await window.loadLibrary();
    await sleep(250);
    await window.openEpisode(encodeURIComponent(B));
    await sleep(900);
    out.打开乙_工具条按钮 = $("readbtn").textContent.trim();
    out.打开乙_结果区标题 = $("rmeta").textContent.replace(/\s+/g, " ").trim();
    if (!out.打开乙_工具条按钮.includes("稍后读")) {
      fails.push(`打开状态为「稍后读」的文章后 #readbtn 期望含「稍后读」，实际「${out.打开乙_工具条按钮}」`);
    }
    click($("readbtn"));
    await sleep(150);
    out.工具条菜单可见 = $("readmenu").hidden === false;
    out.工具条菜单项 = [...doc.querySelectorAll("#readmenu .mi")].map((m) => m.textContent.replace(/\s+/g, " ").trim());
    if (!out.工具条菜单可见) fails.push("点 #readbtn 后 #readmenu 仍处于 hidden");
    const markRead = doc.querySelector('#readmenu .mi[data-st="read"]');
    if (!markRead) fails.push("#readmenu 里没有「标为已读」项");
    else {
      click(markRead);
      const okBtn = await until(() => $("readbtn").textContent.trim() === "已读 ▾", 4000);
      out.点标为已读_按钮文案 = $("readbtn").textContent.trim();
      out.点标为已读_服务端状态 = (await library()).status[B] || "unread";
      if (!okBtn) fails.push(`点「标为已读」后期望 #readbtn 文案「已读 ▾」，实际「${out.点标为已读_按钮文案}」`);
      if (out.点标为已读_服务端状态 !== "read") fails.push(`点「标为已读」后期望服务端 read，实际 ${out.点标为已读_服务端状态}`);
      out.工具条菜单收起 = $("readmenu").hidden === true;
      if (!out.工具条菜单收起) fails.push("点完菜单项后 #readmenu 没有收起");
    }

    // 6) 打开未读文章 → 服务端自动变「在读」
    await setStatus(C, "");
    await window.loadLibrary();
    await sleep(250);
    out.打开丙前_服务端状态 = (await library()).status[C] || "unread";
    await window.openEpisode(encodeURIComponent(C));
    const autoReading = await until(async () => (await library()).status[C] === "reading", 4000);
    out.打开丙后_服务端状态 = (await library()).status[C] || "unread";
    out.打开丙后_工具条按钮 = $("readbtn").textContent.trim();
    if (!autoReading) fails.push(`打开未读文章后期望服务端自动变成 reading，实际 ${out.打开丙后_服务端状态}`);

    // 7) 侧边栏「稍后读」筛选（此时甲 = later，乙 = read，丙 = reading）
    click(statusRow("later"));
    await sleep(400);
    out.筛选稍后读_主区标题 = $("libtitle").textContent;
    out.筛选稍后读_卡片目录 = [...doc.querySelectorAll("#libgrid .ep")].map((c) => c.dataset.dir);
    out.筛选稍后读_高亮行 = (doc.querySelector("#catnav .folder.on") || { textContent: "" }).textContent.replace(/\s+/g, " ").trim();
    if (out.筛选稍后读_主区标题 !== "稍后读") fails.push(`筛选后期望 #libtitle「稍后读」，实际「${out.筛选稍后读_主区标题}」`);
    if (JSON.stringify(out.筛选稍后读_卡片目录) !== JSON.stringify([A])) {
      fails.push(`「稍后读」筛选期望只显示「${A}」，实际 ${JSON.stringify(out.筛选稍后读_卡片目录)}`);
    }
    if (!out.筛选稍后读_高亮行.includes("稍后读")) fails.push(`筛选后侧边栏未高亮「稍后读」行，实际「${out.筛选稍后读_高亮行}」`);

    // 8) 拖卡片到侧边栏「已读」行
    click(rows.length ? [...doc.querySelectorAll("#catnav .folder")].find((f) => f.textContent.includes("全部文章")) : null);
    await sleep(400);
    const cardC = card(C);
    out.拖拽前_丙卡片存在 = !!cardC;
    const pd = (t, x, y, el) => (el || doc).dispatchEvent(new window.MouseEvent(t, {
      bubbles: true, clientX: x, clientY: y, button: 0,
    }));
    window.__under = cardC;
    pd("pointerdown", 100, 100, cardC);
    const readRow = statusRow("read");
    window.__under = readRow;
    pd("pointermove", 160, 140);
    await sleep(150);
    out.拖拽_已读行高亮 = readRow.classList.contains("drop");
    if (!out.拖拽_已读行高亮) fails.push("把卡片拖到「已读」行时该行没有高亮 .drop");
    pd("pointerup", 160, 140);
    const dragged = await until(async () => (await library()).status[C] === "read", 4000);
    await sleep(200);
    out.拖拽后_丙服务端状态 = (await library()).status[C] || "unread";
    out.拖拽后_丙卡片文案 = card(C) ? card(C).textContent.replace(/\s+/g, " ").trim().slice(0, 70) : "(卡片不存在)";
    if (!dragged) fails.push(`拖到「已读」行后期望服务端 read，实际 ${out.拖拽后_丙服务端状态}`);
  } catch (e) {
    fails.push("异常：" + ((e && e.stack) || e));
  } finally {
    for (const name of Object.keys(files)) {
      try { await setStatus(name, ""); } catch (e) { /* 服务已退出时忽略 */ }
      fs.rmSync(files[name], { recursive: true, force: true });
    }
  }
  out.清理_残留目录 = Object.values(files).filter((p) => fs.existsSync(p));
  if (out.清理_残留目录.length) fails.push("自造数据没清干净：" + out.清理_残留目录.join("、"));

  report("阅读状态全部通过", out, fails);
})();
