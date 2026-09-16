/**
 * 只读镜像模式的前端表现（公网部署那台机器）。
 *
 * 后端拦截由 tests/test_readonly.py 守；这里守的是「界面别让人白点」：
 * 输入框、生成按钮、发布按钮、助手悬浮球都要收起并写清原因。
 *
 * 注：测试服务器不是只读模式，所以这里走 `applyServerConfig()` 主动切一遍 ——
 * 同时顺便验证 `/api/config` 在普通模式下的真实返回。
 */
const { boot, report, sleep, until } = require("./harness");

(async () => {
  const { window, doc, $ } = await boot();
  const out = {}, fails = [];

  // ---------- 1) 普通模式（本机）：一切照常，且 /api/config 能拿到
  const cfg = await window.loadServerConfig();
  out.普通_服务端配置 = cfg;
  out.普通_输入框可用 = !$("url").disabled;
  out.普通_按钮文案 = $("go").textContent.trim();
  if (cfg.readonly !== false) fails.push(`普通模式下 /api/config 应说 readonly=false，实际 ${JSON.stringify(cfg)}`);
  if ($("url").disabled) fails.push("普通模式下输入框不该是禁用的");
  if (out.普通_按钮文案 !== "生成文章") fails.push(`普通模式按钮文案不对：「${out.普通_按钮文案}」`);

  // ---------- 2) 打开一篇文章（悬浮球的条件依赖「有文章在读」）
  const card = doc.querySelector("#libgrid .ep");
  if (!card) { console.log("没有历史卡片，无法测试"); process.exit(1); }
  const dirArg = card.getAttribute("onclick").match(/'([^']+)'/)[1];
  await window.openEpisode(dirArg);
  await until(() => $("result").classList.contains("show"), 8000, 150);
  await sleep(200);
  out.普通_悬浮球出现 = $("fab").classList.contains("show");
  if (!out.普通_悬浮球出现) fails.push("普通模式下打开文章后应出现助手悬浮球（否则下面的断言没有意义）");

  // ---------- 3) 切成只读镜像
  window.applyServerConfig({
    readonly: true, hint: "这是一台只读镜像：生成文章、语音转写与 AI 助手都在你的 Mac 上跑。",
    assistant: false, publish: false,
  });
  await sleep(100);
  out.只读_输入框禁用 = $("url").disabled;
  out.只读_占位文案 = $("url").placeholder;
  out.只读_按钮文案 = $("go").textContent.trim();
  out.只读_按钮禁用 = $("go").disabled;
  const hintText = $("hint").textContent.trim();
  out.只读_提示 = hintText.slice(0, 30) + "…";
  out.只读_发布禁用 = $("nbtn").disabled;
  out.只读_悬浮球 = $("fab").classList.contains("show");
  if (!out.只读_输入框禁用) fails.push("只读镜像下输入框应禁用");
  if (!out.只读_占位文案.includes("Mac")) fails.push(`只读镜像的占位文案要指出在 Mac 上做：「${out.只读_占位文案}」`);
  if (!out.只读_按钮禁用 || !out.只读_按钮文案.includes("只读")) {
    fails.push(`只读镜像下按钮应禁用并写明只读，实际「${out.只读_按钮文案}」disabled=${out.只读_按钮禁用}`);
  }
  if (!hintText.includes("Mac")) fails.push(`提示里要说清算力在 Mac 上：「${hintText.slice(0, 60)}」`);
  if (!out.只读_发布禁用) fails.push("只读镜像下发布按钮应禁用");
  if (out.只读_悬浮球) fails.push("只读镜像下不该出现助手悬浮球（这台机器没有密钥）");

  // 文案不会被 updateComposerHint 覆盖回去
  $("url").dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(80);
  out.只读_输入后按钮文案 = $("go").textContent.trim();
  if (!out.只读_输入后按钮文案.includes("只读")) {
    fails.push(`只读镜像下按钮文案被 updateComposerHint 覆盖了：「${out.只读_输入后按钮文案}」`);
  }

  // ---------- 4) 切回普通模式要恢复
  window.applyServerConfig(null);
  await sleep(80);
  out.恢复_输入框可用 = !$("url").disabled;
  out.恢复_悬浮球 = $("fab").classList.contains("show");
  if (!out.恢复_输入框可用) fails.push("切回普通模式后输入框应恢复可用");
  if (!out.恢复_悬浮球) fails.push("切回普通模式后助手悬浮球应恢复");

  report("只读镜像前端通过", out, fails);
})();
