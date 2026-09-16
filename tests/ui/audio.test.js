const { boot, report, sleep } = require("./harness");
const BASE = process.env.PA_BASE || "http://127.0.0.1:8787";
(async () => {
  const out = {}, fails = [];
  const { window, doc, $ } = await boot();

  // 打开一篇有音频的文章
  const card = [...doc.querySelectorAll("#libgrid .ep")].find((c) => c.dataset.dir.includes("鱼不存在"))
            || doc.querySelector("#libgrid .ep");
  const dir = card.dataset.dir;
  await window.openEpisode(encodeURIComponent(dir));
  await sleep(1500);

  const tsLinks = doc.querySelectorAll("#article .ts");
  out.文章内可点时间戳 = tsLinks.length;
  out.示例 = tsLinks.length ? tsLinks[0].textContent + " → " + tsLinks[0].dataset.sec + " 秒" : "-";
  if (!tsLinks.length) fails.push("文章里没有可点时间戳");

  // 点时间戳 → 载入音频、播放条出现
  const ts = tsLinks[0];
  ts.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(400);
  out.播放条出现 = $("player").classList.contains("show");
  out.音频地址 = (pa_src = $("paudio").getAttribute("src") || "").includes("/api/audio/") ? decodeURIComponent(pa_src).slice(-24) : "(空)";
  out.标题 = $("ptitle").textContent.slice(0, 26);
  out.时间戳高亮 = ts.classList.contains("active");
  out.播放图标_载入前 = $("picon-play").style.display === "block" ? "播放" : "暂停";
  if (!out.播放条出现) fails.push("播放条没出现");
  if (!( $("paudio").getAttribute("src") || "").includes("/api/audio/")) fails.push("音频地址未设置");
  if (!out.时间戳高亮) fails.push("被点的时间戳没高亮");

  // 模拟播放进度
  const a = $("paudio");
  Object.defineProperty(a, "duration", { value: 6564, configurable: true });
  Object.defineProperty(a, "currentTime", { value: 607, writable: true, configurable: true });
  a.dispatchEvent(new window.Event("loadedmetadata"));
  a.dispatchEvent(new window.Event("timeupdate"));
  await sleep(200);
  out.载入后_自动播放图标 = $("picon-pause").style.display === "block" ? "暂停(播放中)" : "仍是播放";
  if ($("picon-pause").style.display !== "block") fails.push("载入元数据后未开始播放");
  out.总时长显示 = $("ptotal").textContent;
  out.当前时间显示 = $("pnow").textContent;
  out.进度条宽度 = $("pfill").style.width;
  if (out.当前时间显示 !== "10:07") fails.push("当前时间显示不对: " + out.当前时间显示);
  if (out.总时长显示 !== "1:49:24") fails.push("总时长显示不对: " + out.总时长显示);

  // 暂停
  window.togglePlay();
  await sleep(100);
  out.暂停后图标 = $("picon-play").style.display === "block" ? "播放" : "暂停";

  // ±15 秒
  window.seekBy(15); await sleep(50);
  out.前进15秒后 = Math.round(a.currentTime);
  if (out.前进15秒后 !== 622) fails.push("±15 秒跳转不对: " + out.前进15秒后);

  // 点进度条中点
  const bar = $("pbar");
  bar.getBoundingClientRect = () => ({ left: 0, width: 200, top: 0, height: 4, right: 200, bottom: 4 });
  bar.dispatchEvent(new window.MouseEvent("click", { bubbles: true, clientX: 100 }));
  await sleep(50);
  out.点进度条中点 = Math.round(a.currentTime);
  if (Math.abs(out.点进度条中点 - 3282) > 5) fails.push("进度条跳转不对: " + out.点进度条中点);

  // 文字稿里的时间戳也可点
  await window.toggleTranscript();
  await sleep(1200);
  out.文字稿内可点时间戳 = doc.querySelectorAll("#transcript .ts").length;
  if (!out.文字稿内可点时间戳) fails.push("文字稿里没有可点时间戳");

  // 时间区间的时间戳也要能点：模型引用一段跨了十几秒的话时就写 [00:20:00-00:20:15]
  // （真实反馈：这种时候链一个都点不动），点了要跳到区间起点
  const rangeTs = [...doc.querySelectorAll("#article .ts")].find((el) => el.textContent.includes("-"));
  out.区间时间戳_存在 = !!rangeTs;
  if (!rangeTs) {
    fails.push("区间形态的时间戳（[00:20:00-00:20:15]）没有变成可点元素");
  } else {
    out.区间时间戳_文案 = rangeTs.textContent.trim();
    out.区间时间戳_秒数 = rangeTs.dataset.sec;
    rangeTs.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    // jsdom 里 audio.readyState 始终是 0，playAt 会把跳转挂到 loadedmetadata 上再执行
    // （上面第一次点击也是靠这招才真的跳到 607 秒）
    a.dispatchEvent(new window.Event("loadedmetadata"));
    await sleep(300);
    out.区间时间戳_高亮 = rangeTs.classList.contains("active");
    out.区间时间戳_跳转 = Math.round(a.currentTime);
    if (out.区间时间戳_秒数 !== "1200") {
      fails.push(`区间时间戳应指向起点 1200 秒，实际 data-sec=${out.区间时间戳_秒数}`);
    }
    if (out.区间时间戳_跳转 !== 1200) {
      fails.push(`点区间时间戳应跳到 1200 秒，实际 ${out.区间时间戳_跳转}`);
    }
    if (!out.区间时间戳_高亮) fails.push("点过的区间时间戳没有高亮");
  }

  // 关闭播放器
  window.closePlayer();
  await sleep(200);
  out.关闭后_播放条隐藏 = !$("player").classList.contains("show");
  out.关闭后_高亮清除 = doc.querySelectorAll(".ts.active").length === 0;
  if (!out.关闭后_播放条隐藏 || !out.关闭后_高亮清除) fails.push("关闭播放器后状态未复位");

  report("时间戳回听全部通过", out, fails);
})();
