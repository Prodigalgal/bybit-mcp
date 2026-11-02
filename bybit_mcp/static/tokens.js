"use strict";
window.AuthorizationTokens = (() => {
  let tokens = [],
    operations = [],
    editing = null,
    rows = [];
  const formatDate = (value) =>
    value == null
      ? "无限期"
      : new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
  async function load() {
    try {
      const data = await api("authorization-tokens");
      tokens = data.items;
      operations = data.operations;
      const list = clear($("#tokens-list"));
      if (!tokens.length)
        empty(
          list,
          "尚未生成授权 Token",
          "先创建免 Key 行情连接或保存账户 Key，再选择允许的连接与操作。",
        );
      for (const value of tokens.sort((a, b) => b.created_at - a.created_at)) {
        const card = node("article", "panel token-card");
        const head = node("div", "panel-heading");
        head.append(
          node("h2", "", value.name),
          node(
            "span",
            "pill",
            { active: "有效", expired: "已到期", revoked: "已撤销" }[
              value.status
            ],
          ),
        );
        card.append(
          head,
          node("p", "small muted", "有效期：" + formatDate(value.expires_at)),
        );
        for (const binding of value.bindings) {
          const entry = node("div", "token-binding-summary");
          entry.append(
            node(
              "strong",
              "",
              connectionLabel(binding) + " · " + binding.name,
            ),
            node("code", "", binding.key_hint),
          );
          entry.append(
            node(
              "p",
              "small",
              binding.public_only ? "主网公开数据 · 无账户和交易权限" : binding.scopes.map((s) => state.status.scopes[s]).join("、"),
            ),
          );
          if (binding.operations === null)
            entry.append(node("p", "small muted", "上述权限类别内的全部操作"));
          else {
            const detail = node("details");
            detail.append(
              node(
                "summary",
                "small",
                `允许 ${binding.operations.length} 项操作`,
              ),
            );
            const ul = node("ul", "operation-list");
            binding.operations.forEach((o) => {
              const li = node("li");
              li.append(node("code", "", o));
              ul.append(li);
            });
            detail.append(ul);
            entry.append(detail);
          }
          if (!binding.enabled)
            entry.append(node("p", "small danger-text", "连接已禁用或删除"));
          card.append(entry);
        }
        if (!value.revoked) {
          const actions = node("div", "button-row"),
            edit = node("button", "secondary", "编辑权限与时限"),
            revoke = node("button", "secondary danger-text", "撤销 Token");
          edit.onclick = () => open(value);
          revoke.onclick = () =>
            busy(revoke, async () => {
              if (
                !(await confirmAction(
                  "撤销授权 Token",
                  "此 Token 的 Bearer 访问及派生的 OAuth 令牌将失效，定时撤单任务会停止执行。",
                ))
              )
                return;
              await api(
                "authorization-tokens/" + encodeURIComponent(value.id),
                { method: "DELETE" },
              );
              await load();
            });
          actions.append(edit, revoke);
          card.append(actions);
        }
        list.append(card);
      }
    } catch (error) {
      toast(error.message, true);
    }
  }
  function defaults() {
    const select = $("#token-default"),
      previous = select.value;
    clear(select);
    const none = node("option", "", "不指定（调用时选择）");
    none.value = "";
    select.append(none);
    rows
      .filter((r) => r.select.checked)
      .forEach((r) => {
        const option = node(
          "option",
          "",
          connectionLabel(r.profile) + " · " + r.profile.name,
        );
        option.value = r.profile.id;
        select.append(option);
      });
    select.value = [...select.options].some((o) => o.value === previous)
      ? previous
      : "";
  }
  function connectionRow(profile, binding) {
    const card = node("fieldset", "token-binding-editor"),
      legend = node("legend"),
      label = node("label", "check"),
      select = node("input");
    select.type = "checkbox";
    select.checked = !!binding;
    select.setAttribute("aria-label", "允许 " + profile.name);
    label.append(
      select,
      document.createTextNode(profile.name + " · " + connectionLabel(profile)),
    );
    legend.append(label);
    card.append(legend);
    const content = node("div", "token-binding-content");
    content.append(node("p", "small muted", connectionSummary(profile)));
    content.hidden = !select.checked;
    const scopes = node("div", "token-scopes"),
      scopeInputs = [];
    for (const scope of profile.scopes) {
      const item = node("label", "check"),
        check = node("input");
      check.type = "checkbox";
      check.value = scope;
      check.checked = binding
        ? binding.scopes.includes(scope)
        : scope === "bybit:read";
      item.append(check, document.createTextNode(profile.public_only ? "主网公开数据" : state.status.scopes[scope]));
      scopes.append(item);
      scopeInputs.push(check);
    }
    const preset = node("button", "secondary", profile.public_only ? "主网公开数据权限" : "常用 USDT 交易权限");
    preset.type = "button";
    content.append(scopes, preset);
    const modeLabel = node("label", "", "允许操作"),
      mode = node("select");
    mode.setAttribute("aria-label", profile.name + " 操作模式");
    for (const [key, title] of [
      ["all", profile.public_only ? "全部主网公开数据" : "所选权限类别内的全部操作"],
      ["selected", "只允许选中的操作"],
    ]) {
      const option = node("option", "", title);
      option.value = key;
      mode.append(option);
    }
    mode.value = binding?.operations === null || !binding ? "all" : "selected";
    const chooser = node("div", "token-operation-chooser"),
      filter = node("input");
    filter.type = "search";
    filter.placeholder = "搜索路径、操作名称";
    filter.setAttribute("aria-label", profile.name + " 搜索操作");
    const list = node("div", "token-operation-list"),
      chosen = new Set(binding?.operations || []);
    chooser.append(filter, list);
    content.append(modeLabel, mode, chooser);
    card.append(content);
    const row = { profile, select, scopeInputs, mode, chosen };
    function renderOperations() {
      chooser.hidden = mode.value !== "selected";
      clear(list);
      const allowed = new Set(
          scopeInputs.filter((i) => i.checked).map((i) => i.value),
        ),
        query = filter.value.trim().toLowerCase();
      const choices = operations.filter(
        (o) =>
          allowed.has(o.scope) &&
          (!profile.public_only || o.public) &&
          (profile.environment !== "demo" || o.demo) &&
          (!query ||
            (o.title + " " + o.path + " " + o.id)
              .toLowerCase()
              .includes(query)),
      );
      for (const operation of choices) {
        const item = node("label", "check token-operation"),
          check = node("input");
        check.type = "checkbox";
        check.checked = chosen.has(operation.id);
        check.value = operation.id;
        check.onchange = () =>
          check.checked
            ? chosen.add(operation.id)
            : chosen.delete(operation.id);
        const text = node("span");
        text.append(
          node("strong", "", operation.title),
          node("code", "", operation.method + " " + operation.path),
        );
        item.append(check, text);
        list.append(item);
      }
      if (!choices.length)
        list.append(
          node("p", "small muted", "暂无匹配操作，请先选择权限类别。"),
        );
    }
    scopeInputs.forEach((input) => (input.onchange = renderOperations));
    mode.onchange = renderOperations;
    filter.oninput = renderOperations;
    select.onchange = () => {
      content.hidden = !select.checked;
      defaults();
    };
    preset.onclick = () => {
      scopeInputs.forEach(
        (i) => (i.checked = ["bybit:read", "bybit:trade"].includes(i.value)),
      );
      mode.value = "selected";
      chosen.clear();
      if (profile.public_only) {
        operations.filter((o) => o.public).forEach((o) => chosen.add(o.id));
        renderOperations();
        return;
      }
      const paths = new Set([
        "/v5/market/time",
        "/v5/market/instruments-info",
        "/v5/market/tickers",
        "/v5/market/kline",
        "/v5/market/orderbook",
        "/v5/market/open-interest",
        "/v5/market/funding/history",
        "/v5/account/info",
        "/v5/account/wallet-balance",
        "/v5/position/list",
        "/v5/order/realtime",
        "/v5/order/history",
        "/v5/execution/list",
        "/v5/order/create",
        "/v5/order/amend",
        "/v5/order/cancel",
        "/v5/order/cancel-all",
        "/v5/order/create-batch",
        "/v5/order/amend-batch",
        "/v5/order/cancel-batch",
        "/v5/position/set-leverage",
        "/v5/position/trading-stop",
        "managed:read_orders",
        "ws:public",
        "ws:private",
      ]);
      operations
        .filter((o) => paths.has(o.path))
        .forEach((o) => chosen.add(o.id));
      renderOperations();
    };
    renderOperations();
    return { card, row };
  }
  async function open(value = null) {
    if (!operations.length) await load();
    if (!state.profiles.some((p) => p.enabled)) {
      toast("请先添加并启用账户连接", true);
      return;
    }
    editing = value;
    rows = [];
    $("#token-policy-form").reset();
    $("#token-policy-error").hidden = true;
    $("#token-name").value = value?.name || "";
    $("#token-dialog-title").textContent = value
      ? "编辑授权 Token"
      : "生成授权 Token";
    $("#save-token-policy").textContent = value
      ? "保存权限与时限"
      : "生成 Token";
    if (value) {
      $("#token-expiry").value =
        value.expires_at === null ? "unlimited" : "custom";
      if (value.expires_at !== null) {
        const date = new Date(value.expires_at * 1000);
        $("#token-expires-at").value = new Date(
          date.getTime() - date.getTimezoneOffset() * 60000,
        )
          .toISOString()
          .slice(0, 16);
      }
    }
    expiryChanged();
    const container = clear($("#token-bindings"));
    for (const env of ["public", "demo", "testnet", "mainnet"]) {
      const profiles = state.profiles.filter(
        (p) => p.enabled && (env === "public" ? p.public_only : !p.public_only && p.environment === env),
      );
      if (!profiles.length) continue;
      container.append(node("h3", "token-env-heading", env === "public" ? "主网公开数据（免 Key）" : environmentLabel(env)));
      for (const profile of profiles) {
        const { card, row } = connectionRow(
          profile,
          value?.bindings.find((b) => b.profile_id === profile.id),
        );
        rows.push(row);
        container.append(card);
      }
    }
    defaults();
    $("#token-default").value = value?.default_profile_id || "";
    $("#token-policy-dialog").showModal();
  }
  function expiryChanged() {
    const custom = $("#token-expiry").value === "custom";
    $("#token-expires-at").disabled = !custom;
    $("#token-expires-at").required = custom;
  }
  $("#token-expiry").onchange = expiryChanged;
  $("#create-token").onclick = () => open();
  $("#refresh-tokens").onclick = () => busy($("#refresh-tokens"), load);
  for (const id of ["close-token-policy", "cancel-token-policy"])
    $("#" + id).onclick = () => $("#token-policy-dialog").close();
  $("#token-policy-form").onsubmit = async (event) => {
    event.preventDefault();
    $("#token-policy-error").hidden = true;
    await busy($("#save-token-policy"), async () => {
      try {
        const bindings = rows
          .filter((r) => r.select.checked)
          .map((r) => {
            const scopes = r.scopeInputs
              .filter((i) => i.checked)
              .map((i) => i.value);
            const operationsAllowed =
              r.mode.value === "all"
                ? null
                : [...r.chosen].filter((id) =>
                    operations.some(
                      (o) => o.id === id && scopes.includes(o.scope),
                    ),
                  );
            return {
              profile_id: r.profile.id,
              environment: r.profile.environment,
              scopes,
              operations: operationsAllowed,
            };
          });
        if (!bindings.length) throw new Error("至少选择一个行情或 Key 连接");
        const expiry = $("#token-expiry").value,
          expires =
            expiry === "unlimited"
              ? null
              : expiry === "custom"
                ? new Date($("#token-expires-at").value).getTime() / 1000
                : Date.now() / 1000 + Number(expiry) * 86400;
        if (
          expires !== null &&
          (!Number.isFinite(expires) || expires <= Date.now() / 1000)
        )
          throw new Error("请选择未来的截止时间");
        const body = {
          name: $("#token-name").value,
          expires_at: expires,
          bindings,
          default_profile_id: $("#token-default").value || null,
        };
        const result = await api(
          "authorization-tokens" +
            (editing ? "/" + encodeURIComponent(editing.id) : ""),
          { method: editing ? "PATCH" : "POST", body: JSON.stringify(body) },
        );
        $("#token-policy-dialog").close();
        await load();
        if (result.token) {
          $("#generated-token").value = result.token;
          $("#token-secret-dialog").showModal();
        } else toast("权限与时限已更新");
      } catch (error) {
        $("#token-policy-error").textContent = error.message;
        $("#token-policy-error").hidden = false;
      }
    });
  };
  $("#copy-generated-token").onclick = () =>
    busy($("#copy-generated-token"), async () => {
      await navigator.clipboard.writeText($("#generated-token").value);
      toast("Token 已复制");
    });
  $("#close-token-secret").onclick = () => $("#token-secret-dialog").close();
  $("#token-secret-dialog").addEventListener(
    "close",
    () => ($("#generated-token").value = ""),
  );
  return { load };
})();
