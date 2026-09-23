(() => {
  "use strict";
  const page = location.pathname === "/invite" ? "invite" : "login";
  const rawFragment = page === "invite" ? location.hash.slice(1) : "";
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  let inviteToken = "";
  try { inviteToken = decodeURIComponent(rawFragment); } catch (_) { inviteToken = ""; }

  const byId = (id) => document.getElementById(id);
  const setBusy = (form, busy) => {
    const button = form.querySelector("button[type=submit]");
    if (button) button.disabled = busy;
  };
  const setError = (id, message) => { const node = byId(id); if (node) node.textContent = message; };
  async function post(path, payload) {
    const csrfResponse = await fetch("/api/auth/csrf", { credentials: "same-origin" });
    if (!csrfResponse.ok) throw new Error("暂时无法连接，请刷新页面后重试。");
    const csrf = await csrfResponse.json();
    return fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf.csrf_token || "" },
      body: JSON.stringify(payload),
    });
  }
  async function responseData(response) {
    try { return await response.json(); } catch (_) { return {}; }
  }

  byId(page === "invite" ? "invite-view" : "login-view").hidden = false;
  if (page === "invite") {
    byId("page-eyebrow").textContent = "INVITATION ONLY";
    byId("manifest-title").innerHTML = "一个账号，<br>一份独立空间。";
  }

  byId("login-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    setError("login-error", "");
    setBusy(form, true);
    try {
      const response = await post("/api/auth/login", {
        username: byId("username").value,
        password: byId("password").value,
        next: new URLSearchParams(location.search).get("next"),
      });
      const data = await responseData(response);
      if (!response.ok) throw new Error(response.status === 429 ? "尝试次数过多，请稍后再试。" : "用户名或密码错误，请重试。");
      location.assign(data.must_change_password ? "/login?must_change=1" : (data.next || "/"));
    } catch (error) {
      setError("login-error", error.message || "暂时无法连接，请刷新页面后重试。");
    } finally { setBusy(form, false); }
  });

  byId("invite-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    setError("invite-error", "");
    if (!inviteToken) { setError("invite-error", "邀请链接无效或已使用。请联系邀请你的人获取新链接。"); return; }
    const token = inviteToken;
    inviteToken = "";
    setBusy(form, true);
    try {
      const response = await post("/api/auth/register", {
        invite_token: token,
        username: byId("invite-username").value,
        password: byId("invite-password").value,
      });
      const data = await responseData(response);
      if (!response.ok) throw new Error(data.message || "邀请链接无效或已过期，请联系邀请你的人。");
      location.assign("/");
    } catch (error) {
      setError("invite-error", error.message || "暂时无法连接，请刷新页面后重试。");
    } finally { setBusy(form, false); }
  });

  byId("password-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    setError("password-error", "");
    setBusy(form, true);
    try {
      const response = await post("/api/auth/password", {
        current_password: byId("current-password").value,
        new_password: byId("new-password").value,
      });
      const data = await responseData(response);
      if (!response.ok) throw new Error(data.message || "无法更新密码，请检查输入后重试。");
      location.assign("/");
    } catch (error) {
      setError("password-error", error.message || "暂时无法连接，请刷新页面后重试。");
    } finally { setBusy(form, false); }
  });

  if (page === "login" && new URLSearchParams(location.search).get("must_change") === "1") {
    byId("login-view").hidden = true;
    byId("password-view").hidden = false;
  }
  window.__loginPageReady = true;
})();
