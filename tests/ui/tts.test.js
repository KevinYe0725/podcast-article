/**
 * 阅读页的「朗读」播放条。
 *
 * 生成本身由 tests/test_tts.py 用真实后端测（macOS 的 say）；这里只测界面逻辑：
 * 播放条什么时候出现、状态文案对不对、整篇 / 分段两种模式接的音频源对不对。
 * 所有 /api/tts 请求都打桩成固定响应，所以不依赖服务器上有没有配 TTS。
 */
const { boot, report, sleep } = require("./harness");

const READY_MERGED = {
  state: "ready", enabled: true, provider: "macos", voice: "Tingting",
  chars: 2966, seconds: 649.1, total: 5, done: 5, fresh: true,
  merged: "/api/tts/X/audio", chunks: [], error: "", note: "",
};
const READY_CHUNKS = {
  ...READY_MERGED, merged: null,
  chunks: [{ url: "/api/tts/X/chunk/0", chars: 600 }, { url: "/api/tts/X/chunk/1", chars: 620 }],
};
const RUNNING = { ...READY_MERGED, state: "running", merged: null, chunks: [], done: 2, note: "第 3/5 块" };
const OFF = { ...READY_MERGED, state: "idle", enabled: false, merged: null, chunks: [], chars: null, seconds: null };

(async () => {
  const out = {}, fails = [];

  /** 起一个页面，并把 /api/tts 的三个接口打桩成给定状态 */
  async function withTts(initial) {
    const t = await boot({
      beforeParse(w) {
        w.__tts = { status: initial, posts: [], deletes: 0 };
        const real = w.fetch;
        w.fetch = async (url, opts) => {
          const u = String(url);
          if (u.includes("/api/tts/") && u.endsWith("/status")) {
            return { ok: true, json: async () => w.__tts.status };
          }
          if (u.includes("/api/tts/") && (!opts || !opts.method || opts.method === "GET")) {
            return { ok: true, json: async () => ({ state: "running", tts: RUNNING }) };
          }
          if (/\/api\/tts\/[^/]+$/.test(u) && opts && opts.method === "POST") {
            w.__tts.posts.push(JSON.parse(opts.body || "{}"));
            w.__tts.status = RUNNING;
            return { ok: true, json: async () => ({ state: "running", tts: RUNNING }) };
          }
          if (/\/api\/tts\/[^/]+$/.test(u) && opts && opts.method === "DELETE") {
            w.__tts.deletes += 1;
            w.__tts.status = OFF;
            return { ok: true, json: async () => ({ ok: true }) };
          }
          return real(u, opts);
        };
      },
    });
    const card = t.doc.querySelector("#libgrid .ep");
    if (!card) { console.log("没有历史卡片，无法测试"); process.exit(1); }
    const dirArg = card.getAttribute("onclick").match(/'([^']+)'/)[1];
    await t.window.openEpisode(dirArg);
    await sleep(900);
    t.window.ttsWireAudio();
    return t;
  }

  // ---------- 1) 打开文章：播放条默认收起，点按钮才展开
  let t = await withTts(READY_MERGED);
  out.打开后_播放条隐藏 = t.$("ttsbar").style.display === "none";
  out.打开后_按钮存在 = !!t.$("ttsbtn");
  t.window.toggleTtsBar();
  await sleep(300);
  out.展开后_播放条可见 = t.$("ttsbar").style.display !== "none";
  if (!out.打开后_播放条隐藏) fails.push("刚打开文章时朗读条不该自己展开");
  if (!out.打开后_按钮存在) fails.push("阅读页工具条上没有「朗读」按钮");
  if (!out.展开后_播放条可见) fails.push("点「朗读」没有展开播放条");

  // ---------- 2) 整篇模式（有 ffmpeg 拼过）
  out.整篇_状态文案 = t.$("ttsstate").textContent.trim();
  out.整篇_计数 = t.$("ttscount").textContent.trim();
  out.整篇_音频源 = t.$("ttsaudio").getAttribute("src") || "";
  if (!out.整篇_状态文案.includes("Tingting")) fails.push(`状态里要显示音色：「${out.整篇_状态文案}」`);
  if (out.整篇_音频源 !== "/api/tts/X/audio") fails.push(`整篇模式该用合并文件，实际「${out.整篇_音频源}」`);
  if (!/\d/.test(out.整篇_计数)) fails.push(`计数该显示时长，实际「${out.整篇_计数}」`);

  // 播放：改了倍速要作用到 audio 上
  t.$("ttsspeed").value = "1.5";
  t.window.ttsSetRate();
  out.倍速 = t.$("ttsaudio").playbackRate;
  if (Math.abs(out.倍速 - 1.5) > 0.001) fails.push(`倍速没生效：${out.倍速}`);

  // 删除：条要收起来
  await t.window.ttsDelete();
  await sleep(200);
  out.删除后_条已收起 = t.$("ttsbar").style.display === "none";
  out.删除_请求次数 = t.window.__tts.deletes;
  if (!out.删除后_条已收起) fails.push("删除后朗读条该收起来");
  if (out.删除_请求次数 !== 1) fails.push(`删除应该只发一次请求，实际 ${out.删除_请求次数}`);
  t.window.close();

  // ---------- 3) 分段模式（没有 ffmpeg）：按块连播
  t = await withTts(READY_CHUNKS);
  t.window.toggleTtsBar();
  await sleep(300);
  out.分段_音频源 = t.$("ttsaudio").getAttribute("src") || "";
  out.分段_计数 = t.$("ttscount").textContent.trim();
  if (out.分段_音频源 !== "/api/tts/X/chunk/0") fails.push(`分段模式该从第 0 块开始，实际「${out.分段_音频源}」`);
  if (!out.分段_计数.includes("1/2")) fails.push(`计数该显示第 1/2 段，实际「${out.分段_计数}」`);
  // 第一段播完应自动接第二段
  const a = t.$("ttsaudio");
  Object.defineProperty(a, "played", { value: [], writable: true, configurable: true });
  a.play = function () { this.played.push(this.getAttribute("src")); return Promise.resolve(); };
  a.dispatchEvent(new t.window.Event("ended"));
  await sleep(200);
  out.分段_连播后音频源 = a.getAttribute("src");
  if (out.分段_连播后音频源 !== "/api/tts/X/chunk/1") {
    fails.push(`第一段结束该自动接第二段，实际「${out.分段_连播后音频源}」`);
  }
  t.window.close();

  // ---------- 4) 未启用后端：点朗读要给出去设置页的指引，而不是傻等着
  t = await withTts(OFF);
  t.window.toggleTtsBar();
  await sleep(200);
  t.window.ttsStart(false);
  await sleep(300);
  out.未启用_提示 = t.$("toast").textContent.trim();
  out.未启用_没有发请求 = t.window.__tts.posts.length;
  if (!out.未启用_提示.includes("设置")) fails.push(`未启用时该提示去设置页，实际「${out.未启用_提示}」`);
  if (out.未启用_没有发请求 !== 0) fails.push("未启用时不该发生成请求（会白白失败）");
  out.未启用_状态文案 = t.$("ttsstate").textContent.trim();
  if (!out.未启用_状态文案.includes("设置")) fails.push(`状态区也该写明去哪开：「${out.未启用_状态文案}」`);
  t.window.close();

  report("朗读播放条通过", out, fails);
})();
