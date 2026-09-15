const { boot, report, sleep } = require("./harness");
const BASE = process.env.PA_BASE || "http://127.0.0.1:8787";
(async () => {
  const out = {}, fails = [];
  let prompted = 0, confirmed = 0;
  const { window, doc, $ } = await boot({
    beforeParse(w) {
      w.prompt = () => { prompted++; return ""; };      // 若还在用原生弹窗就会被计数
      w.confirm = () => { confirmed++; return true; };
    },
  });
  const key = (k) => $("modalinput").dispatchEvent(new window.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }));

  // 1) 打开新建弹窗
  [...doc.querySelectorAll("#catnav .folder")].find((c) => c.textContent.includes("新建分类")).click();
  await sleep(200);
  out.弹窗出现 = $("modal").classList.contains("show");
  out.标题 = $("modaltitle").textContent;
  out.有输入框 = $("modalinput").style.display === "block";
  out.输入框已聚焦 = doc.activeElement === $("modalinput");
  out.聚焦的是页面元素 = doc.activeElement.tagName;
  if (!out.弹窗出现) fails.push("新建弹窗没出现");

  // 2) 空值不提交
  key("Enter"); await sleep(150);
  out.空值_弹窗仍在 = $("modal").classList.contains("show");
  if (!out.空值_弹窗仍在) fails.push("空值竟然提交了");

  // 3) 正常提交
  $("modalinput").value = "AI 技术";
  key("Enter"); await sleep(1500);
  out.提交后_弹窗关闭 = !$("modal").classList.contains("show");
  const cats1 = await (await fetch(BASE + "/api/categories")).json();
  out.已创建 = cats1.categories.some((c) => c.name === "AI 技术");
  if (!out.提交后_弹窗关闭 || !out.已创建) fails.push("创建流程失败");

  // 4) Esc 关闭
  window.createCat(); await sleep(200);
  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await sleep(200);
  out.Esc关闭 = !$("modal").classList.contains("show");
  if (!out.Esc关闭) fails.push("Esc 关不掉弹窗");

  // 5) 点遮罩关闭
  window.createCat(); await sleep(200);
  $("modal").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(200);
  out.遮罩关闭 = !$("modal").classList.contains("show");
  if (!out.遮罩关闭) fails.push("点遮罩关不掉弹窗");

  // 6) 重命名：输入框预填
  const cat = cats1.categories.find((c) => c.name === "AI 技术");
  const editBtn = [...doc.querySelectorAll("#catnav .folder .fedit span")].find((e) => e.getAttribute("onclick").includes(cat.id) && e.textContent === "✎");
  editBtn.click(); await sleep(300);
  out.重命名_标题 = $("modaltitle").textContent;
  out.重命名_预填 = $("modalinput").value;
  if (out.重命名_预填 !== "AI 技术") fails.push("重命名没预填原名");
  $("modalinput").value = "AI 与工程";
  $("modalok").click(); await sleep(1500);
  const cats2 = await (await fetch(BASE + "/api/categories")).json();
  out.重命名完成 = cats2.categories.some((c) => c.name === "AI 与工程");
  if (!out.重命名完成) fails.push("重命名失败");

  // 7) 删除：危险样式 + 无输入框
  const delBtn = [...doc.querySelectorAll("#catnav .folder .fedit span")].find((e) => e.getAttribute("onclick").includes(cat.id) && e.textContent === "✕");
  delBtn.click(); await sleep(300);
  out.删除_标题 = $("modaltitle").textContent;
  out.删除_无输入框 = $("modalinput").style.display === "none";
  out.删除_按钮样式 = $("modalok").className;
  out.删除_按钮文案 = $("modalok").textContent;
  if (!out.删除_无输入框 || !/danger/.test(out.删除_按钮样式)) fails.push("删除弹窗样式不对");
  $("modalok").click(); await sleep(1500);
  const cats3 = await (await fetch(BASE + "/api/categories")).json();
  out.删除完成 = !cats3.categories.some((c) => c.name === "AI 与工程");
  if (!out.删除完成) fails.push("删除失败");

  out.原生弹窗调用次数 = prompted + confirmed;
  if (prompted + confirmed > 0) fails.push("仍在调用原生弹窗");

  report("同风格模态框全部通过", out, fails);
})();
