/**
 * 手机端（窄屏）适配。
 *
 * ⚠️ 这里**不做布局验证**：jsdom 没有排版引擎，也不求值 @media（实测
 * `getComputedStyle(.appmain).padding-left` 在 390px 下依然返回桌面的 40px），
 * 所以「手机上长什么样」只能靠人眼看。这份测试守的是两件能确定的事：
 *
 *  1) 行为：顶栏用量胶囊在窄屏换短文案、转屏/拉窗口时跟着换（真跑页面脚本）
 *  2) 规矩（CSS 静态检查）：手机上必须成立的几条硬约束 —— 输入框 ≥16px（否则 iOS
 *     聚焦会整页放大）、抽屉高度用 100dvh、卡片网格不撑破屏宽、触屏没有 hover 的
 *     入口要直接可见、贴底元素让开 Home 指示条
 */
const { boot, report, sleep, BASE } = require("./harness");
const fs = require("fs");
const path = require("path");

const OUT = process.env.PA_OUTPUT_DIR;
const DIR = "__UI测试单集";        // seed.js 造的那一集
const USAGE_FILE = OUT ? path.join(OUT, DIR, "usage.json") : "";

/** 取出某个 @media 块（用 marker 认准是哪一个，同名块有好几个） */
function pickBlock(css, header, marker) {
  let i = -1;
  while ((i = css.indexOf(header, i + 1)) >= 0) {
    const start = css.indexOf("{", i);
    if (start < 0) break;
    let depth = 0, j = start;
    for (; j < css.length; j++) {
      if (css[j] === "{") depth++;
      else if (css[j] === "}" && --depth === 0) break;
    }
    const body = css.slice(start + 1, j);
    if (!marker || body.includes(marker)) return body;
  }
  return "";
}

(async () => {
  if (!OUT) { console.log("缺少 PA_OUTPUT_DIR，必须通过 run.sh 运行"); process.exit(2); }
  const out = {}, fails = [];

  // 给这一集补一份用量记录（顶栏胶囊要有内容才测得出短/长文案的差别）；
  // 收尾时删掉，别影响后面的测试
  fs.writeFileSync(USAGE_FILE, JSON.stringify({
    model: "deepseek-flash", calls: 84,
    hit_tokens: 500000, miss_tokens: 100000, out_tokens: 40000,
    peak: { hit: 0, miss: 0, out: 0 },
    off: { hit: 500000, miss: 100000, out: 40000 },
    elapsed_s: 312.5,
  }));

  const usage = (await (await fetch(BASE + "/api/library")).json()).usage_total || {};
  out.数据_累计费用 = usage.cost_cny;
  if (!usage.episodes) fails.push("临时输出目录里没能造出用量数据，胶囊断言没有意义");

  // ---------- 1) 窄屏下的真实行为：用量胶囊换短文案
  const narrow = await boot({
    beforeParse(w) {
      Object.defineProperty(w, "innerWidth", { value: 390, configurable: true });
    },
  });
  const money = /\d/.test(String(usage.cost_cny))
    ? String((usage.cost_cny >= 1 ? usage.cost_cny.toFixed(2) : usage.cost_cny.toFixed(3)))
    : "";
  out.窄屏_窗口宽度 = narrow.window.innerWidth;
  out.窄屏_胶囊文案 = narrow.$("usagepill").textContent.trim();
  out.窄屏_胶囊有明细 = narrow.$("usagepill").title.includes("次模型调用");
  if (out.窄屏_窗口宽度 !== 390) fails.push("测试环境没把窗口设成窄屏，后面的断言没有意义");
  if (out.窄屏_胶囊文案.includes("tokens")) {
    fails.push(`手机顶栏的用量胶囊应只留金额，实际「${out.窄屏_胶囊文案}」`);
  }
  if (money && !out.窄屏_胶囊文案.includes(money)) {
    fails.push(`短文案里应保留金额 ${money}，实际「${out.窄屏_胶囊文案}」`);
  }
  if (!out.窄屏_胶囊有明细) fails.push("短文案不能把明细弄丢，title 里要有完整数字");

  // 转屏到桌面宽度 → 立刻换回完整文案（同一个 window 上改宽度再渲染）
  Object.defineProperty(narrow.window, "innerWidth", { value: 1280, configurable: true });
  narrow.window.renderUsagePill();
  await sleep(50);
  out.宽屏_胶囊文案 = narrow.$("usagepill").textContent.trim();
  if (!out.宽屏_胶囊文案.includes("tokens") || !out.宽屏_胶囊文案.includes("累计")) {
    fails.push(`宽屏应显示完整用量，实际「${out.宽屏_胶囊文案}」`);
  }
  narrow.window.close();

  // ---------- 2) 窄屏导航：汉堡按钮 → 抽屉 + 遮罩（手机上的主导航就是它）
  const { window, doc, $ } = await boot({
    beforeParse(w) {
      Object.defineProperty(w, "innerWidth", { value: 390, configurable: true });
    },
  });
  out.汉堡按钮存在 = !!doc.querySelector(".topbar .menubtn");
  out.初始_抽屉未展开 = !$("appside").classList.contains("open");
  window.toggleSide();
  await sleep(50);
  out.展开后_抽屉 = $("appside").classList.contains("open");
  out.展开后_遮罩 = !!(doc.querySelector(".sideback") || {}).classList &&
    doc.querySelector(".sideback").classList.contains("open");
  window.closeSide();
  await sleep(50);
  out.收起后_抽屉 = !$("appside").classList.contains("open");
  if (!out.汉堡按钮存在) fails.push("顶栏没有汉堡按钮（手机上打不开侧边栏）");
  if (!out.展开后_抽屉) fails.push("toggleSide() 没有展开抽屉");
  if (!out.展开后_遮罩) fails.push("抽屉展开时没有遮罩层（点空白关不掉）");
  if (!out.收起后_抽屉) fails.push("closeSide() 没有收起抽屉");
  window.close();

  // ---------- 3) CSS 静态检查：手机上必须成立的几条硬约束
  const css = await (await fetch(BASE + "/static/app.css")).text();
  const mobile = pickBlock(css, "@media (max-width: 900px)", ".menubtn { display: inline-flex; }");
  const hoverNone = pickBlock(css, "@media (hover: none)", "");
  out.CSS_找到移动块 = !!mobile;
  out.CSS_找到触屏块 = !!hoverNone;
  if (!mobile) fails.push("找不到窄屏（≤900px）那段样式，检查选择器或 media 查询");

  const rule = (blockText, re) => re.test(blockText);

  // 3a. iOS 聚焦放大：输入控件在手机上必须 ≥16px
  out.CSS_桌面输入框小于16 = rule(css, /\.composer #url \{[\s\S]{0,400}?font-size: 15\.5px/);
  out.CSS_手机输入框16px = rule(mobile, /\.composer #url[\s\S]{0,600}?font-size: 16px/);
  if (!out.CSS_桌面输入框小于16) {
    fails.push("桌面输入框字号不再是 15.5px —— 检查这条断言是否已过时");
  }
  if (!out.CSS_手机输入框16px) {
    fails.push("手机上的输入框没有提到 16px：iOS 聚焦时会整页放大");
  }

  // 3b. 抽屉高度：100vh 之外必须有 100dvh（iOS 地址栏会吃掉 100vh 的底部）
  out.CSS_抽屉带dvh = rule(mobile, /height: 100vh;\s*height: 100dvh;/);
  if (!out.CSS_抽屉带dvh) fails.push("侧边栏抽屉只有 100vh，iOS 上底部（设置按钮）会被地址栏挡住");

  // 3c. 卡片网格在 320px 宽的机器上不能撑出横向滚动
  out.CSS_网格用min = css.includes("minmax(min(300px, 100%), 1fr)");
  if (!out.CSS_网格用min) fails.push("卡片网格的最小轨道没有用 min(300px, 100%)，窄屏会横向溢出");

  // 3d. 触屏没有 hover：靠悬停才出现的入口要直接可见
  out.CSS_触屏_分类入口可见 = rule(hoverNone, /\.ep \.catlabel\.empty \{ opacity: 1; \}/);
  if (!out.CSS_触屏_分类入口可见) fails.push("触屏设备上「＋ 分类」仍然靠 hover 才出现，等于看不见");

  // 3e. 贴底元素让开 Home 指示条
  out.CSS_安全区 = ["player", ".fab", ".appmain", ".rdbody", ".appside"]
    .filter((k) => !css.includes(`safe-area-inset-bottom`)) .length === 0;
  out.CSS_安全区出现次数 = (css.match(/env\(safe-area-inset-bottom\)/g) || []).length;
  if (out.CSS_安全区出现次数 < 4) {
    fails.push(`贴底元素的安全区处理太少（${out.CSS_安全区出现次数} 处）`);
  }

  // 3f. 输入类控件的聚焦提示不能用绿色（用户反馈：点一下粘贴框就套一个绿框）。
  //     原因记在这：textarea / input 点击也会命中 :focus-visible，所以绿色描边必须只留给按钮。
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");   // 先去掉注释，否则说明文字会被当成规则
  const focusRules = (bare.match(/[^{}]*:focus[^{}]*\{[^}]*\}/g) || []);
  const greenInputFocus = focusRules.filter((r) =>
    /var\(--green\)/.test(r) && /(input|textarea|searchbox|#url|\.field|modaltarea)/.test(r));
  out.CSS_输入框聚焦不用绿色 = greenInputFocus.length === 0;
  if (!out.CSS_输入框聚焦不用绿色) {
    fails.push("输入框的聚焦样式还是绿色：" +
      greenInputFocus.map((r) => r.split("{")[0].trim()).join(" / "));
  }

  // 3g. 侧边栏是「可滚动的纵向 flex 容器」：里面的行必须不可压缩。
  //     压缩了就是「分类按钮都扁了」那个症状（内容超过一屏时纵向 flex 会按比例压行）。
  out.CSS_分类行不可压缩 = /\.folder \{[^}]*flex: 0 0 auto/.test(bare);
  out.CSS_抽屉子块不可压缩 = /\.appside > \*, \.appside nav > \* \{ flex: 0 0 auto; \}/.test(bare);
  if (!out.CSS_分类行不可压缩) fails.push("侧边栏分类行没有 flex: 0 0 auto，内容超一屏时会被压扁");
  if (!out.CSS_抽屉子块不可压缩) fails.push("抽屉的直接子块没有 flex: 0 0 auto，会被压扁");

  // 3h. 触屏上可点区域要够大（iOS HIG 44pt），且 hover-only 的入口要直接可见
  out.CSS_触屏_分类行高度 = /\.folder \{ min-height: 44px; \}/.test(hoverNone);
  out.CSS_触屏_重命名入口 = /\.folder \.fedit \{ opacity: 1; \}/.test(hoverNone);
  if (!out.CSS_触屏_分类行高度) fails.push("触屏上分类行没有 44px 的可点高度");
  if (!out.CSS_触屏_重命名入口) fails.push("触屏上分类的「✎/✕」仍然只在 hover 时出现，等于点不到");

  // 3i. 宽内容（表格 / 代码块）在手机上要自己横滚，不许把整页撑宽
  out.CSS_表格可横滚 = rule(mobile, /#article table \{ display: block; overflow-x: auto/);
  if (!out.CSS_表格可横滚) fails.push("文章里的表格在窄屏没有横滚处理，会把整页撑出横向滚动");

  try { fs.unlinkSync(USAGE_FILE); } catch (e) { /* 本来就没有就算了 */ }
  report("手机端适配检查通过", out, fails);
})();
