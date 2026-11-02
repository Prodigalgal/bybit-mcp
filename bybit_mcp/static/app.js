"use strict";
const $ = (s) => document.querySelector(s),
  $$ = (s) => [...document.querySelectorAll(s)];
const state = { profiles: [], status: null, endpoint: null, preview: null };
const csrf = $('meta[name="csrf-token"]').content;
function node(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = String(text);
  return n;
}
function clear(el) {
  el.replaceChildren();
  return el;
}
function toast(message, error = false) {
  const n = $("#toast");
  n.textContent = message;
  n.classList.toggle("error", error);
  n.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (n.hidden = true), error ? 8500 : 3500);
}
async function api(path, options = {}) {
  const r = await fetch("/admin/api/" + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf,
      ...options.headers,
    },
  });
  let data;
  try {
    data = await r.json();
  } catch {
    throw new Error("服务暂时不可用");
  }
  if (r.status === 401) {
    location.href = "/admin";
    throw new Error("登录已过期");
  }
  if (!r.ok) {
    const message = data.error || data.detail || "请求失败";
    const error = new Error(
      data.code ? `[Bybit ${data.code}] ${message}` : message,
    );
    error.details = data;
    error.status = r.status;
    throw error;
  }
  return data;
}
async function busy(button, fn) {
  if (button?.disabled) return;
  const old = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "处理中…";
  }
  try {
    return await fn();
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = old;
    }
  }
}
function output(selector, data) {
  const n = $(selector);
  n.hidden = false;
  n.textContent = JSON.stringify(data, null, 2);
}
function empty(el, title, body) {
  clear(el);
  const n = node("div", "empty-state");
  n.append(node("span", "", "◇"), node("h3", "", title), node("p", "", body));
  el.append(n);
}
function environmentLabel(env) {
  return (
    { demo: "DEMO 模拟", testnet: "TESTNET", mainnet: "MAINNET 实盘" }[env] ||
    env
  );
}
function connectionLabel(p) {
  return p.public_only ? "主网公开数据 · 免 Key" : environmentLabel(p.environment);
}
function connectionSummary(p) {
  return p.public_only
    ? "行情来源：主网 · 无账户及交易权限"
    : `行情来源：${p.market_data_environment || (p.environment === "demo" ? "mainnet" : p.environment)} · Key 环境：${environmentLabel(p.environment)}`;
}
function profile(id) {
  return state.profiles.find((p) => p.id === id);
}
function selectedProfile(selector) {
  const p = profile($(selector).value);
  if (!p) throw new Error("请先添加并选择一个账户连接");
  return p;
}
function confirmAction(title, message) {
  return new Promise((resolve) => {
    const d = $("#confirm-dialog");
    $("#confirm-title").textContent = title;
    $("#confirm-message").textContent = message;
    d.returnValue = "cancel";
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), {
      once: true,
    });
    d.showModal();
  });
}
function route() {
  const page = location.hash.slice(1) || "overview";
  const valid = [
    "overview",
    "connections",
    "explorer",
    "trading",
    "access",
    "tokens",
  ].includes(page)
    ? page
    : "overview";
  $$("[data-view]").forEach((e) => (e.hidden = e.dataset.view !== valid));
  $$("[data-page]").forEach((e) =>
    e.classList.toggle("active", e.dataset.page === valid),
  );
  $("#page-crumb").textContent = $(`[data-page="${valid}"]`)
    .textContent.replace(/^[^\u4e00-\u9fff]+/, "")
    .trim()
    .replace(/\d+$/, "");
  if (valid === "explorer") loadEndpoints();
  if (valid === "access") loadAccess();
  if (valid === "tokens") window.AuthorizationTokens?.load();
  if (valid === "trading") window.Trading?.activate();
}
async function loadStatus() {
  const s = await api("status");
  state.status = s;
  $("#metric-endpoints").textContent = s.catalog.count;
  $("#metric-demo").textContent = s.catalog.demo_count;
  $("#metric-profiles").textContent = s.profiles;
  $("#metric-grants").textContent = s.grants;
  $("#mcp-url").textContent = s.mcp_url;
  $("#version").textContent = "v" + s.version;
  $("#tool-count").textContent =
    s.tools +
    " 个 · " +
    (s.full_endpoint_tools ? "全部展开" : "常用 + 通用路由");
  $("#catalog-rev").textContent = s.catalog.revision.slice(0, 9);
  $("#verification").textContent = s.verification;
  const groups = $("#endpoint-group");
  for (const g of s.catalog.groups) {
    const o = node("option", "", g);
    o.value = g;
    groups.append(o);
  }
  const scopes = clear($("#scope-options"));
  for (const [key, label] of Object.entries(s.scopes)) {
    const l = node("label", "check");
    const input = node("input");
    input.type = "checkbox";
    input.name = "scopes";
    input.value = key;
    input.checked = ["bybit:read", "bybit:trade"].includes(key);
    l.append(input, document.createTextNode(label));
    scopes.append(l);
  }
  updateConnectionMode();
}
async function loadProfiles() {
  state.profiles = (await api("profiles")).items;
  $("#nav-count").textContent = state.profiles.length;
  $("#metric-profiles").textContent = state.profiles.length;
  const list = clear($("#profiles-list"));
  if (!state.profiles.length)
    empty(list, "还没有连接", "可先创建免 Key 的主网行情连接，或添加对应环境的 API Key。");
  for (const p of state.profiles) {
    const card = node("div", "connection-card"),
      head = node("div", "connection-head");
    head.append(
      node("strong", "", p.name),
      node("span", "pill " + (p.public_only ? "" : p.environment), connectionLabel(p)),
    );
    card.append(
      head,
      node(
        "p",
        "",
        p.key_hint + " · " + (p.enabled ? "连接已启用" : "连接已停用"),
      ),
      node("p", "", connectionSummary(p)),
      node("p", "", p.public_only ? "仅主网公开数据" : p.scopes.join(" / ")),
      node(
        "p",
        "",
        p.public_only ? "账户和交易操作需另选 API Key 连接" : p.usdt_only ? "仅允许 USDT 合约交易" : "按 Bybit API 权限开放产品",
      ),
    );
    const actions = node("div", "connection-actions");
    for (const [text, handler] of [
      ["设置", async () => openConnectionSettings(p)],
      [
        "测试连接",
        () =>
          api(`profiles/${p.id}/test`, { method: "POST" }).then(() =>
            toast(p.public_only ? "主网公开数据连接通过" : "账户验证通过"),
          ),
      ],
      [
        p.enabled ? "停用" : "启用",
        () =>
          api("profiles/" + p.id, {
            method: "PATCH",
            body: JSON.stringify({ enabled: !p.enabled }),
          }).then(loadProfiles),
      ],
      [
        "删除",
        async () => {
          if (
            await confirmAction(
              "删除账户连接",
              p.public_only ? `删除「${p.name}」并撤销其授权？` : `删除「${p.name}」并撤销其 OAuth 授权？该账户的到期撤单将停止执行，请先处理现有委托。`,
            )
          ) {
            await api("profiles/" + p.id, { method: "DELETE" });
            await loadProfiles();
          }
        },
      ],
    ]) {
      const b = node("button", "secondary", text);
      b.type = "button";
      b.onclick = () => busy(b, handler);
      actions.append(b);
    }
    card.append(actions);
    list.append(card);
  }
  for (const select of $$(".profile-select")) {
    const prev = select.value;
    clear(select);
    const enabled = state.profiles.filter((p) => p.enabled);
    const choices =
      select.id === "trade-profile"
        ? enabled.filter((p) => !p.public_only && p.scopes.includes("bybit:trade"))
        : enabled;
    if (!choices.length) {
      const o = node("option", "", "请先添加账户连接");
      o.value = "";
      select.append(o);
    }
    for (const p of choices) {
      const o = node(
        "option",
        "",
        `${p.name} · ${connectionLabel(p)}`,
      );
      o.value = p.id;
      select.append(o);
    }
    if (choices.some((p) => p.id === prev)) select.value = prev;
  }
  updateTradeEnv();
}
function updateTradeEnv() {
  window.Trading?.contextChanged();
}
let searchTimer;
async function loadEndpoints() {
  const q = new URLSearchParams({
    query: $("#endpoint-query").value,
    group: $("#endpoint-group").value,
    demo_only: $("#demo-filter").checked,
    limit: 100,
  });
  const result = await api("catalog?" + q);
  $("#endpoint-total").textContent =
    `找到 ${result.total} 个接口${result.total > 100 ? "，当前显示前 100 个，请使用搜索缩小范围" : ""}`;
  const list = clear($("#endpoint-list"));
  for (const e of result.items) {
    const b = node(
      "button",
      "endpoint-item" + (state.endpoint?.id === e.id ? " selected" : ""),
    );
    const method = node("span", e.method === "GET" ? "" : "post", e.method);
    b.append(method, node("strong", "", e.title), node("small", "", e.path));
    b.onclick = () => busy(b, () => selectEndpoint(e.id));
    list.append(b);
  }
}
async function selectEndpoint(id) {
  const e = await api("endpoint/" + encodeURIComponent(id));
  state.endpoint = e;
  const box = clear($("#endpoint-detail"));
  box.append(
    node("h2", "", e.title),
    node("p", "", e.method + " " + e.path),
    node(
      "span",
      "pill " + (e.demo ? "demo" : ""),
      e.demo ? "DEMO 支持" : "DEMO 不支持",
    ),
    node("p", "small muted", "权限：" + e.scope),
  );
  const a = node("a", "accent-link", "官方文档 ↗");
  a.href = e.docs;
  a.target = "_blank";
  a.rel = "noopener";
  box.append(a);
  const details = node("details"),
    summary = node("summary", "", "查看参数定义"),
    pre = node("pre", "", JSON.stringify(e.schema, null, 2));
  details.append(summary, pre);
  box.append(details);
  $("#explorer-form").hidden = false;
  $("#explorer-result").hidden = true;
  const params = {};
  for (const key of e.schema.required || []) {
    const s = e.schema.properties[key] || {};
    params[key] =
      s.default ??
      s.enum?.[0] ??
      (s.type === "integer"
        ? 1
        : s.type === "boolean"
          ? false
          : s.type === "array"
            ? []
            : s.type === "object"
              ? {}
              : "");
  }
  if (e.schema.properties.category) params.category = "linear";
  if (e.schema.properties.symbol) params.symbol = "XAUUSDT";
  if (e.schema.properties.accountType) params.accountType = "UNIFIED";
  $("#explorer-params").value = JSON.stringify(params, null, 2);
}
async function execute(p, operation, params, ask = true) {
  const mutation = !operation.startsWith("GET ");
  if (
    mutation &&
    ask &&
    !(await confirmAction(
      "确认 " + environmentLabel(p.environment) + " 操作",
      `${p.name}\n${operation}\n${p.environment === "mainnet" ? "本次操作会影响真实账户资产。" : "本次操作将提交至对应模拟 / 测试环境。"}`,
    ))
  )
    return { cancelled: true };
  return api("execute", {
    method: "POST",
    body: JSON.stringify({
      profile_id: p.id,
      operation,
      params,
      confirm_environment: mutation ? p.environment : undefined,
    }),
  });
}
$("#logout").onclick = () =>
  busy($("#logout"), async () => {
    await api("logout", { method: "POST" });
    location.href = "/admin";
  });
$("#copy-url").onclick = () =>
  busy($("#copy-url"), async () => {
    await navigator.clipboard.writeText(state.status.mcp_url);
    toast("MCP 地址已复制");
  });
$("#refresh-profiles").onclick = () =>
  busy($("#refresh-profiles"), loadProfiles);
$("#refresh-audit").onclick = () => busy($("#refresh-audit"), loadAccess);
function updateConnectionMode() {
  const publicOnly = $("#connection-mode").value === "public";
  const env = publicOnly ? "mainnet" : $("#connection-env").value,
    n = $("#connection-note");
  $("#connection-environment-field").hidden = publicOnly;
  $("#connection-env").disabled = publicOnly;
  $("#connection-key-fields").hidden = publicOnly;
  for (const field of [$("#connection-key"), $("#connection-secret")]) {
    field.disabled = publicOnly;
    field.required = !publicOnly;
  }
  $('#connection-form [name="usdt_only"]').disabled = publicOnly;
  $$('#scope-options input').forEach((input) => {
    input.disabled = publicOnly;
    input.closest("label").hidden = publicOnly && input.value !== "bybit:read";
    input.closest("label").lastChild.textContent = publicOnly ? "主网公开数据" : state.status.scopes[input.value];
    if (publicOnly) input.checked = input.value === "bybit:read";
  });
  $("#save-connection").textContent = publicOnly ? "保存主网行情连接 →" : "验证并保存 Key 连接 →";
  n.textContent =
    publicOnly ? "免 Key 连接使用主网公开数据，仅用于行情分析。账户与交易操作需要 API Key。" : env === "mainnet"
      ? "当前为实盘环境，交易与资金操作会影响真实资产，请检查密钥权限。"
      : env === "testnet"
        ? "请使用 testnet.bybit.com 创建的测试网密钥。"
        : "请在 Bybit 主站切换到 Demo Trading，再创建独立 API 密钥。";
  n.classList.toggle("danger", !publicOnly && env === "mainnet");
}
$("#connection-env").onchange = updateConnectionMode;
$("#connection-mode").onchange = updateConnectionMode;
$("#connection-form").onsubmit = (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    const f = new FormData(e.target),
      data = Object.fromEntries(f);
    data.scopes = f.getAll("scopes");
    data.usdt_only = f.has("usdt_only");
    data.public_only = data.connection_mode === "public";
    delete data.connection_mode;
    if (data.public_only) {
      data.environment = "mainnet";
      data.scopes = ["bybit:read"];
      delete data.api_key;
      delete data.api_secret;
    }
    if (
      !data.public_only && data.environment === "mainnet" &&
      !(await confirmAction(
        "保存实盘账户",
        "此连接指向 Mainnet，请确认所勾选的交易和资金权限。",
      ))
    )
      return;
    await api("profiles", { method: "POST", body: JSON.stringify(data) });
    $("#connection-key").value = "";
    $("#connection-secret").value = "";
    toast(data.public_only ? "主网公开行情连接已保存" : "连接已验证并加密保存");
    await loadProfiles();
  });
};
for (const s of ["#endpoint-query", "#endpoint-group", "#demo-filter"])
  $(s).addEventListener(s === "#endpoint-query" ? "input" : "change", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(
      () => loadEndpoints().catch((e) => toast(e.message, true)),
      220,
    );
  });
$("#explorer-form").onsubmit = (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    if (!state.endpoint) throw new Error("请先选择接口");
    const result = await execute(
      selectedProfile("#explorer-profile"),
      state.endpoint.method + " " + state.endpoint.path,
      JSON.parse($("#explorer-params").value),
    );
    output("#explorer-result", result);
  });
};
async function loadAccess() {
  const [grants, audit, secrets] = await Promise.all([
    api("grants"),
    api("audit"),
    api("secret-results"),
  ]);
  renderSecretResults(secrets.items);
  const list = clear($("#grants-list"));
  if (!grants.items.length)
    empty(list, "还没有 OAuth 授权", "AI 客户端完成连接后，授权会显示在这里。");
  for (const g of grants.items) {
    const card = node("div", "connection-card"),
      head = node("div", "connection-head");
    head.append(
      node(
        "strong",
        "",
        g.bindings
          ?.map((b) => connectionLabel(b) + " · " + b.name)
          .join(" / ") ||
          profile(g.profile)?.name ||
          "旧版授权（请重新连接）",
      ),
      node(
        "span",
        "pill",
        g.revoked ? "已撤销" : g.active ? "有效授权" : "已失效",
      ),
    );
    card.append(head, node("p", "", g.client_id));
    if (!g.revoked) {
      const b = node("button", "secondary", "撤销授权");
      b.onclick = () =>
        busy(b, async () => {
          if (
            await confirmAction(
              "撤销客户端访问",
              "撤销后，该授权下的访问令牌与刷新令牌都会失效。",
            )
          ) {
            await api("grants/" + encodeURIComponent(g.id), {
              method: "DELETE",
            });
            await loadAccess();
          }
        });
      card.append(b);
    }
    list.append(card);
  }
  const rows = clear($("#audit-list"));
  for (const a of audit.items) {
    const row = node("tr");
    for (const value of [
      new Date(a.at * 1000).toLocaleString("zh-CN", { hour12: false }),
      profile(a.profile)?.name || a.profile,
      a.action,
      a.outcome,
      a.detail,
    ])
      row.append(node("td", "", value || "—"));
    rows.append(row);
  }
  if (!audit.items.length) {
    const r = node("tr"),
      td = node("td", "", "暂无操作记录");
    td.colSpan = 5;
    r.append(td);
    rows.append(r);
  }
}
window.addEventListener("hashchange", route);
(async () => {
  try {
    await loadStatus();
    await loadProfiles();
    route();
  } catch (e) {
    toast(e.message, true);
  }
})();

function renderSecretResults(items) {
  let panel = $("#secret-results-panel");
  if (!panel) {
    panel = node("article", "panel");
    panel.id = "secret-results-panel";
    const heading = node("div", "panel-heading");
    heading.append(
      node("h2", "", "新建密钥的安全下载"),
      node("span", "pill", "10 分钟内 · 仅可下载一次"),
    );
    panel.append(
      heading,
      node(
        "p",
        "small muted",
        "通过密钥管理接口生成的新凭证会加密暂存。只向已登录的管理员提供下载，不会返回给 AI 客户端。",
      ),
    );
    const list = node("div", "stack");
    list.id = "secret-results-list";
    panel.append(list);
    $('[data-view="access"]').append(panel);
  }
  const list = clear($("#secret-results-list"));
  if (!items.length) {
    list.append(node("p", "small muted", "暂无待下载的新凭证。"));
    return;
  }
  for (const item of items) {
    const card = node("div", "connection-card");
    card.append(
      node("strong", "", item.operation),
      node("p", "muted small", profile(item.profile)?.name || item.profile),
    );
    const a = node("a", "secondary", "下载新凭证 JSON");
    a.href = "/admin/api/secret-results/" + encodeURIComponent(item.id);
    a.setAttribute("download", "");
    card.append(a);
    list.append(card);
  }
}

let editingConnection = null;
function openConnectionSettings(p) {
  editingConnection = p.id;
  $("#settings-name").value = p.name;
  $("#settings-usdt").checked = p.usdt_only;
  $("#settings-usdt").disabled = p.public_only || p.environment === "demo";
  const scopes = clear($("#settings-scopes"));
  for (const [scope, label] of Object.entries(state.status.scopes)) {
    if (p.public_only && scope !== "bybit:read") continue;
    const row = node("label", "check"),
      input = node("input");
    input.type = "checkbox";
    input.value = scope;
    input.checked = p.scopes.includes(scope);
    row.append(input, document.createTextNode(p.public_only ? "主网公开数据" : label));
    scopes.append(row);
  }
  $("#connection-settings-dialog").showModal();
}
$("#settings-cancel").onclick = () => $("#connection-settings-dialog").close();
$("#connection-settings-form").onsubmit = (e) => {
  e.preventDefault();
  busy(e.submitter, async () => {
    const scopes = [
      ...$("#settings-scopes").querySelectorAll("input:checked"),
    ].map((i) => i.value);
    if (!scopes.length) throw new Error("请至少保留一项权限");
    await api("profiles/" + editingConnection, {
      method: "PATCH",
      body: JSON.stringify({
        name: $("#settings-name").value.trim(),
        usdt_only: $("#settings-usdt").checked,
        scopes,
      }),
    });
    $("#connection-settings-dialog").close();
    await loadProfiles();
    toast("连接设置已保存");
  });
};
