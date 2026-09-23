/**
 * 批量队列的前端交互测试。
 *
 * 隔离说明：队列由 PlatformStore 按账号保存在 PA_DATA_ROOT/platform.sqlite。
 * 本测试通过 session_seed.py 对临时测试账号造数和核对，不读写仓库根目录的 queue.json。
 * run.sh 设了 PA_SCHEDULER=0，所以只有测试明确触发时才启动队列项。
 *
 * 链接一律用 http://127.0.0.1:9/...（discard 端口，没人监听）：一旦真的有任务
 * 被启动，它会立刻连接被拒并失败，既不会联网也不会挂住测试。
 */
const { boot, report, sleep, until } = require("./harness");
const { spawnSync } = require("node:child_process");

const QUEUE_FIXTURE = require("node:path").join(__dirname, "session_seed.py");
const legacyIds = new Map();
const LINKS = [
  "http://127.0.0.1:9/ep-one",
  "http://127.0.0.1:9/ep-two",
  "http://127.0.0.1:9/ep-three",
];

function queueFixture(mode, items) {
  const result = spawnSync("uv", ["run", "python", QUEUE_FIXTURE, "--session-file", process.env.PA_UI_SESSION_FILE, mode], {
    input: mode === "--queue-write" ? JSON.stringify(items) : undefined,
    encoding: "utf8", env: process.env,
  });
  if (result.status !== 0) throw new Error("无法读取账号测试队列：" + (result.stderr || "seed helper failed"));
  return JSON.parse(result.stdout);
}
const readQueue = () => ({ items: queueFixture("--queue-read").map((item) => ({
  ...item, id: [...legacyIds].find(([, actual]) => actual === item.id)?.[0] || item.id,
})) });
const writeQueue = (items) => {
  const created = queueFixture("--queue-write", items);
  items.forEach((item, index) => { if (item.id && created[index]) legacyIds.set(item.id, created[index].id); });
  return created;
};
const queueId = (id) => legacyIds.get(id) || id;
const clearQueue = () => writeQueue([]);

(async () => {
  if (!process.env.PA_PLATFORM_DB || !process.env.PA_TEST_USER_ID) { console.log("缺少临时账号数据库，必须通过 run.sh 运行"); process.exit(2); }
  clearQueue();

  const out = {}, fails = [];
  const { window, doc, $, BASE } = await boot();

  const rows = () => [...doc.querySelectorAll("#queuelist .qrow")];
  const urls = () => rows().map((r) => r.querySelector(".qurl").textContent.trim().split("　")[0]);

  // ---------- 1) 一次粘贴多条：按钮变成「加入队列 (N)」
  $("url").value = LINKS.join("\n");
  $("url").dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(200);
  out.按钮文案_多条 = $("go").textContent.trim();
  out.提示文案_多条 = $("hint").textContent.trim();
  if (!out.按钮文案_多条.includes("加入队列")) fails.push(`粘多条时按钮应变成「加入队列」，实际「${out.按钮文案_多条}」`);
  if (!out.按钮文案_多条.includes("3")) fails.push(`按钮上应显示条数 3，实际「${out.按钮文案_多条}」`);

  // 只留一条时按钮要变回去
  $("url").value = LINKS[0];
  $("url").dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(150);
  out.按钮文案_一条 = $("go").textContent.trim();
  if (out.按钮文案_一条 !== "生成文章") fails.push(`单条链接时按钮应回到「生成文章」，实际「${out.按钮文案_一条}」`);

  // ---------- 2) 点按钮 → 全部入队并切到队列视图
  $("url").value = LINKS.join("\n");
  $("url").dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(150);
  $("go").click();
  await until(() => rows().length === 3, 8000, 150);
  await sleep(600);

  out.队列视图可见 = $("queueview").style.display !== "none";
  out.文章库已隐藏 = $("lib").style.display === "none";
  out.队列行数 = rows().length;
  out.侧边栏计数 = $("navqcount").textContent.trim();
  out.输入框已清空 = $("url").value === "";
  out.状态徽章文案 = rows().map((r) => r.querySelector(".qstate").textContent.trim());
  if (!out.队列视图可见) fails.push("点「加入队列」后没有切到队列视图");
  if (out.队列行数 !== 3) fails.push(`队列里应有 3 条，实际 ${out.队列行数}`);
  if (out.输入框已清空 !== true) fails.push("入队后首页输入框应被清空");

  const file = readQueue();
  out.落盘条数 = file.items.length;
  out.落盘链接 = file.items.map((i) => i.url);
  out.选项快照 = file.items.map((i) => JSON.stringify(i.opts || {}));
  if (out.落盘条数 !== 3) fails.push(`queue.json 里应有 3 条，实际 ${out.落盘条数}`);
  if (!file.items.every((i) => LINKS.includes(i.url))) fails.push(`落盘的链接不对：${out.落盘链接}`);
  if (!file.items.every((i) => (i.opts || {}).mode)) fails.push("入队时应把首页的选项（mode）快照下来");

  // ---------- 3) 用受控数据测排序 / 删除（不触发真实运行）
  const mk = (n, url) => ({
    id: "q" + n, url, pick: 1, title: "第 " + n + " 条", source: "manual",
    state: "pending", opts: {}, added_at: 1700000000 + n, started_at: null,
    finished_at: null, dir: null, error: "",
  });
  writeQueue([mk(1, "http://127.0.0.1:9/a"), mk(2, "http://127.0.0.1:9/b"), mk(3, "http://127.0.0.1:9/c")]);
  await window.loadQueue();
  out.受控_初始顺序 = urls();

  window.queueMove(queueId("q3"), -1);
  await until(() => urls().join() !== out.受控_初始顺序.join(), 5000, 100);
  out.下移后顺序 = urls();
  out.下移后文件顺序 = readQueue().items.map((i) => i.url);
  if (out.下移后顺序[1] !== "http://127.0.0.1:9/c") {
    fails.push(`上移一条应改变顺序，期望第 2 位是 /c，实际 ${JSON.stringify(out.下移后顺序)}`);
  }

  window.queueRemove(queueId("q1"));
  await until(() => rows().length === 2, 5000, 100);
  out.删除后行数 = rows().length;
  out.删除后文件条数 = readQueue().items.length;
  if (out.删除后行数 !== 2) fails.push(`删除一条后应剩 2 行，实际 ${out.删除后行数}`);
  if (out.删除后文件条数 !== 2) fails.push(`删除后 queue.json 应剩 2 条，实际 ${out.删除后文件条数}`);

  // ---------- 4) 重复链接不会二次入队
  $("url").value = "http://127.0.0.1:9/b";
  $("url").dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(150);
  $("go").click();
  await sleep(1200);
  out.重复入队后条数 = readQueue().items.length;
  out.重复入队后提示 = $("toast").textContent.trim().slice(0, 24);
  if (out.重复入队后条数 !== 2) fails.push(`已在队列里的链接不应重复入队，实际 ${out.重复入队后条数} 条`);

  // ---------- 5) 空队列：显示空状态
  writeQueue([]);
  await window.loadQueue();
  await sleep(200);
  out.空队列_行数 = rows().length;
  out.空队列_提示可见 = $("empty-queue").style.display !== "none";
  out.空队列_侧边栏计数 = $("navqcount").textContent.trim();
  if (out.空队列_提示可见 !== true) fails.push("队列为空时应显示空状态提示");
  if (out.空队列_侧边栏计数 !== "") fails.push(`队列为空时侧边栏计数应为空，实际「${out.空队列_侧边栏计数}」`);

  // ---------- 6) 失败条目能被「重试失败」退回排队（走真实接口）
  writeQueue([
    { ...mk(1, "http://127.0.0.1:9/x"), state: "error", error: "模拟失败" },
    { ...mk(2, "http://127.0.0.1:9/y"), state: "done", dir: "__UI测试单集" },
  ]);
  await window.loadQueue();
  out.失败_状态徽章 = rows().map((r) => r.querySelector(".qstate").textContent.trim());
  out.失败_错误文案 = (rows()[0].querySelector(".qerr") || { textContent: "" }).textContent.trim();
  if (out.失败_错误文案 !== "模拟失败") fails.push("失败条目应把错误信息显示出来");

  window.queueRetry();
  // 注意：界面的「重试失败」在退回 pending 之后会自动开跑（queueRetry → queueRun），
  // 而这里造的是不可达地址，所以条目很快又会变成 error。因此只断言「不再是那条旧错误」。
  await until(() => {
    const it = readQueue().items.find((i) => i.id === "q1") || {};
    return it.state !== "error" || (it.error || "") !== "模拟失败";
  }, 5000, 100);
  const retried = readQueue().items.find((i) => i.id === "q1") || {};
  out.重试后状态 = retried.state;
  out.重试后错误 = (retried.error || "").slice(0, 40);
  if (retried.state === "error" && (retried.error || "") === "模拟失败") {
    fails.push("「重试失败」没有把条目退回排队（状态与错误信息都没变）");
  }

  // done 的条目：清空时会被清掉，pending 必须留着（用受控数据，避免被自动开跑干扰）
  writeQueue([
    { ...mk(1, "http://127.0.0.1:9/p"), state: "pending" },
    { ...mk(2, "http://127.0.0.1:9/d"), state: "done", dir: "__UI测试单集" },
    { ...mk(3, "http://127.0.0.1:9/e"), state: "error", error: "旧的失败" },
  ]);
  await fetch(BASE + "/api/queue/clear", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
  });
  out.清空后文件状态 = readQueue().items.map((i) => i.state);
  if (out.清空后文件状态.includes("done")) fails.push("「清空已结束」应清掉 done 条目");
  if (out.清空后文件状态.includes("error")) fails.push("「清空已结束」默认应清掉 error 条目");
  if (!out.清空后文件状态.includes("pending")) fails.push("「清空已结束」绝不能删掉还在排队的条目");

  // keep_failed=true 时失败的留着
  writeQueue([
    { ...mk(1, "http://127.0.0.1:9/e"), state: "error", error: "旧的失败" },
    { ...mk(2, "http://127.0.0.1:9/d"), state: "done" },
  ]);
  await fetch(BASE + "/api/queue/clear", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep_failed: true }),
  });
  out.保留失败_文件状态 = readQueue().items.map((i) => i.state);
  if (out.保留失败_文件状态.join() !== "error") {
    fails.push(`keep_failed=true 时应只清掉 done，实际 ${JSON.stringify(out.保留失败_文件状态)}`);
  }

  // ---------- 8) 粘贴分享文案（带标题、序号、句末标点）：只认链接
  // 用户真实遇到的形态：B 站/小宇宙的分享按钮复制出来是「【标题】https://…」，
  // 以前整行不是一个链接就识别不出来。
  const titled = [
    "【赫拉利警示：AI正在悄然接管人类世界。】http://127.0.0.1:9/titled-one",
    "2. 另一期节目 http://127.0.0.1:9/titled-two 讲得不错",
  ];
  const WANT = "http://127.0.0.1:9/titled-one,http://127.0.0.1:9/titled-two";
  out.抠链接_带标题 = window.urlsIn(titled.join("\n"));
  out.抠链接_句末标点 = window.urlsIn("看这个 http://127.0.0.1:9/x。 挺有意思");
  out.抠链接_连写多条 = window.urlsIn("http://127.0.0.1:9/a，http://127.0.0.1:9/b");
  out.抠链接_本地路径 = window.urlsIn("/Users/me/录音.m4a");
  if (out.抠链接_带标题.join() !== WANT) {
    fails.push(`带标题的粘贴应只抠出链接，实际 ${JSON.stringify(out.抠链接_带标题)}`);
  }
  if (out.抠链接_句末标点.join() !== "http://127.0.0.1:9/x") {
    fails.push(`链接末尾的句号应被去掉，实际 ${JSON.stringify(out.抠链接_句末标点)}`);
  }
  if (out.抠链接_连写多条.join() !== "http://127.0.0.1:9/a,http://127.0.0.1:9/b") {
    fails.push(`一行里连写的多条链接应拆开，实际 ${JSON.stringify(out.抠链接_连写多条)}`);
  }
  if (out.抠链接_本地路径.join() !== "/Users/me/录音.m4a") {
    fails.push(`本机路径不该被当成链接抠坏，实际 ${JSON.stringify(out.抠链接_本地路径)}`);
  }

  // 首页一次粘两段带标题的文案 → 按钮显示 2 条，入队的是纯链接
  clearQueue();
  await window.loadQueue();
  $("url").value = titled.join("\n");
  $("url").dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(200);
  out.按钮文案_带标题 = $("go").textContent.trim();
  // 前面的小节起过一个真任务，任务在跑时首页按钮是禁用的（禁用按钮的 click 不触发）——
  // 这里等它跑完再做这一段的断言，不然测的是「按钮禁用」而不是「抠链接」
  out.按钮可点 = await until(() => !$("go").disabled, 15000, 150);
  $("go").click();
  await until(() => readQueue().items.length === 2, 8000, 150);
  out.落盘链接_带标题 = readQueue().items.map((i) => i.url);
  if (!out.按钮文案_带标题.includes("2")) {
    fails.push(`两段带标题的文案应识别成 2 条链接，按钮是「${out.按钮文案_带标题}」`);
  }
  if (!out.按钮可点) fails.push("前面的任务一直没结束，按钮始终是禁用的（测试环境问题）");
  if (out.落盘链接_带标题.join() !== WANT) {
    fails.push(`入队应是纯链接，实际 ${JSON.stringify(out.落盘链接_带标题)}`);
  }

  // 批量弹窗走的是服务端解析（整段文本原样提交）：带说明句、没有链接的行都要忽略
  clearQueue();
  await window.loadQueue();
  window.openBatch();
  await sleep(250);
  $("batchtext").value = titled.join("\n") + "\n这句没有链接，应该被忽略";
  $("modalok").click();
  await until(() => readQueue().items.length === 2, 8000, 150);
  out.弹窗落盘链接 = readQueue().items.map((i) => i.url);
  if (out.弹窗落盘链接.join() !== WANT) {
    fails.push(`批量弹窗里整段粘贴也应只入队链接，实际 ${JSON.stringify(out.弹窗落盘链接)}`);
  }

  clearQueue();   // 收尾：别把临时目录里的队列留给下一个测试
  report("批量队列全部通过", out, fails);
})();
