"use strict";
(() => {
  const t = {
    generation: 0,
    dataEpoch: 0,
    managedRequest: 0,
    context: null,
    preview: null,
    rows: [],
    selected: new Set(),
    tab: "orders",
    cursors: [""],
    nextCursor: "",
    pending: new Set(),
    edit: null,
    position: null,
    initialized: false,
  };
  const activeStatuses = new Set([
    "New",
    "PartiallyFilled",
    "Untriggered",
    "Triggered",
    "Created",
  ]);
  const labels = {
    New: "挂单中",
    PartiallyFilled: "部分成交",
    Untriggered: "等待触发",
    Triggered: "已触发",
    Filled: "已成交",
    Cancelled: "已撤销",
    Rejected: "已拒绝",
    Deactivated: "已失效",
    PartiallyFilledCanceled: "部分成交后撤销",
    submitting: "提交中，待核查",
    acknowledged: "已受理，待核查",
    partially_rejected: "批量部分失败",
    unknown: "结果未知，需核查",
    rejected: "请求被拒绝",
    confirmed: "已查到订单",
    scheduled: "等待到期",
    checking: "核查订单中",
    cancel_requested: "撤单已受理",
    cancel_unknown: "撤单响应未知",
    completed: "已结束",
    needs_attention: "需要处理",
    disabled: "已停用",
    retry: "稍后核查",
  };
  const symbol = () => $("#trade-symbol").value.trim().toUpperCase();
  const account = () => selectedProfile("#trade-profile");
  const key = () => $("#trade-profile").value + ":" + symbol();
  const stamp = () => ({ generation: t.generation, key: key() });
  const current = (s) => s.generation === t.generation && s.key === key();
  const when = (value) =>
    value
      ? new Date(
          Number(value) > 1e12 ? Number(value) : Number(value) * 1000,
        ).toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" })
      : "—";
  const display = (value) =>
    value === null || value === undefined || value === "" ? "—" : String(value);
  const button = (label, handler, cls = "secondary") => {
    const b = node("button", cls, label);
    b.type = "button";
    b.onclick = () => busy(b, handler);
    return b;
  };
  function field(id) {
    return $(id).value.trim();
  }
  function invalidate() {
    t.preview = null;
    $("#t-preview-panel").hidden = true;
  }
  function note(message, error = false) {
    const box = $("#t-operation-result");
    box.hidden = false;
    box.classList.toggle("error", error);
    clear(box).append(node("p", "", message));
  }
  function operationResult(result) {
    const outcome = result.state || "acknowledged";
    const message = result.error
      ? result.error
      : result.note || "请求已受理，请核对订单最新状态。";
    note(
      (labels[outcome] || outcome) + "：" + message,
      ["unknown", "rejected", "partially_rejected"].includes(outcome),
    );
    const box = $("#t-operation-result");
    if (result.batch_results)
      for (const r of result.batch_results)
        box.append(
          node(
            "p",
            "small",
            `第 ${r.index + 1} 笔：${r.accepted === true ? "已受理" : r.accepted === false ? "失败" : "待核查"} · ${r.message || ""} ${r.code ?? ""}`,
          ),
        );
    if (result.items)
      for (const r of result.items)
        box.append(
          node(
            "p",
            "small",
            `${r.order_id} · ${r.code === 0 ? "受理成功" : "需核查"} ${r.code ?? ""} ${r.message || ""}`,
          ),
        );
    const details = node("details");
    details.append(
      node("summary", "", "查看返回详情"),
      node("pre", "result", JSON.stringify(result, null, 2)),
    );
    box.append(details);
  }
  function resetTable() {
    t.rows = [];
    t.selected.clear();
    t.cursors = [""];
    t.nextCursor = "";
    renderTable();
  }
  function contextChanged() {
    t.generation++;
    t.context = null;
    invalidate();
    resetTable();
    for (const d of $$("dialog.trade-dialog")) if (d.open) d.close();
    $("#t-market").hidden = true;
    const p = profile($("#trade-profile").value);
    $("#t-environment").textContent = p
      ? environmentLabel(p.environment)
      : "未选择账户";
    $("#t-environment").className = "pill " + (p?.environment || "");
    $("#t-context-status").textContent = p
      ? "请载入精确合约。"
      : "请先添加并选择账户连接。";
    clear($("#t-submissions"));
    clear($("#t-expiry-jobs"));
    if (p && location.hash === "#trading" && /^[A-Z0-9_]+USDT$/.test(symbol()))
      return refreshAll(true);
  }
  function activate() {
    if (!t.context && $("#trade-profile").value) refreshAll(true);
  }
  async function loadContext(wallet = false) {
    const p = account(),
      s = stamp(),
      epoch = t.dataEpoch;
    const pending = "context:" + s.key + ":" + s.generation + ":" + epoch;
    if (t.pending.has(pending)) return;
    t.pending.add(pending);
    try {
      const params = new URLSearchParams({
        profile_id: p.id,
        symbol: symbol(),
        include_wallet: wallet,
      });
      const ctx = await api("trading/context?" + params);
      if (!current(s) || epoch !== t.dataEpoch) return;
      ctx.profile_id = p.id;
      t.context = ctx;
      $("#t-market").hidden = false;
      $("#t-last").textContent = display(ctx.ticker.lastPrice);
      $("#t-mark").textContent = display(ctx.ticker.markPrice);
      $("#t-spread").textContent =
        `${display(ctx.ticker.bid1Price)} / ${display(ctx.ticker.ask1Price)}`;
      $("#t-steps").textContent =
        `${ctx.instrument.lotSizeFilter.qtyStep} / ${ctx.instrument.priceFilter.tickSize}`;
      $("#t-actual-leverage").textContent =
        [...new Set(ctx.positions.map((p) => p.leverage).filter(Boolean))]
          .map((x) => x + "×")
          .join(" / ") || "无法读取";
      if (ctx.wallet) {
        const coin = ctx.wallet.list
          ?.flatMap((a) => a.coin || [])
          .find((c) => c.coin === "USDT");
        $("#t-wallet").textContent = coin
          ? display(coin.walletBalance)
          : "未返回 USDT";
      }
      if (ctx.wallet_error) $("#t-wallet").textContent = "暂不可用";
      $("#t-context-status").textContent =
        `${ctx.symbol} · 最小数量 ${ctx.instrument.lotSizeFilter.minOrderQty} · 最小名义金额 ${ctx.instrument.lotSizeFilter.minNotionalValue || "以 Bybit 为准"} USDT · 更新 ${when(ctx.at)}（北京时间）`;
      $("#t-price").placeholder =
        `价格步长 ${ctx.instrument.priceFilter.tickSize}`;
      $("#t-amount").placeholder =
        $("#t-amount-mode").value === "qty"
          ? `数量步长 ${ctx.instrument.lotSizeFilter.qtyStep}`
          : "输入 USDT 金额";
    } catch (e) {
      if (current(s)) {
        $("#t-context-status").textContent = e.message;
        throw e;
      }
    } finally {
      t.pending.delete(pending);
    }
  }
  async function requireContext() {
    if (
      !t.context ||
      t.context.profile_id !== account().id ||
      t.context.symbol !== symbol()
    )
      await loadContext(true);
    if (
      !t.context ||
      t.context.profile_id !== account().id ||
      t.context.symbol !== symbol()
    )
      throw new Error("合约数据尚未载入，请稍后重试");
  }
  function setKind() {
    const kind = $("#t-kind").value,
      market = kind.endsWith("market"),
      conditional = kind.startsWith("conditional");
    $("#t-limit-fields").hidden = market;
    $("#t-price").required = !market;
    $("#t-trigger-fields").hidden = !conditional;
    $("#t-trigger-price").required = conditional;
    $("#t-slippage-fields").hidden = !market || conditional;
    $("#t-expiry").disabled = market && !conditional;
    if (market && !conditional) $("#t-expiry").value = "0";
    invalidate();
  }
  function draft() {
    const kind = $("#t-kind").value;
    const d = {
      symbol: symbol(),
      kind,
      side: $("#t-side").value,
      amount_mode: $("#t-amount-mode").value,
      amount: field("#t-amount"),
      position_idx:
        $("#t-position-idx").value === "auto"
          ? "auto"
          : Number($("#t-position-idx").value),
      time_in_force: $("#t-tif").value,
      expire_seconds: Number($("#t-expiry").value),
      reduce_only: $("#t-reduce-only").checked,
      close_on_trigger: $("#t-close-trigger").checked,
      tpsl_mode: $("#t-tpsl-mode").value,
    };
    if (!kind.endsWith("market")) d.price = field("#t-price");
    if (kind.startsWith("conditional"))
      Object.assign(d, {
        trigger_price: field("#t-trigger-price"),
        trigger_by: $("#t-trigger-by").value,
        trigger_direction:
          $("#t-trigger-direction").value === "auto"
            ? "auto"
            : Number($("#t-trigger-direction").value),
      });
    if (kind === "market" && field("#t-slippage"))
      d.slippage = field("#t-slippage");
    for (const [prefix, key] of [
      ["tp", "take_profit"],
      ["sl", "stop_loss"],
    ]) {
      if (field(`#t-${prefix}-value`))
        d[key] = {
          value: field(`#t-${prefix}-value`),
          unit: $(`#t-${prefix}-unit`).value,
          trigger_by: $(`#t-${prefix}-trigger`).value,
          order_type: $(`#t-${prefix}-type`).value,
          limit_price: field(`#t-${prefix}-limit`),
        };
    }
    return d;
  }
  async function previewOrder(d) {
    const p = account(),
      s = stamp();
    const result = await api("trading/preview", {
      method: "POST",
      body: JSON.stringify({ profile_id: p.id, draft: d }),
    });
    if (!current(s)) return;
    renderPreview(result);
  }
  function renderPreview(preview) {
    t.preview = preview;
    const panel = $("#t-preview-panel");
    panel.hidden = false;
    $("#t-preview-env").textContent = environmentLabel(preview.environment);
    $("#t-preview-env").className = "pill " + preview.environment;
    const box = clear($("#t-preview-summary"));
    box.append(
      node(
        "p",
        "",
        `${preview.symbol} · ${preview.orders.length} 笔 · 总名义金额 ${preview.total_notional_usdt} USDT`,
      ),
    );
    for (let i = 0; i < preview.orders.length; i++) {
      const o = preview.orders[i],
        summary = preview.summaries[i];
      const row = node("div", "preview-order");
      row.append(
        node(
          "strong",
          "",
          `${i + 1}. ${o.side === "Buy" ? "买入" : "卖出"} ${o.qty} · ${o.triggerPrice ? "条件" : ""}${o.orderType === "Market" ? "市价" : "限价 " + o.price}`,
        ),
        node(
          "p",
          "small muted",
          `持仓索引 ${o.positionIdx} · ${o.timeInForce} ${o.reduceOnly ? "· 只减仓" : ""}`,
        ),
      );
      if (o.triggerPrice)
        row.append(
          node(
            "p",
            "small",
            `${o.triggerBy} ${o.triggerDirection === 1 ? "上涨" : "下跌"}至 ${o.triggerPrice} 触发`,
          ),
        );
      if (o.takeProfit || o.stopLoss)
        row.append(
          node(
            "p",
            "small",
            `止盈 ${o.takeProfit || "未设置"} · 止损 ${o.stopLoss || "未设置"} · ${o.tpslMode}`,
          ),
        );
      row.append(
        node(
          "p",
          "small",
          `名义金额 ${summary.notional_usdt} USDT · 实际杠杆 ${summary.actual_leverage ? summary.actual_leverage + "×" : "未读到"}`,
        ),
      );
      if (summary.estimated_initial_margin_usdt)
        row.append(
          node(
            "p",
            "small",
            `估算初始保证金 ${summary.estimated_initial_margin_usdt} USDT`,
          ),
        );
      row.append(
        node(
          "p",
          "small",
          `标的波动 1% 对应约 ±${summary.pnl_at_one_percent_usdt} USDT（费前）`,
        ),
      );
      row.append(
        node(
          "p",
          "small muted",
          summary.expiry_seconds
            ? `${summary.expiry_seconds} 秒后由服务端尝试撤销剩余委托`
            : "未设置到期撤单",
        ),
      );
      row.append(node("code", "small muted", o.orderLinkId));
      box.append(row);
    }
    box.append(node("p", "small muted", preview.summaries[0].note));
    const warnings = clear($("#t-preview-warnings"));
    for (const warning of [
      ...new Set(preview.summaries.flatMap((s) => s.warnings)),
    ])
      warnings.append(node("div", "alert", warning));
    $("#t-preview-expiry").textContent =
      "预览有效至 " +
      when(preview.expires_at) +
      "（北京时间）。修改表单、账户或合约后需重新预览。";
    $("#t-submit-preview").disabled = false;
    panel.scrollIntoView({
      behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "instant"
        : "smooth",
      block: "nearest",
    });
  }
  async function submitPreview() {
    const b = $("#t-submit-preview"),
      preview = t.preview;
    if (!preview || b.disabled) return;
    const p = account(),
      s = stamp();
    if (preview.profile_id !== p.id || preview.symbol !== symbol())
      throw new Error("账户或合约已变化，请重新预览");
    if (preview.expires_at * 1000 <= Date.now())
      throw new Error("预览已过期，请重新预览");
    if (
      p.environment === "mainnet" &&
      !(await confirmAction(
        "确认实盘委托",
        `${p.name} · ${preview.symbol}\n本次提交 ${preview.orders.length} 笔，名义金额合计 ${preview.total_notional_usdt} USDT。`,
      ))
    )
      return;
    b.disabled = true;
    b.textContent = "提交中…";
    try {
      const result = await api("trading/submit", {
        method: "POST",
        body: JSON.stringify({
          profile_id: p.id,
          preview_id: preview.preview_id,
          confirm_environment: p.environment,
        }),
      });
      if (!current(s)) return;
      t.dataEpoch++;
      invalidate();
      operationResult(result);
      await refreshAll(true);
    } catch (e) {
      if (current(s)) {
        invalidate();
        note(
          "提交响应未确认，请按原预览编号核查。请勿重新创建订单后直接重发。",
          true,
        );
        $("#t-operation-result").append(
          button("核查原请求", () => reconcile(preview.preview_id)),
          node("p", "small muted", e.message),
        );
        loadManaged().catch(() => {});
      }
    } finally {
      b.textContent = "确认提交";
      b.disabled = false;
    }
  }
  function addBatchRow(price = "", amount = "") {
    const rows = $("#t-batch-rows");
    if (rows.children.length >= 20) throw new Error("最多 20 档");
    const row = node("div", "batch-row"),
      p = node("input"),
      q = node("input");
    p.inputMode = "decimal";
    p.placeholder = "限价价格";
    p.setAttribute("aria-label", "分批价格");
    p.value = price;
    p.dataset.batchField = "price";
    q.inputMode = "decimal";
    q.placeholder = "基础资产数量";
    q.setAttribute("aria-label", "分批数量");
    q.value = amount;
    q.dataset.batchField = "amount";
    row.append(
      p,
      q,
      button(
        "×",
        async () => {
          row.remove();
          invalidate();
        },
        "remove-row",
      ),
    );
    rows.append(row);
  }
  function batchDraft(forceRows = false) {
    if ($("#t-batch-advanced-toggle").checked && !forceRows) {
      const orders = JSON.parse($("#t-batch-json").value);
      if (!Array.isArray(orders)) throw new Error("请填写 orders 数组");
      return { symbol: symbol(), orders };
    }
    const orders = [...$("#t-batch-rows").children].map((row) => ({
      kind: "limit",
      side: $("#t-batch-side").value,
      amount_mode: "qty",
      amount: row.querySelector('[data-batch-field="amount"]').value.trim(),
      price: row.querySelector('[data-batch-field="price"]').value.trim(),
      time_in_force: $("#t-batch-tif").value,
      expire_seconds: Number($("#t-batch-expiry").value),
      position_idx: "auto",
    }));
    return { symbol: symbol(), orders };
  }
  async function loadTable() {
    const p = account(),
      s = stamp(),
      epoch = t.dataEpoch,
      tab = t.tab,
      cursor = t.cursors.at(-1),
      page = t.cursors.length;
    const pending = `table:${s.key}:${s.generation}:${tab}:${cursor}:${epoch}`;
    if (t.pending.has(pending)) return;
    t.pending.add(pending);
    try {
      const result = await api(
        "trading/table?" +
          new URLSearchParams({
            profile_id: p.id,
            symbol: symbol(),
            tab,
            cursor,
          }),
      );
      if (
        !current(s) ||
        epoch !== t.dataEpoch ||
        tab !== t.tab ||
        page !== t.cursors.length ||
        cursor !== t.cursors.at(-1)
      )
        return;
      t.rows = result.items;
      t.nextCursor = result.next_cursor;
      t.selected = new Set(
        [...t.selected].filter((id) =>
          t.rows.some(
            (o) => o.orderId === id && activeStatuses.has(o.orderStatus),
          ),
        ),
      );
      $("#t-table-status").textContent =
        `${result.symbol} · 当前页 ${result.items.length} 条 · 更新 ${when(result.at)}（北京时间）`;
      renderTable();
    } catch (e) {
      if (current(s) && tab === t.tab) {
        $("#t-table-status").textContent =
          "读取失败，已有数据可能过期：" + e.message;
        throw e;
      }
    } finally {
      t.pending.delete(pending);
    }
  }
  function header(labels) {
    const row = node("tr");
    for (const label of labels) row.append(node("th", "", label));
    clear($("#t-table-head")).append(row);
    return row;
  }
  function cell(row, value, cls = "") {
    const c = node("td", cls, display(value));
    row.append(c);
    return c;
  }
  function selectionChanged() {
    const count = t.selected.size;
    $("#t-selected-count").textContent = `已选 ${count} 笔（最多 20 笔）`;
    $("#t-batch-amend").disabled = count === 0;
    $("#t-batch-cancel").disabled = count === 0;
  }
  function renderTable() {
    const rows = clear($("#t-table-body"));
    let columns = 1;
    $("#t-selection-actions").hidden = t.tab !== "orders";
    if (t.tab === "orders" || t.tab === "history") {
      const head = header([
        t.tab === "orders" ? "选择" : "时间",
        "方向 / 类型",
        "委托 / 成交数量",
        "价格 / 触发价",
        "止盈 / 止损",
        "状态",
        "操作 / 编号",
      ]);
      columns = 7;
      if (t.tab === "orders") {
        const all = node("input");
        all.type = "checkbox";
        all.setAttribute("aria-label", "选择本页最多 20 笔订单");
        all.checked =
          t.selected.size > 0 &&
          t.rows
            .filter((o) => activeStatuses.has(o.orderStatus))
            .slice(0, 20)
            .every((o) => t.selected.has(o.orderId));
        all.onchange = () => {
          t.selected = all.checked
            ? new Set(
                t.rows
                  .filter((o) => activeStatuses.has(o.orderStatus))
                  .slice(0, 20)
                  .map((o) => o.orderId),
              )
            : new Set();
          renderTable();
        };
        clear(head.firstChild).append(all);
      }
      for (const o of t.rows) {
        const row = node("tr"),
          first = node("td");
        if (t.tab === "orders") {
          const c = node("input");
          c.type = "checkbox";
          c.setAttribute("aria-label", "选择订单 " + o.orderId);
          c.checked = t.selected.has(o.orderId);
          c.disabled = !activeStatuses.has(o.orderStatus);
          c.onchange = () => {
            if (c.checked && t.selected.size >= 20) {
              c.checked = false;
              toast("一次最多选择 20 笔", true);
              return;
            }
            if (c.checked) t.selected.add(o.orderId);
            else t.selected.delete(o.orderId);
            selectionChanged();
          };
          first.append(c);
        } else first.textContent = when(o.createdTime);
        row.append(first);
        cell(
          row,
          `${o.side === "Buy" ? "买入" : "卖出"} · ${Number(o.triggerPrice) ? "条件" : ""}${o.orderType}`,
        );
        cell(row, `${o.qty} / ${o.cumExecQty || "0"}`);
        cell(
          row,
          `${Number(o.price) ? o.price : "市价"}${Number(o.triggerPrice) ? " / 触发 " + o.triggerPrice : ""}`,
        );
        cell(
          row,
          `${Number(o.takeProfit) ? o.takeProfit : "—"} / ${Number(o.stopLoss) ? o.stopLoss : "—"}`,
        );
        cell(row, labels[o.orderStatus] || o.orderStatus);
        const actions = node("td", "row-actions");
        if (t.tab === "orders" && activeStatuses.has(o.orderStatus))
          actions.append(
            button("改单", async () => openAmend(o)),
            button("撤单", () => cancelOrder(o)),
          );
        const id = node("small", "muted order-identifier", o.orderId);
        id.title = o.orderLinkId || o.orderId;
        actions.append(id);
        row.append(actions);
        rows.append(row);
      }
    } else if (t.tab === "positions") {
      header([
        "方向 / 索引",
        "数量 / 均价",
        "杠杆 / 标记价",
        "未实现盈亏",
        "止盈 / 止损",
        "操作",
      ]);
      columns = 6;
      for (const p of t.rows.filter((p) => Number(p.size) > 0)) {
        const row = node("tr");
        cell(row, `${p.side === "Buy" ? "多仓" : "空仓"} / ${p.positionIdx}`);
        cell(row, `${p.size} / ${display(p.avgPrice)}`);
        cell(row, `${display(p.leverage)}× / ${display(p.markPrice)}`);
        cell(
          row,
          display(p.unrealisedPnl) + " USDT",
          Number(p.unrealisedPnl) < 0 ? "negative" : "positive",
        );
        cell(
          row,
          `${Number(p.takeProfit) ? p.takeProfit : "—"} / ${Number(p.stopLoss) ? p.stopLoss : "—"}`,
        );
        const actions = node("td", "row-actions");
        actions.append(
          button("止盈止损", async () => openPosition(p)),
          button("平仓预览", async () => openClose(p)),
        );
        row.append(actions);
        rows.append(row);
      }
    } else {
      header([
        "成交时间",
        "方向",
        "成交数量",
        "成交价",
        "手续费 / 币种",
        "订单编号",
      ]);
      columns = 6;
      for (const e of t.rows) {
        const row = node("tr");
        for (const value of [
          when(e.execTime),
          e.side === "Buy" ? "买入" : "卖出",
          e.execQty,
          e.execPrice,
          `${display(e.execFee)} / ${e.feeCurrency || "USDT"}`,
          e.orderId,
        ])
          cell(row, value);
        rows.append(row);
      }
    }
    if (!rows.children.length) {
      const row = node("tr"),
        c = node("td", "table-empty", "当前账户与合约暂无该类记录");
      c.colSpan = columns;
      row.append(c);
      rows.append(row);
    }
    selectionChanged();
    $("#t-page-prev").disabled = t.cursors.length === 1;
    $("#t-page-next").disabled = !t.nextCursor;
    $("#t-page-label").textContent = `第 ${t.cursors.length} 页`;
  }
  async function loadManaged() {
    const p = account(),
      s = stamp(),
      requestId = ++t.managedRequest;
    const result = await api(
      "trading/managed?" + new URLSearchParams({ profile_id: p.id }),
    );
    if (!current(s) || requestId !== t.managedRequest) return;
    const list = clear($("#t-submissions")),
      jobs = clear($("#t-expiry-jobs"));
    if (!result.submissions.length)
      list.append(node("p", "small muted", "暂无通过预览提交的订单。"));
    for (const r of result.submissions.slice(0, 12)) {
      const card = node("div", "managed-row"),
        heading = node("div", "connection-head");
      heading.append(
        node("strong", "", `${r.symbol} · ${r.orders.length} 笔`),
        node("span", "pill", labels[r.state] || r.state),
      );
      card.append(heading, node("p", "small muted", when(r.created_at)));
      if (r.error) card.append(node("p", "small", r.error));
      for (const observation of r.observed || [])
        card.append(
          node(
            "p",
            "small",
            `${observation.order_link_id} · ${observation.found ? labels[observation.order.orderStatus] || observation.order.orderStatus : "尚未查到，需继续核查"}`,
          ),
        );
      card.append(button("核查原订单", () => reconcile(r.preview_id)));
      list.append(card);
    }
    if (!result.jobs.length)
      jobs.append(node("p", "small muted", "暂无到期撤单任务。"));
    for (const j of result.jobs) {
      const card = node("div", "managed-row"),
        heading = node("div", "connection-head");
      heading.append(
        node("strong", "", j.symbol),
        node("span", "pill", labels[j.state] || j.state),
      );
      card.append(
        heading,
        node("p", "small muted", `计划到期 ${when(j.expires_at || j.due_at)}`),
        node("code", "small", j.order_link_id),
      );
      if (j.error) card.append(node("p", "small", j.error));
      if (!["completed", "disabled"].includes(j.state))
        card.append(
          button("停用到期撤单", async () => {
            if (
              await confirmAction(
                "停用到期撤单",
                "此操作会停止服务端的到期撤单任务。当前委托仍按 Bybit 上的订单状态执行。",
              )
            ) {
              await act("disable_expiry", { job_id: j.id });
              await loadManaged();
            }
          }),
        );
      if (["needs_attention", "disabled"].includes(j.state))
        card.append(
          button("重新核查并撤单", async () => {
            if (
              await confirmAction(
                "恢复撤单任务",
                "现在重新核查这笔订单，并撤销尚未成交的部分。",
              )
            ) {
              await act("retry_expiry", { job_id: j.id });
              await loadManaged();
            }
          }),
        );
      jobs.append(card);
    }
  }
  async function reconcile(id) {
    const p = account(),
      s = stamp();
    const result = await api("trading/reconcile", {
      method: "POST",
      body: JSON.stringify({ profile_id: p.id, submission_id: id }),
    });
    if (current(s)) {
      operationResult(result);
      await Promise.all([loadManaged(), loadTable()]);
    }
  }
  async function refreshAll(wallet = false) {
    if (!$("#trade-profile").value) return;
    const results = await Promise.allSettled([
      loadContext(wallet),
      loadTable(),
    ]);
    try {
      await loadManaged();
    } catch (error) {
      results.push({ status: "rejected", reason: error });
    }
    const failure = results.find((r) => r.status === "rejected");
    if (failure) toast(failure.reason.message, true);
  }
  async function act(action, values = {}) {
    const p = account(),
      s = stamp();
    const result = await api("trading/action", {
      method: "POST",
      body: JSON.stringify({
        profile_id: p.id,
        symbol: symbol(),
        action,
        confirm_environment: p.environment,
        ...values,
      }),
    });
    if (current(s)) t.dataEpoch++;
    return result;
  }
  async function confirmMutation(title, message) {
    const p = account();
    return confirmAction(
      `${title} · ${environmentLabel(p.environment)}`,
      `${p.name} · ${symbol()}\n${message}`,
    );
  }
  async function cancelOrder(o) {
    if (
      !(await confirmMutation(
        "撤销委托",
        `订单 ${o.orderId}\n撤销尚未成交的部分。`,
      ))
    )
      return;
    const s = stamp();
    const r = await act("cancel", { order_id: o.orderId });
    if (current(s)) {
      operationResult(r);
      await refreshAll(true);
    }
  }
  function openAmend(o) {
    t.edit = { order: o, key: key() };
    $("#t-amend-info").textContent =
      `${o.symbol} · ${o.orderId} · 已成交 ${o.cumExecQty || "0"}`;
    for (const id of ["price", "qty", "trigger", "tp", "sl"])
      $("#t-amend-" + id).value = "";
    $("#t-amend-price").placeholder = o.price;
    $("#t-amend-qty").placeholder = o.qty;
    $("#t-amend-trigger").placeholder = o.triggerPrice || "非条件单留空";
    $("#t-amend-dialog").showModal();
  }
  function selectedOrders() {
    const rows = t.rows.filter((o) => t.selected.has(o.orderId));
    if (!rows.length || rows.length > 20)
      throw new Error("请选择 1 至 20 笔当前委托");
    return rows;
  }
  function openPosition(p) {
    t.position = { position: p, key: key() };
    $("#t-position-info").textContent =
      `${p.symbol} · ${p.side === "Buy" ? "多仓" : "空仓"} · 数量 ${p.size} · 索引 ${p.positionIdx}`;
    $("#t-pos-mode").value = "Full";
    $("#t-pos-partial").hidden = true;
    for (const id of ["tp", "sl", "trailing", "active", "tp-limit", "sl-limit"])
      $("#t-pos-" + id).value = "";
    $("#t-pos-tp").placeholder = p.takeProfit || "未设置";
    $("#t-pos-sl").placeholder = p.stopLoss || "未设置";
    $("#t-pos-trailing").placeholder = p.trailingStop || "绝对价格距离";
    $("#t-pos-size").value = p.size;
    $("#t-position-dialog").showModal();
  }
  function openClose(p) {
    t.position = { position: p, key: key() };
    $("#t-close-info").textContent =
      `${p.symbol} · ${p.side === "Buy" ? "多仓" : "空仓"} · 当前数量 ${p.size} · 索引 ${p.positionIdx}`;
    $("#t-close-percent").value = "100";
    $("#t-close-dialog").showModal();
  }
  function sameEdit(target) {
    if (!target || target.key !== key())
      throw new Error("账户或合约已切换，请重新打开操作窗口");
  }
  function initialize() {
    if (t.initialized) return;
    t.initialized = true;
    $("#trade-profile").onchange = contextChanged;
    $("#trade-symbol").addEventListener("input", () => {
      t.generation++;
      t.context = null;
      invalidate();
      $("#t-market").hidden = true;
      resetTable();
      clear($("#t-submissions"));
      clear($("#t-expiry-jobs"));
      $("#t-context-status").textContent = "合约代码已更改，请重新载入。";
    });
    $("#trade-symbol").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        contextChanged();
      }
    });
    $("#t-load-context").onclick = () =>
      busy($("#t-load-context"), async () => contextChanged());
    $("#t-kind").onchange = setKind;
    $("#t-amount-mode").onchange = () => {
      const mode = $("#t-amount-mode").value;
      $("#t-amount-label").textContent = {
        qty: "基础资产数量",
        notional: "名义金额（USDT）",
        margin: "保证金预算（USDT）",
      }[mode];
      $("#t-sizing-note").textContent =
        mode === "margin"
          ? "预算按当前账户实际杠杆换算，并向下取数量步长；实际占用受保证金模式、手续费与其他持仓影响。"
          : mode === "notional"
            ? "按委托价格或市价参考价换算基础资产数量，向下取数量步长。"
            : "按基础资产计量；预览时校验数量步长和最小金额。";
      invalidate();
    };
    for (const prefix of ["tp", "sl"])
      $("#t-" + prefix + "-type").onchange = () => {
        $("#t-" + prefix + "-limit-wrap").hidden =
          $("#t-" + prefix + "-type").value !== "Limit";
        invalidate();
      };
    for (const b of $$("[data-fill-price]"))
      b.onclick = () =>
        busy(b, async () => {
          await requireContext();
          $("#t-price").value = t.context.ticker[b.dataset.fillPrice] || "";
          invalidate();
        });
    for (const id of ["#trade-form", "#t-batch-form"])
      $(id).addEventListener("input", invalidate);
    $("#trade-form").onsubmit = (e) => {
      e.preventDefault();
      busy(e.submitter, async () => {
        await requireContext();
        await previewOrder(draft());
      });
    };
    $("#t-batch-form").onsubmit = (e) => {
      e.preventDefault();
      busy(e.submitter, async () => {
        await requireContext();
        await previewOrder(batchDraft());
      });
    };
    $("#t-add-batch-row").onclick = () =>
      busy($("#t-add-batch-row"), async () => addBatchRow());
    addBatchRow();
    addBatchRow();
    for (const [id, batch] of [
      ["#t-single-tab", false],
      ["#t-batch-tab", true],
    ])
      $(id).onclick = () => {
        $("#trade-form").hidden = batch;
        $("#t-batch-form").hidden = !batch;
        for (const [tabId, value] of [
          ["#t-single-tab", !batch],
          ["#t-batch-tab", batch],
        ]) {
          $(tabId).classList.toggle("active", value);
          $(tabId).setAttribute("aria-selected", String(value));
        }
        invalidate();
      };
    $("#t-batch-advanced-toggle").onchange = () => {
      const advanced = $("#t-batch-advanced-toggle").checked;
      $("#t-batch-advanced").hidden = !advanced;
      if (advanced && $("#t-batch-json").value.trim() === "[]")
        $("#t-batch-json").value = JSON.stringify(
          batchDraft(true).orders,
          null,
          2,
        );
      invalidate();
    };
    $("#t-submit-preview").onclick = () =>
      submitPreview().catch((e) => toast(e.message, true));
    $("#t-discard-preview").onclick = invalidate;
    $("#t-refresh-table").onclick = () =>
      busy($("#t-refresh-table"), () => refreshAll(true));
    $("#t-refresh-managed").onclick = () =>
      busy($("#t-refresh-managed"), loadManaged);
    for (const b of $$("[data-trade-tab]"))
      b.onclick = () => {
        t.tab = b.dataset.tradeTab;
        resetTable();
        for (const tab of $$("[data-trade-tab]")) {
          const active = tab === b;
          tab.classList.toggle("active", active);
          tab.setAttribute("aria-selected", String(active));
        }
        if ($("#trade-profile").value)
          loadTable().catch((e) => toast(e.message, true));
      };
    $("#t-page-next").onclick = () =>
      busy($("#t-page-next"), async () => {
        if (!t.nextCursor) return;
        t.cursors.push(t.nextCursor);
        t.selected.clear();
        await loadTable();
      });
    $("#t-page-prev").onclick = () =>
      busy($("#t-page-prev"), async () => {
        if (t.cursors.length < 2) return;
        t.cursors.pop();
        t.selected.clear();
        await loadTable();
      });
    $("#t-batch-cancel").onclick = () =>
      busy($("#t-batch-cancel"), async () => {
        const orders = selectedOrders();
        if (
          await confirmMutation(
            "批量撤单",
            `撤销选中的 ${orders.length} 笔委托未成交部分。`,
          )
        ) {
          operationResult(
            await act("batch_cancel", {
              orders: orders.map((o) => ({ orderId: o.orderId })),
            }),
          );
          t.selected.clear();
          await refreshAll(true);
        }
      });
    $("#t-cancel-symbol").onclick = () =>
      busy($("#t-cancel-symbol"), async () => {
        const p = account();
        if (
          !(await confirmMutation(
            "撤销当前合约全部委托",
            "包含当前合约尚未成交的普通单及条件单。",
          ))
        )
          return;
        const result = await execute(
          p,
          "POST /v5/order/cancel-all",
          { category: "linear", symbol: symbol() },
          false,
        );
        t.dataEpoch++;
        operationResult({ response: result });
        await refreshAll(true);
      });
    $("#t-amend-form").onsubmit = (e) => {
      e.preventDefault();
      busy(e.submitter, async () => {
        sameEdit(t.edit);
        const changes = {};
        for (const [id, k] of [
          ["price", "price"],
          ["qty", "qty"],
          ["trigger", "triggerPrice"],
          ["tp", "takeProfit"],
          ["sl", "stopLoss"],
        ])
          if (field("#t-amend-" + id)) changes[k] = field("#t-amend-" + id);
        if (!Object.keys(changes).length) throw new Error("请填写至少一项修改");
        if (
          !(await confirmMutation(
            "确认改单",
            `订单 ${t.edit.order.orderId}\n${JSON.stringify(changes)}`,
          ))
        )
          return;
        const r = await act("amend", {
          order_id: t.edit.order.orderId,
          changes,
        });
        $("#t-amend-dialog").close();
        operationResult(r);
        await refreshAll(true);
      });
    };
    $("#t-batch-amend").onclick = () =>
      busy($("#t-batch-amend"), async () => {
        const orders = selectedOrders();
        t.edit = { orders, key: key() };
        const rows = clear($("#t-batch-amend-rows"));
        for (const o of orders) {
          const row = node("div", "batch-amend-row");
          row.dataset.orderId = o.orderId;
          row.append(
            node(
              "p",
              "small muted",
              `${o.orderId} · 已成交 ${o.cumExecQty || "0"}`,
            ),
          );
          for (const field of ["price", "qty"]) {
            const label = node(
                "label",
                "",
                field === "price" ? "新委托价格" : "新总数量",
              ),
              input = node("input");
            input.inputMode = "decimal";
            input.dataset.field = field;
            input.placeholder = o[field];
            label.append(input);
            row.append(label);
          }
          rows.append(row);
        }
        $("#t-batch-amend-dialog").showModal();
      });
    $("#t-batch-amend-form").onsubmit = (e) => {
      e.preventDefault();
      busy(e.submitter, async () => {
        sameEdit(t.edit);
        const orders = [...$("#t-batch-amend-rows").children]
          .map((row) => {
            const o = { orderId: row.dataset.orderId };
            for (const input of row.querySelectorAll("input"))
              if (input.value.trim())
                o[input.dataset.field] = input.value.trim();
            return o;
          })
          .filter((o) => Object.keys(o).length > 1);
        if (!orders.length) throw new Error("请填写至少一笔修改");
        if (
          !(await confirmMutation(
            "确认批量改单",
            `提交 ${orders.length} 笔修改，分别核查结果。`,
          ))
        )
          return;
        const r = await act("batch_amend", { orders });
        $("#t-batch-amend-dialog").close();
        operationResult(r);
        await refreshAll(true);
      });
    };
    $("#t-open-leverage").onclick = () =>
      busy($("#t-open-leverage"), async () => {
        await requireContext();
        t.edit = { key: key() };
        const f = t.context.instrument.leverageFilter || {};
        $("#t-leverage-note").textContent =
          `${symbol()} · 允许 ${f.minLeverage || "—"} 至 ${f.maxLeverage || "—"}× · 步长 ${f.leverageStep || "—"}`;
        const get = (idx) =>
          t.context.positions.find((p) => p.positionIdx === idx)?.leverage ||
          t.context.positions[0]?.leverage ||
          "";
        $("#t-buy-leverage").value = get(1);
        $("#t-sell-leverage").value = get(2);
        $("#t-leverage-dialog").showModal();
      });
    $("#t-leverage-form").onsubmit = (e) => {
      e.preventDefault();
      busy(e.submitter, async () => {
        sameEdit(t.edit);
        if (
          !(await confirmMutation(
            "修改实际杠杆",
            `多仓 ${field("#t-buy-leverage")}× / 空仓 ${field("#t-sell-leverage")}×`,
          ))
        )
          return;
        const r = await act("leverage", {
          buy_leverage: field("#t-buy-leverage"),
          sell_leverage: field("#t-sell-leverage"),
        });
        $("#t-leverage-dialog").close();
        invalidate();
        operationResult(r);
        await refreshAll(true);
      });
    };
    $("#t-pos-mode").onchange = () =>
      ($("#t-pos-partial").hidden = $("#t-pos-mode").value !== "Partial");
    $("#t-open-demo-funds").onclick = () =>
      busy($("#t-open-demo-funds"), async () => {
        const p = account();
        if (p.environment !== "demo")
          throw new Error("模拟资金只能申请至 Demo 账户");
        if (!p.scopes.includes("bybit:funds"))
          throw new Error("请先在账户连接的设置中启用资金权限");
        $("#t-funds-dialog").showModal();
      });
    $("#t-funds-form").onsubmit = (e) => {
      e.preventDefault();
      busy(e.submitter, async () => {
        const p = account();
        if (p.environment !== "demo") throw new Error("请选择 Demo 账户");
        const r = await execute(
          p,
          "POST /v5/account/demo-apply-money",
          {
            adjustType: 0,
            utaDemoApplyMoney: [
              { coin: "USDT", amountStr: field("#t-funds-amount") },
            ],
          },
          false,
        );
        $("#t-funds-dialog").close();
        t.dataEpoch++;
        operationResult({ response: r });
        await refreshAll(true);
      });
    };
    $("#t-position-form").onsubmit = (e) => {
      e.preventDefault();
      busy(e.submitter, async () => {
        sameEdit(t.position);
        const params = {
          positionIdx: t.position.position.positionIdx,
          tpslMode: $("#t-pos-mode").value,
        };
        for (const [id, k] of [
          ["tp", "takeProfit"],
          ["sl", "stopLoss"],
          ["trailing", "trailingStop"],
          ["active", "activePrice"],
        ])
          if (field("#t-pos-" + id)) params[k] = field("#t-pos-" + id);
        if (params.takeProfit) params.tpTriggerBy = $("#t-pos-tp-by").value;
        if (params.stopLoss) params.slTriggerBy = $("#t-pos-sl-by").value;
        if (params.tpslMode === "Partial") {
          params.tpSize = params.slSize = field("#t-pos-size");
          params.tpOrderType = $("#t-pos-tp-type").value;
          params.slOrderType = $("#t-pos-sl-type").value;
          if (params.tpOrderType === "Limit")
            params.tpLimitPrice = field("#t-pos-tp-limit");
          if (params.slOrderType === "Limit")
            params.slLimitPrice = field("#t-pos-sl-limit");
        }
        if (
          !(await confirmMutation(
            "设置仓位保护",
            "将修改所选仓位的止盈止损或移动止损。",
          ))
        )
          return;
        const r = await act("stops", { params });
        $("#t-position-dialog").close();
        operationResult({ response: r });
        await refreshAll(true);
      });
    };
    $("#t-close-form").onsubmit = (e) => {
      e.preventDefault();
      busy(e.submitter, async () => {
        sameEdit(t.position);
        const p = account(),
          s = stamp();
        const result = await api("trading/close-preview", {
          method: "POST",
          body: JSON.stringify({
            profile_id: p.id,
            symbol: symbol(),
            position_idx: t.position.position.positionIdx,
            percent: Number($("#t-close-percent").value),
          }),
        });
        if (!current(s)) return;
        $("#t-close-dialog").close();
        renderPreview(result);
      });
    };
    for (const b of $$("[data-close-dialog]"))
      b.onclick = () => $("#" + b.dataset.closeDialog).close();
    for (const id of [
      "#t-page-prev",
      "#t-page-next",
      "#t-batch-cancel",
      "#t-batch-amend",
    ]) {
      const b = $(id),
        handler = b.onclick;
      b.onclick = async () => {
        await handler();
        selectionChanged();
        $("#t-page-prev").disabled = t.cursors.length === 1;
        $("#t-page-next").disabled = !t.nextCursor;
      };
    }
    setKind();
    renderTable();
    setInterval(() => {
      if (
        !document.hidden &&
        location.hash === "#trading" &&
        $("#t-auto-refresh").checked &&
        $("#trade-profile").value &&
        /^[A-Z0-9_]+USDT$/.test(symbol())
      )
        refreshAll(false);
    }, 5000);
  }
  window.Trading = { contextChanged, activate, state: t };
  initialize();
})();
