/**
 * UI 测试共用骨架。
 *
 * 页面把样式与脚本拆到了 /static/app.css 与 /static/app.js。jsdom 默认**不会**去取
 * 外部资源（没开 resources 时 <script src> 被静默跳过），那样测试只会得到一堆
 * 「函数不存在」的假失败，还很难看出原因——所以这里统一开 resources: "usable"，
 * 让 jsdom 真的把页面声明的 CSS/JS 拉下来执行，等价于浏览器行为。
 *
 * 另外统一打掉几处 jsdom 没实现或不该在测试里真跑的东西：
 *  - fetch：改成直连同一个服务（jsdom 自带的实现不带 cookie/相对路径处理，且慢）
 *  - 滚动：记录调用目标，便于断言「关掉文章后回到了列表」
 *  - 音频：jsdom 不实现播放，打桩成可控的假播放器
 */
const { JSDOM } = require("jsdom");

const BASE = process.env.PA_BASE || "http://127.0.0.1:8787";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 起一个真实页面。opts.beforeParse 会在页面脚本执行前拿到 window，可继续打桩。 */
async function boot(opts = {}) {
  const html = await (await fetch(BASE + "/")).text();
  const scrolled = [];
  const dom = new JSDOM(html, {
    url: BASE,
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    beforeParse(w) {
      w.__scrolled = scrolled;
      w.fetch = (u, o) => fetch(u.startsWith("http") ? u : BASE + u, o);
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () { scrolled.push(this.id || "(anon)"); };
      stubMedia(w);
      if (opts.beforeParse) opts.beforeParse(w);
    },
  });
  const { window } = dom;
  await waitReady(window, opts.settle);
  const doc = window.document;
  return {
    dom, window, doc, BASE, scrolled, sleep,
    $: (id) => doc.getElementById(id),
    /** 派发一次 Esc（用于验证分层退出） */
    esc: () => doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })),
    key: (k) => doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true })),
    click: (el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true })),
  };
}

/** 页面的入口函数都挂在 window 上；等它出现再断言，避免和加载速度赛跑。 */
async function waitReady(window, limit) {
  const deadline = Date.now() + (limit || 12000);
  while (Date.now() < deadline) {
    if (typeof window.loadLibrary === "function" && window.document.getElementById("libgrid")) {
      // loadLibrary 是异步的，再给一轮网络往返的时间把卡片渲染出来
      await sleep(1200);
      return;
    }
    await sleep(100);
  }
  throw new Error("页面脚本未就绪：/static/app.js 没被加载或执行（检查 index.html 里的 <script src>）");
}

/** jsdom 不实现 HTMLMediaElement 的播放能力，打桩成能记录状态的假播放器 */
function stubMedia(w) {
  w.HTMLMediaElement.prototype.play = function () {
    this._playing = true; this.dispatchEvent(new w.Event("play")); return Promise.resolve();
  };
  w.HTMLMediaElement.prototype.pause = function () {
    this._playing = false; this.dispatchEvent(new w.Event("pause"));
  };
  w.HTMLMediaElement.prototype.load = function () {};
}

/** 等某个条件成立；超时返回 false，让调用方自己决定怎么报错。 */
async function until(fn, ms = 5000, step = 100) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (fn()) return true;
    await sleep(step);
  }
  return false;
}

/** 测试收尾：输出结果对象与失败清单，退出码决定 CI 成败。 */
function report(title, out, fails) {
  console.log(JSON.stringify(out, null, 1));
  console.log(fails.length ? "✕ 失败: " + fails.join(" / ") : `✓ ${title}`);
  process.exit(fails.length ? 1 : 0);
}

module.exports = { boot, until, report, sleep, BASE, stubMedia };
