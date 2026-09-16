/**
 * 全文检索：#q 输入 → /api/search → 结果卡片 / 高亮 / 文字稿时间戳回听 / 空结果 / 回列表。
 *
 * 自造两集数据（只写 PA_OUTPUT_DIR 指向的临时目录，绝不碰仓库根目录 output/）：
 *   甲：article.md 里有独特中文词 + 英文 OpenAI
 *   乙：transcript.txt 里有带时间戳的独有句子
 * 特意不用 seed.js 里已有的「缝在鱼身上」，否则会命中共用测试数据、计数不确定。
 */
const fs = require("fs");
const path = require("path");
const { boot, until, report, sleep, BASE } = require("./harness");

const DIR_ART = "__搜索测试甲";           // 正文命中
const DIR_TS = "__搜索测试乙";            // 文字稿命中（带时间戳）
const WORD = "缝在鱼鳞上";                 // 只在甲的文章里
const LINE = "这是独有的一句话";           // 只在乙的文字稿里
const EN = "OpenAI";                      // 只在甲的文章里（英文，测大小写不敏感）

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

function writeEpisode(name, contents) {
  const dir = path.join(OUT, name);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  for (const [file, body] of Object.entries(contents)) {
    fs.writeFileSync(path.join(dir, file), body);
  }
  files[name] = dir;
  return dir;
}

async function setStatus(dir, status) {
  await fetch(BASE + "/api/status", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dir, status }),
  });
}

(async () => {
  checkEnv();
  const out = {}, fails = [];
  try {
    writeEpisode(DIR_ART, {
      "meta.json": JSON.stringify({
        title: "搜索测试甲", podcast: "检索台", author: "某人",
        url: "https://example.com/search-a", pub_date: "2024-03-01T00:00:00Z", duration: 1800,
      }),
      "article.md":
        "# 他把名字缝在鱼鳞上\n\n" +
        "> 一句话引语，只说张力不交底。\n\n" +
        "## 第一节：他把名字缝在鱼鳞上\n\n" +
        "正文在这里，讲一家叫 OpenAI 的公司，也讲一根针。\n",
      "transcript.txt": "[00:00:02] 甲的开场白\n",
    });
    writeEpisode(DIR_TS, {
      "meta.json": JSON.stringify({
        title: "搜索测试乙", podcast: "检索台",
        url: "https://example.com/search-b", pub_date: "2024-03-02T00:00:00Z", duration: 900,
      }),
      "article.md": "# 乙的文章\n\n这里没有检索关键词，只有一句收尾。\n",
      "transcript.txt": "[00:00:05] 乙的开场\n[00:10:07] " + LINE + "\n[00:20:00] 乙的收尾\n",
      "audio.m4a": Buffer.alloc(64 * 1024, 0),
    });

    const { window, doc, $ } = await boot();

    /** 输入关键词并等结果渲染完（标签变说明 renderSearch 已经跑完） */
    async function search(text) {
      $("q").value = text;
      $("q").dispatchEvent(new window.Event("input", { bubbles: true }));
      const done = await until(() => $("libtitle").textContent === `搜索「${text}」`, 5000);
      await sleep(60);
      return done;
    }
    const dirs = () => [...doc.querySelectorAll("#libgrid .sr")].map((c) => c.dataset.dir);

    // 1) 正文命中：卡片、标题、计数
    const ok1 = await search(WORD);
    out.正文搜索_渲染完成 = ok1;
    out.正文搜索_结果卡片数 = doc.querySelectorAll("#libgrid .sr").length;
    out.正文搜索_命中目录 = dirs();
    out.正文搜索_主区标题 = $("libtitle").textContent;
    out.正文搜索_计数文案 = $("libcount").textContent;
    out.正文搜索_命中字段 = [...doc.querySelectorAll("#libgrid .srfield")].map((e) => e.textContent);
    if (!ok1) fails.push(`输入「${WORD}」后 #libtitle 没有变成「搜索「${WORD}」」，实际「${$("libtitle").textContent}」`);
    if (out.正文搜索_结果卡片数 !== 1) fails.push(`搜「${WORD}」期望 1 张 .sr 结果卡片，实际 ${out.正文搜索_结果卡片数} 张（${out.正文搜索_命中目录.join("、")}）`);
    if (out.正文搜索_命中目录[0] !== DIR_ART) fails.push(`期望命中的是「${DIR_ART}」，实际「${out.正文搜索_命中目录.join("、")}」`);
    if (!out.正文搜索_计数文案.includes("命中")) fails.push(`期望 #libcount 含「命中」，实际「${out.正文搜索_计数文案}」`);

    // 2) 高亮
    const marks = [...doc.querySelectorAll("#libgrid mark")];
    out.高亮数量 = marks.length;
    out.高亮文本 = marks.map((m) => m.textContent);
    if (marks.length < 1) fails.push("搜索结果里没有 <mark> 高亮");
    if (marks.some((m) => m.textContent !== WORD)) {
      fails.push(`期望 <mark> 文本都是「${WORD}」，实际 ${JSON.stringify(out.高亮文本)}`);
    }

    // 3) 英文大小写不敏感
    await search(EN.toLowerCase());
    const enMarks = [...doc.querySelectorAll("#libgrid mark")].map((m) => m.textContent);
    out.小写查询_结果卡片数 = doc.querySelectorAll("#libgrid .sr").length;
    out.小写查询_高亮文本 = enMarks;
    if (out.小写查询_结果卡片数 !== 1) fails.push(`小写「${EN.toLowerCase()}」期望命中 1 集，实际 ${out.小写查询_结果卡片数} 集`);
    if (!enMarks.includes(EN)) fails.push(`期望高亮保留原文大小写「${EN}」，实际 ${JSON.stringify(enMarks)}`);

    // 4) AND 语义（空格分词 = 都要出现）
    await search(`${WORD} ${EN.toLowerCase()}`);
    const andDirs = dirs();
    out.AND_命中集数 = andDirs.length;
    out.AND_命中目录 = andDirs;
    if (andDirs.length !== 1 || andDirs[0] !== DIR_ART) {
      fails.push(`AND（「${WORD} ${EN.toLowerCase()}」）期望只命中「${DIR_ART}」，实际 ${JSON.stringify(andDirs)}`);
    }
    await search(`${WORD} ${LINE}`);
    out.AND反例_命中集数 = doc.querySelectorAll("#libgrid .sr").length;
    out.AND反例_网格为空 = $("libgrid").innerHTML.trim() === "";
    if (out.AND反例_命中集数 !== 0) {
      fails.push(`AND 反例（两个词分属两集）期望 0 命中，实际 ${out.AND反例_命中集数} 集`);
    }

    // 5) OR 语义（| 分隔 = 命中其一即可）
    await search(`${WORD}|${LINE}`);
    const orDirs = dirs().sort();
    out.OR_命中集数 = orDirs.length;
    out.OR_命中目录 = orDirs;
    out.语义结论 = "空格 = AND（两词都要出现）；| = OR（命中其一即可）——两种都测了";
    if (orDirs.length !== 2 || !orDirs.includes(DIR_ART) || !orDirs.includes(DIR_TS)) {
      fails.push(`OR（「${WORD}|${LINE}」）期望命中甲乙两集，实际 ${JSON.stringify(orDirs)}`);
    }

    // 6) 文字稿命中：字段标签 + 可点时间戳
    await search(LINE);
    const sr = doc.querySelector("#libgrid .sr");
    out.文字稿搜索_结果卡片数 = doc.querySelectorAll("#libgrid .sr").length;
    out.文字稿搜索_命中目录 = dirs();
    out.文字稿搜索_字段标签 = sr ? [...sr.querySelectorAll(".srfield")].map((e) => e.textContent) : [];
    const ts = sr ? sr.querySelector(".ts") : null;
    out.文字稿搜索_时间戳文本 = ts ? ts.textContent : "(无)";
    out.文字稿搜索_时间戳秒数 = ts ? ts.dataset.sec : "";
    out.文字稿搜索_时间戳归属 = ts ? ts.dataset.dir : "";
    if (!sr || out.文字稿搜索_命中目录[0] !== DIR_TS) fails.push(`搜「${LINE}」期望命中「${DIR_TS}」，实际 ${JSON.stringify(out.文字稿搜索_命中目录)}`);
    if (!out.文字稿搜索_字段标签.includes("文字稿")) fails.push(`期望命中条目有 .srfield「文字稿」，实际 ${JSON.stringify(out.文字稿搜索_字段标签)}`);
    if (!ts) fails.push("文字稿命中没有可点时间戳 .ts");

    // 7) 先打开另一篇（甲），再点搜索结果的 .ts：音频应该指向命中那一集（乙）
    await window.openEpisode(encodeURIComponent(DIR_ART));
    await sleep(700);
    out.点时间戳前_当前文章 = $("rmeta").textContent.replace(/\s+/g, " ").trim();
    const tsLive = doc.querySelector("#libgrid .sr .ts");
    if (tsLive) {
      tsLive.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
      await sleep(400);
      const src = $("paudio").src || "";
      out.播放条出现 = $("player").classList.contains("show");
      out.音频地址 = decodeURIComponent(src).replace(BASE, "");
      out.音频指向 = decodeURIComponent(src).includes(DIR_TS) ? DIR_TS
        : decodeURIComponent(src).includes(DIR_ART) ? DIR_ART + "（错：指向当前打开的文章）" : "(都不是)";
      out.音频时间 = $("pnow").textContent;
      if (!out.播放条出现) fails.push("#player 没有出现 .show（点时间戳未载入音频）");
      if (!src.includes("/api/audio/")) fails.push(`期望 #paudio.src 含 /api/audio/，实际「${src}」`);
      if (!decodeURIComponent(src).includes(DIR_TS)) {
        fails.push(`期望音频指向命中那一集「${DIR_TS}」，实际「${out.音频地址}」`);
      }
      if (decodeURIComponent(src).includes(DIR_ART)) {
        fails.push(`音频指向了当时打开的文章「${DIR_ART}」，而不是命中那一集`);
      }
      // 点搜索结果里的时间戳只应该放音频，不该顺带把文章打开（否则搜索结果会消失）
      await sleep(900);
      const rmeta = $("rmeta").textContent.replace(/\s+/g, " ").trim();
      out.点时间戳后_结果区标题 = rmeta || "(没有打开文章)";
      const opened = rmeta.includes("搜索测试乙");
      out.点搜索结果时间戳_是否顺带打开了文章 = opened ? "是（同时打开了该文章）" : "否（只播音频）";
      if (opened) fails.push("点搜索结果里的时间戳顺带打开了文章，搜索结果会因此丢失");
    }

    // 8) 搜不到：空提示 + 网格清空
    await search("绝对搜不到的词xyzzy");
    out.空结果_提示可见 = $("empty-lib").style.display === "block";
    out.空结果_提示文案 = $("empty-lib").textContent.trim();
    out.空结果_结果卡片数 = doc.querySelectorAll("#libgrid .sr").length;
    out.空结果_网格为空 = $("libgrid").innerHTML.trim() === "";
    if (!out.空结果_提示可见) fails.push("#empty-lib 没有显示（style.display=" + $("empty-lib").style.display + "）");
    if (out.空结果_结果卡片数 !== 0 || !out.空结果_网格为空) fails.push("搜不到时 #libgrid 应该为空");
    if (!out.空结果_提示文案.includes("没有找到")) fails.push(`期望空提示含「没有找到」，实际「${out.空结果_提示文案}」`);

    // 9) 点清空 → 回到列表模式
    $("qclear").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await sleep(300);
    out.清空后_主区标题 = $("libtitle").textContent;
    out.清空后_列表卡片数 = doc.querySelectorAll("#libgrid .ep").length;
    out.清空后_结果卡片数 = doc.querySelectorAll("#libgrid .sr").length;
    out.清空后_输入框值 = $("q").value;
    out.清空后_空提示隐藏 = $("empty-lib").style.display === "none";
    if (out.清空后_主区标题 !== "全部文章") fails.push(`清空后期望标题回到分类标题「全部文章」，实际「${out.清空后_主区标题}」`);
    if (out.清空后_列表卡片数 < 1) fails.push("清空后列表里没有 .ep 卡片");
    if (out.清空后_结果卡片数 !== 0) fails.push(`清空后仍残留 .sr 结果卡片 ${out.清空后_结果卡片数} 张`);
    if (out.清空后_输入框值 !== "") fails.push(`清空后 #q 应该为空，实际「${out.清空后_输入框值}」`);
  } catch (e) {
    fails.push("异常：" + ((e && e.stack) || e));
  } finally {
    // 清理：只删本次造的目录与阅读状态（打开文章会自动标成「在读」）
    for (const name of Object.keys(files)) {
      try { await setStatus(name, ""); } catch (e) { /* 服务已退出时忽略 */ }
      fs.rmSync(files[name], { recursive: true, force: true });
    }
  }
  out.清理_目录是否还在 = Object.values(files).filter((p) => fs.existsSync(p));
  if (out.清理_目录是否还在.length) fails.push("自造数据没清干净：" + out.清理_目录是否还在.join("、"));

  report("全文检索全部通过", out, fails);
})();
