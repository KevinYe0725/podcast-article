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
const { JSDOM, VirtualConsole } = require("jsdom");
const fs = require("fs");

// jsdom 不实现 EventSource，而页面用 SSE 推任务进度。不补上它的话，
// 任何「真的启动一次任务」的测试都会在 new EventSource 处直接崩掉。
// （package.json 里本来就依赖 eventsource，就是为这个场景准备的。）
//
// 注意：eventsource 这个包**不接受相对地址**（浏览器里 "/api/stream/x" 会按文档
// URL 解析，包里直接抛 DOMException SyntaxError），所以这里补一层把相对地址补全成绝对地址。
let EventSourcePolyfill = null;
try {
  EventSourcePolyfill = require("eventsource").EventSource;
} catch (e) {
  EventSourcePolyfill = null;
}

const BASE = process.env.PA_BASE || "http://127.0.0.1:8787";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const nodeFetch = globalThis.fetch.bind(globalThis);
let seededSession = {};
try { seededSession = JSON.parse(fs.readFileSync(process.env.PA_UI_SESSION_FILE, "utf8")); } catch (_) {}

function EventSourceShim(url, opts) {
  const abs = /^https?:/i.test(String(url)) ? String(url) : BASE + String(url);
  return new EventSourcePolyfill(abs, opts);
}

/** 起一个真实页面。opts.beforeParse 会在页面脚本执行前拿到 window，可继续打桩。
    opts.url 可以指定带 hash 的地址（测「刷新阅读页」这类深链接）。 */
async function boot(opts = {}) {
  const targetUrl = opts.url || BASE;
  const target = new URL(targetUrl, BASE);
  const html = await (await harnessFetch(BASE + target.pathname + target.search)).text();
  const scrolled = [];
  const navigationAttempts = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => {
    if (String(error.message).includes("Not implemented: navigation")) navigationAttempts.push(error.message);
  });
  const dom = new JSDOM(html, {
    url: targetUrl,
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    virtualConsole,
    beforeParse(w) {
      w.__scrolled = scrolled;
      w.fetch = (u, o = {}) => harnessFetch(u.startsWith("http") ? u : BASE + u, o);
      w.scrollTo = () => {};
      w.Element.prototype.scrollIntoView = function () { scrolled.push(this.id || "(anon)"); };
      if (EventSourcePolyfill && !w.EventSource) w.EventSource = EventSourceShim;
      stubMedia(w);
      if (opts.beforeParse) opts.beforeParse(w);
    },
  });
  const { window } = dom;
  await waitReady(window, opts.settle);
  const doc = window.document;
  return {
    dom, window, doc, BASE, scrolled, navigationAttempts, sleep,
    $: (id) => doc.getElementById(id),
    /** 派发一次 Esc（用于验证分层退出） */
    esc: () => doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })),
    key: (k) => doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true })),
    /** 真实点击是「可取消」的：不写 cancelable 的话 preventDefault 会被忽略，jsdom 照旧
        执行链接的默认动作（跳到 href），测出来的行为会跟浏览器不一样。 */
    click: (el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true })),
  };
}

function harnessFetch(url, options = {}) {
  const requestUrl = new URL(String(url.url || url), BASE);
  const baseUrl = new URL(BASE);
  if (requestUrl.origin !== baseUrl.origin || !["127.0.0.1", "localhost", "::1"].includes(requestUrl.hostname.toLowerCase())) {
    return nodeFetch(url, options);
  }
  const headers = new Headers(options.headers || {});
  const cookieParts = [];
  if (seededSession.session_token) cookieParts.push(`pa_session=${seededSession.session_token}`);
  if (seededSession.csrf_token) cookieParts.push(`pa_csrf=${seededSession.csrf_token}`);
  if (cookieParts.length) headers.set("Cookie", cookieParts.join("; "));
  const method = options.method || url.method || "GET";
  if (seededSession.csrf_token && ["POST", "PUT", "PATCH", "DELETE"].includes(String(method).toUpperCase())) {
    headers.set("X-CSRF-Token", seededSession.csrf_token);
  }
  return nodeFetch(url, { ...options, headers });
}
globalThis.fetch = harnessFetch;

/** 页面的入口函数都挂在 window 上；等它出现再断言，避免和加载速度赛跑。 */
async function waitReady(window, limit) {
  const deadline = Date.now() + (limit || 12000);
  const pathname = window.location.pathname;
  while (Date.now() < deadline) {
    if ((pathname === "/login" || pathname === "/invite") && window.__loginPageReady) return;
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
