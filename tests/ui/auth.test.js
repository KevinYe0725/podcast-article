const assert = require("node:assert/strict");
const { boot, until, report, BASE } = require("./harness");

(async () => {
  const fails = [];
  const out = {};
  try {
    const page = await boot({ url: `${BASE}/login` });
    out.loginFormPresent = !!page.$("login-form");
    page.$("username").value = "alice";
    page.$("password").value = "wrong passphrase";
    page.$("login-form").dispatchEvent(new page.window.Event("submit", { bubbles: true, cancelable: true }));
    out.invalidLoginGeneric = await until(() => !!page.$("login-error")?.textContent.trim());
    if (!out.invalidLoginGeneric) fails.push("invalid login did not show a generic error");
    page.window.close();

    let registerCalls = 0;
    let sentToken = "";
    const invite = await boot({
      url: `${BASE}/invite#one-time-test-token`,
      beforeParse(w) {
        const originalFetch = w.fetch;
        w.fetch = async (url, options = {}) => {
          if (String(url).endsWith("/api/auth/csrf")) return { ok: true, json: async () => ({ csrf_token: "csrf-test" }) };
          if (String(url).endsWith("/api/auth/register")) {
            registerCalls += 1;
            sentToken = JSON.parse(options.body).invite_token;
            return { ok: false, status: 400, json: async () => ({ message: "invite rejected" }) };
          }
          return originalFetch(url, options);
        };
      },
    });
    out.inviteFragmentRemoved = invite.window.location.hash === "";
    invite.$("invite-username").value = "alice";
    invite.$("invite-password").value = "a sufficiently long passphrase";
    invite.$("invite-form").dispatchEvent(new invite.window.Event("submit", { bubbles: true, cancelable: true }));
    await until(() => registerCalls > 0);
    out.invitePostedOnce = registerCalls === 1 && sentToken === "one-time-test-token";
    if (!out.inviteFragmentRemoved) fails.push("invite token remained in the browser URL");
    if (!out.invitePostedOnce) fails.push("invite token was not posted exactly once");
    invite.window.close();

    const app = await boot();
    out.accountRendered = await until(() => !!app.$("account-menu") && !app.$("account-menu").hidden);
    if (!out.accountRendered) fails.push("authenticated account menu was not rendered");
    const accountText = app.$("account-menu")?.textContent || "";
    out.noProviderSecrets = !/sk-[A-Za-z0-9]|NOTION_TOKEN|TTS_API_KEY/.test(accountText);
    if (!out.noProviderSecrets) fails.push("account UI exposed provider secrets");
    out.quotaRendered = /本月转写/.test(app.$("account-quota")?.textContent || "") && /本月 AI 写作/.test(app.$("account-quota")?.textContent || "");
    if (!out.quotaRendered) fails.push("monthly ASR and LLM usage were not rendered");
    app.window.close();

    const member = await boot({ beforeParse(w) {
      const originalFetch = w.fetch;
      w.fetch = (url, options) => String(url).endsWith("/api/auth/me")
        ? Promise.resolve({ ok: true, status: 200, json: async () => ({
          authenticated: true, username: "friend", role: "member", quota: { asr: {}, llm: {} },
        }) })
        : originalFetch(url, options);
    }});
    out.memberControlsHidden = await until(() => !!member.$("account-menu") && !member.$("account-menu").hidden)
      && member.$("pane-keys").hidden && member.$("pane-mcp").hidden;
    if (!out.memberControlsHidden) fails.push("member can see admin service-key or MCP controls");
    member.window.close();

    const expiredApp = await boot({ beforeParse(w) {
      const originalFetch = w.fetch;
      w.__return401 = false;
      w.fetch = (url, options) => w.__return401 && String(url).endsWith("/api/library")
        ? Promise.resolve({ ok: false, status: 401, json: async () => ({ error: "authentication_required" }) })
        : originalFetch(url, options);
    }});
    expiredApp.window.localStorage.setItem("pa.test.private", "state");
    expiredApp.window.__return401 = true;
    const response = await expiredApp.window.fetch("/api/library");
    out.expiredSessionHandled = response.status === 401 && expiredApp.window.localStorage.length === 0;
    if (!out.expiredSessionHandled) fails.push("401 did not clear local app state");
    out.expiredSessionRedirected = expiredApp.navigationAttempts.some((message) => message.includes("navigation"));
    if (!out.expiredSessionRedirected) fails.push("401 did not redirect to login");
    expiredApp.window.close();
  } catch (error) {
    out.error = String(error && error.stack || error);
    fails.push("auth test threw");
  }
  report("登录与账号界面", out, fails);
})();
