/**
 * 手机端高频交互回归：导航、焦点、设置入口、搜索和阅读页低频操作。
 *
 * 这些用例只断言可观察的用户行为。布局尺寸仍由真实浏览器检查；jsdom
 * 没有排版引擎，所以这里把窗口宽度设为 390px，只覆盖手机分支的交互。
 */
const { boot, report, sleep, until } = require("./harness");

const MOBILE_WIDTH = 390;
const DESKTOP_WIDTH = 1280;

function setWidth(window, width) {
  Object.defineProperty(window, "innerWidth", { value: width, configurable: true });
}

function visible(page, id) {
  return page.$(id)?.style.display !== "none";
}

function optionValues(select) {
  return select ? [...select.options].map((option) => option.value) : [];
}

function authIdentity(role) {
  const admin = role === "admin";
  return {
    authenticated: true,
    id: admin ? "admin-race-id" : "member-race-id",
    username: admin ? "race-admin" : "race-member",
    role,
    must_change_password: false,
    quota: {
      month_key: "2026-10",
      asr: { limit_seconds: 7200, used_seconds: 0, reserved_seconds: 0, remaining_seconds: 7200 },
      llm: { limit_cny: "10.00", used_cny: "0.00", reserved_cny: "0.00", remaining_cny: "10.00" },
      cache_bytes: 1073741824, queue_items: 3, max_upload_bytes: 209715200,
    },
    services: { personal_llm_active: false, personal_asr_active: false },
  };
}

function authResponse(identity) {
  return { ok: true, status: 200, json: async () => identity };
}

async function closePage(page) {
  // 页面初始化会并行拉取设置、用量、队列、订阅等数据。显式等待这些真实入口
  // 收敛，再关闭 jsdom，避免未完成 promise 在 window.close() 后访问 document。
  const settle = ["loadSettings", "loadServerConfig", "loadLibrary", "loadQueue", "loadFeeds",
    "loadSearchService", "pollCurrentJob"];
  await Promise.allSettled(settle.map((name) => {
    const fn = page.window[name];
    return typeof fn === "function" ? fn() : null;
  }));
  // loadUsageBox 是设置加载过程里启动的独立请求，继续等它把占位内容替换掉。
  await until(() => {
    const box = page.$("usagebox");
    if (!box) return true;
    const text = box.textContent.trim();
    return !!text && !text.includes("正在统计");
  }, 6000);
  await sleep(20);
  page.window.close();
}

(async () => {
  const out = {}, fails = [];
  const check = (name, pass, detail = "") => {
    out[name] = !!pass;
    if (!pass) fails.push(detail ? `${name}: ${detail}` : name);
  };

  try {
    // ---------- 1) 手机侧边栏进入设置后，抽屉和遮罩必须一起收起
    const navigation = await boot({
      beforeParse(window) { setWidth(window, MOBILE_WIDTH); },
    });
    const side = navigation.$("appside");
    const appmain = navigation.doc.querySelector(".appmain");
    const mobileMenu = navigation.$("mobile-menu") || navigation.doc.querySelector(".topbar .menubtn");
    const backdrop = () => navigation.doc.querySelector(".sideback");
    check("手机抽屉初始关闭状态可访问", mobileMenu?.getAttribute("aria-expanded") === "false");
    check("手机抽屉关闭时自身不可聚焦", side.inert === true);
    check("手机抽屉关闭时主界面保持可用", appmain?.inert === false);
    navigation.window.toggleSide();
    await until(() => side.classList.contains("open"));
    check("手机抽屉打开状态同步到菜单按钮", mobileMenu?.getAttribute("aria-expanded") === "true");
    check("手机抽屉打开后抽屉可聚焦", side.inert === false);
    check("手机抽屉打开后背景主界面不可聚焦", appmain?.inert === true);
    navigation.click(navigation.doc.querySelector(".sidefoot .folder"));
    await until(() => visible(navigation, "settings"), 4000);
    await sleep(120);
    const settingsBack = navigation.$("settings-back");
    check("进入设置后焦点落在设置页返回按钮", !!settingsBack
      && navigation.doc.activeElement === settingsBack
      && settingsBack.tagName === "BUTTON", "焦点仍停留在侧栏或 body");
    check("手机侧栏进入设置会关闭抽屉", !side.classList.contains("open"), "#appside 仍处于 open");
    check("手机侧栏进入设置会关闭遮罩", !backdrop()?.classList.contains("open"), ".sideback 仍处于 open");

    // 关闭按钮在品牌名内部，不能冒泡成「回到文章库」动作。
    navigation.window.toggleSide();
    await until(() => side.classList.contains("open"));
    const sidehide = navigation.doc.querySelector(".appbrand .sidehide");
    if (sidehide) navigation.click(sidehide);
    await sleep(100);
    check("设置页点击抽屉关闭按钮不会离开设置", visible(navigation, "settings") && !visible(navigation, "main"),
      "设置页被切回文章库");
    navigation.window.closeSettings();
    await closePage(navigation);

    const desktopNavigation = await boot({
      beforeParse(window) { setWidth(window, DESKTOP_WIDTH); },
    });
    desktopNavigation.window.syncSideState();
    check("桌面侧栏不被 inert", desktopNavigation.$("appside").inert === false);
    check("桌面菜单状态保持关闭", desktopNavigation.$("mobile-menu")?.getAttribute("aria-expanded") === "false");
    await closePage(desktopNavigation);

    // ---------- 2) 手机端打开助手和关闭阅读页不应自动弹出键盘
    const mobileReader = await boot({
      beforeParse(window) { setWidth(window, MOBILE_WIDTH); },
    });
    // 用「生成后直接打开」的真实入口，关闭阅读页时页面原本会尝试把焦点放回首页输入框。
    await mobileReader.window.showArticle("__UI测试单集", { fromLib: false, route: false });
    await until(() => mobileReader.$("result").classList.contains("show"), 4000);
    mobileReader.$("aq").blur();
    mobileReader.$("url").blur();
    mobileReader.$("fab").focus();
    mobileReader.click(mobileReader.$("fab"));
    await sleep(120);
    check("手机打开助手不自动聚焦输入框", mobileReader.doc.activeElement !== mobileReader.$("aq"),
      "打开阅读助手时直接聚焦了 #aq");
    const mobileAssistFocus = mobileReader.doc.activeElement;
    check("手机打开助手把焦点放到助手按钮", mobileAssistFocus?.closest("#assist") === mobileReader.$("assist")
      && mobileAssistFocus.tagName === "BUTTON", "助手打开后没有可见的非文本焦点");
    check("手机打开助手会隔离阅读背景", mobileReader.$("result").inert === true);
    mobileReader.window.closeAssist();
    check("手机关闭助手恢复阅读背景", mobileReader.$("result").inert === false);
    check("手机关闭助手把焦点还给悬浮按钮", mobileReader.doc.activeElement === mobileReader.$("fab"));

    mobileReader.$("url").blur();
    mobileReader.window.playAt(0, null, "__UI测试单集");
    check("打开阅读页后播放器可见", mobileReader.$("player").classList.contains("show"));
    mobileReader.window.closeResult({ keepRoute: true });
    check("手机关闭生成结果不自动聚焦链接框", mobileReader.doc.activeElement !== mobileReader.$("url"),
      "关闭阅读页时直接聚焦了 #url，手机会弹出键盘");
    check("关闭阅读页会收起播放器", !mobileReader.$("player").classList.contains("show"),
      "关闭阅读页后 #player 仍处于 show");
    await closePage(mobileReader);

    // 桌面保留原来的快捷体验：打开助手仍自动聚焦输入框。
    const desktopReader = await boot({
      beforeParse(window) { setWidth(window, DESKTOP_WIDTH); },
    });
    await desktopReader.window.openEpisode(encodeURIComponent("__UI测试单集"));
    await until(() => desktopReader.$("result").classList.contains("show"), 4000);
    desktopReader.$("aq").blur();
    desktopReader.$("fab").focus();
    desktopReader.click(desktopReader.$("fab"));
    await sleep(120);
    check("桌面打开助手仍自动聚焦输入框", desktopReader.doc.activeElement === desktopReader.$("aq"),
      "桌面端助手没有聚焦 #aq");
    check("桌面打开助手不隔离阅读背景", desktopReader.$("result").inert === false);
    await closePage(desktopReader);

    // ---------- 3) 手机设置页使用下拉选择器，且管理员选项完整
    const settings = await boot({
      beforeParse(window) { setWidth(window, MOBILE_WIDTH); },
    });
    await settings.window.openSettings();
    await until(() => visible(settings, "settings"), 4000);
    const section = settings.$("settings-section");
    check("手机设置页有分类选择器", !!section && section.tagName === "SELECT", "缺少 #settings-section");
    const allowedAdminSections = [
      "profile", "gen", "keys", "subs", "assist", "tts", "dest", "memory", "mcp", "storage", "invites",
    ];
    const values = optionValues(section);
    check("管理员设置分类完整", allowedAdminSections.every((value) => values.includes(value)),
      `当前选项为 ${JSON.stringify(values)}`);
    if (section) {
      section.value = "keys";
      section.dispatchEvent(new settings.window.Event("change", { bubbles: true }));
      await sleep(80);
      check("设置选择器切换到 API 服务", settings.doc.querySelector('.settings-nav .tab[data-tab="keys"]')?.dataset.active === "true"
        && visible(settings, "pane-keys"), "选择 keys 后没有切换到 API 服务");
      settings.window.switchTab("assist");
      await sleep(50);
      check("设置页签切换会同步选择器", section.value === "assist", `选择器仍为 ${section.value}`);
    }
    await closePage(settings);

    // 身份返回较慢时，不能先把管理员页签渲染出来，也不能在身份恢复后留下空白 MCP 页。
    let releaseMember;
    const raceMember = await boot({ beforeParse(window) {
      const originalFetch = window.fetch;
      window.fetch = (url, options) => String(url).endsWith("/api/auth/me")
        ? new Promise(resolve => { releaseMember = () => resolve(authResponse(authIdentity("member"))); })
        : originalFetch(url, options);
    }});
    await until(() => typeof releaseMember === "function", 4000);
    await raceMember.window.openSettings("mcp");
    await until(() => visible(raceMember, "settings"), 4000);
    releaseMember();
    await until(() => raceMember.$("account-role")?.textContent.includes("个人账号"), 4000);
    await sleep(120);
    const raceMemberValues = optionValues(raceMember.$("settings-section"));
    check("延迟身份恢复后成员设置不含 MCP", !raceMemberValues.includes("mcp"), `当前选项为 ${JSON.stringify(raceMemberValues)}`);
    check("延迟身份恢复后成员设置不含邀请", !raceMemberValues.includes("invites"), `当前选项为 ${JSON.stringify(raceMemberValues)}`);
    check("延迟身份恢复后成员页签回到个人资料", raceMember.$("pane-profile")?.style.display === "block"
      && raceMember.$("pane-mcp")?.style.display !== "block", "当前设置页为空白或仍停在管理员页签");
    await closePage(raceMember);

    let releaseAdmin;
    const raceAdmin = await boot({ beforeParse(window) {
      const originalFetch = window.fetch;
      window.fetch = (url, options) => String(url).endsWith("/api/auth/me")
        ? new Promise(resolve => { releaseAdmin = () => resolve(authResponse(authIdentity("admin"))); })
        : originalFetch(url, options);
    }});
    await until(() => typeof releaseAdmin === "function", 4000);
    await raceAdmin.window.openSettings("mcp");
    await until(() => visible(raceAdmin, "settings"), 4000);
    releaseAdmin();
    await until(() => raceAdmin.$("account-role")?.textContent.includes("管理员账号"), 4000);
    await sleep(120);
    const raceAdminValues = optionValues(raceAdmin.$("settings-section"));
    check("延迟身份恢复后管理员设置含 MCP", raceAdminValues.includes("mcp"), `当前选项为 ${JSON.stringify(raceAdminValues)}`);
    check("延迟身份恢复后管理员设置含邀请", raceAdminValues.includes("invites"), `当前选项为 ${JSON.stringify(raceAdminValues)}`);
    await closePage(raceAdmin);

    // 普通成员仍可使用个人设置，但不能从手机选择器进入管理员功能。
    const member = await boot({ beforeParse(window) {
      const originalFetch = window.fetch;
      window.fetch = (url, options) => String(url).endsWith("/api/auth/me")
        ? Promise.resolve({ ok: true, status: 200, json: async () => ({
          authenticated: true,
          id: "member-test-id",
          username: "friend",
          role: "member",
          must_change_password: false,
          quota: {
            month_key: "2026-10",
            asr: { limit_seconds: 7200, used_seconds: 0, reserved_seconds: 0, remaining_seconds: 7200 },
            llm: { limit_cny: "10.00", used_cny: "0.00", reserved_cny: "0.00", remaining_cny: "10.00" },
            cache_bytes: 1073741824, queue_items: 3, max_upload_bytes: 209715200,
          },
          services: { personal_llm_active: false, personal_asr_active: false },
        }) })
        : originalFetch(url, options);
    }});
    await member.window.openSettings();
    await until(() => visible(member, "settings"), 4000);
    const memberSection = member.$("settings-section");
    const memberValues = optionValues(memberSection);
    check("普通成员设置不包含邀请功能", !memberValues.includes("invites"), `当前选项为 ${JSON.stringify(memberValues)}`);
    check("普通成员设置不包含 MCP 管理", !memberValues.includes("mcp"), `当前选项为 ${JSON.stringify(memberValues)}`);
    check("普通成员仍能看到个人 API 设置", memberValues.includes("keys") && !member.$("pane-keys").hidden);
    await closePage(member);

    // ---------- 4) 手机搜索入口打开抽屉、聚焦搜索框，回车后收起抽屉
    const search = await boot({
      beforeParse(window) { setWidth(window, MOBILE_WIDTH); },
    });
    const mobileSearch = search.$("mobile-search");
    check("手机顶栏有搜索入口", !!mobileSearch, "缺少 #mobile-search");
    if (mobileSearch) {
      search.click(mobileSearch);
      await until(() => search.$("appside").classList.contains("open"));
      check("点击手机搜索入口会打开抽屉", search.$("appside").classList.contains("open"));
      check("打开手机搜索入口会聚焦搜索框", search.doc.activeElement === search.$("q"), "#q 没有获得焦点");
      search.$("q").value = "测试";
      search.$("q").dispatchEvent(new search.window.Event("input", { bubbles: true }));
      search.$("q").dispatchEvent(new search.window.KeyboardEvent("keydown", {
        key: "Enter", bubbles: true, cancelable: true,
      }));
      await until(() => !search.$("appside").classList.contains("open"), 4000);
      check("手机搜索回车后收起抽屉", !search.$("appside").classList.contains("open"));
    }
    await closePage(search);

    // ---------- 5) 阅读页低频操作收进 details 后仍然可用
    const readerMore = await boot({
      beforeParse(window) { setWidth(window, MOBILE_WIDTH); },
    });
    await readerMore.window.openEpisode(encodeURIComponent("__UI测试单集"));
    await until(() => readerMore.$("result").classList.contains("show"), 4000);
    const more = readerMore.$("reader-more");
    check("阅读页有低频操作折叠区", !!more && more.tagName === "DETAILS", "缺少 #reader-more");
    if (more) {
      check("手机阅读页低频操作默认收起", more.open === false, "手机端 #reader-more 默认应收起");
      more.open = true;
      const exportButton = readerMore.$("expbtn");
      const statusButton = readerMore.$("readbtn");
      check("导出按钮位于低频操作折叠区", !!exportButton && more.contains(exportButton));
      check("状态按钮位于低频操作折叠区", !!statusButton && more.contains(statusButton));
      if (exportButton) {
        readerMore.click(exportButton);
        await sleep(30);
        check("折叠区里的导出按钮仍可用", !readerMore.$("exportmenu").hidden);
        readerMore.window.hidePopmenus();
      }
      if (statusButton) {
        readerMore.click(statusButton);
        await sleep(30);
        check("折叠区里的状态按钮仍可用", !readerMore.$("readmenu").hidden);
        readerMore.window.hidePopmenus();
      }
    }
    await closePage(readerMore);

    const desktopMore = await boot({
      beforeParse(window) { setWidth(window, DESKTOP_WIDTH); },
    });
    await desktopMore.window.openEpisode(encodeURIComponent("__UI测试单集"));
    await until(() => desktopMore.$("result").classList.contains("show"), 4000);
    check("桌面阅读页低频操作默认展开", desktopMore.$("reader-more")?.open === true,
      "桌面端 #reader-more 默认应展开");
    await closePage(desktopMore);
  } catch (error) {
    out.error = String(error && error.stack || error);
    fails.push("手机端交互回归测试抛出异常");
  }
  report("手机端交互回归", out, fails);
})();
