(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  const windowBarEl = document.getElementById("windowbar");

  let tab = "batches";
  let templates = [];
  let batches = [];
  let reports = [];
  let selected = null;
  let stampValue = "";
  let traceValue = "WM-ORD-131";
  let traceResult = null;

  // 窗界读数：全页只保留这一份。打开页时取一次，落标前后各刷一次；
  // 所有「能不能落标」的判断都读它，不在各页各写一套口径。
  let window = null;
  let windowError = "";

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(j.error || "HTTP " + r.status);
      err.status = r.status;
      err.body = j;
      throw err;
    }
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function esc(v) {
    return String(v === null || v === undefined ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function windowTone() {
    if (!window) return "unknown";
    if (window.expired) return "expired";
    if (window.not_started) return "pending";
    return "open";
  }

  function windowShort() {
    if (!window) return "窗界未读到";
    if (window.expired) return "窗外禁落标";
    if (window.not_started) return "窗未生效";
    return "窗内可落标";
  }

  function renderWindowBar() {
    if (!window) {
      windowBarEl.innerHTML = `
        <div class="windowbar-main">
          <span class="chip tone-unknown">窗界未读到</span>
          <span class="muted">${esc(windowError || "正在读取窗界…")}</span>
        </div>`;
      return;
    }
    const tone = windowTone();
    windowBarEl.innerHTML = `
      <div class="windowbar-main">
        <span class="chip tone-${tone}" data-window-chip="1">${esc(window.status)}</span>
        <span class="window-range">${esc(window.window_start)} ~ ${esc(window.window_end)}</span>
        <span class="muted">读数源：${esc(window.source_label)} · 第 ${esc(window.revision)} 版</span>
      </div>
      <div class="windowbar-meter" title="窗界进度">
        <div class="meter-track"><div class="meter-fill tone-${tone}" style="width:${Number(window.progress) || 0}%"></div></div>
        <span class="muted" data-window-progress="1">${esc(window.days_elapsed)}/${esc(window.days_total)} 天 · ${
          window.expired ? "已过期" : "剩 " + esc(window.days_left) + " 天"
        }</span>
      </div>
      <details class="window-edit">
        <summary>调度改窗</summary>
        <div class="row">
          <input id="window-start" value="${esc(window.window_start)}" aria-label="窗界起始" />
          <input id="window-end" value="${esc(window.window_end)}" aria-label="窗界截止" />
          <button type="button" class="ghost" id="btn-window">提交改窗</button>
        </div>
        <p class="muted">提交时带上第 ${esc(window.revision)} 版做乐观校验；后端按排他事务写入，双人争用只落一版。</p>
      </details>`;

    const btnWindow = document.getElementById("btn-window");
    if (btnWindow) {
      btnWindow.onclick = async () => {
        setMsg("");
        try {
          const j = await api("/api/window", {
            method: "POST",
            body: JSON.stringify({
              window_start: document.getElementById("window-start").value,
              window_end: document.getElementById("window-end").value,
              expected_revision: window ? window.revision : null,
            }),
          });
          window = j.window;
          windowError = "";
          setMsg("窗界已更新：第 " + window.revision + " 版 · " + window.status);
          await refreshBatches();
          renderWindowBar();
          render();
        } catch (e) {
          setMsg(e.message);
          await refreshWindow();
          render();
        }
      };
    }
  }

  async function refreshWindow() {
    try {
      const j = await api("/api/window");
      window = j.window;
      windowError = "";
    } catch (e) {
      window = null;
      windowError = e.message;
    }
    renderWindowBar();
  }

  async function refresh() {
    const meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    if (meta.window) {
      window = meta.window;
      windowError = "";
    }
    templates = (await api("/api/templates")).items || [];
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/gap-reports")).items || [];
    renderWindowBar();
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  function stampStateChip(b) {
    if (b.status === "待盖印") {
      return `<span class="chip tone-pending">待盖印</span>`;
    }
    if (b.status === "已作废") {
      return `<span class="chip tone-expired">已作废</span>`;
    }
    if (b.stamp_state === "valid") {
      return `<span class="chip tone-open">已盖印</span>`;
    }
    if (b.stamp_state === "invalid") {
      return `<span class="chip tone-expired" title="${esc((b.stamp_reasons || []).join("；"))}">盖印失效</span>`;
    }
    return `<span class="chip">${esc(b.status)}</span>`;
  }

  // 窗源目录：模板版式 + 各自生效窗 + 与现行窗界的关系。
  function renderTemplates() {
    return `
      <h2>窗源目录</h2>
      <p class="muted">窗界只认顶栏一份读数；下表列出各版式的生效窗，未生效版不得顶上落标。</p>
      <div class="chips">
        <span class="chip tone-${windowTone()}" data-window-source-chip="1">${esc(window ? window.status : "窗界未读到")}</span>
        <span class="chip">窗源：${esc(window ? window.source_label : windowError || "-")}</span>
        <span class="chip">窗界 ${esc(window ? window.window_start + " ~ " + window.window_end : "-")}</span>
        <span class="chip">第 ${esc(window ? window.revision : "-")} 版</span>
      </div>
      <div class="cards">
        ${["dw_orders_d", "dw_payments_d", "dm_gmv_d"]
          .map((table) => {
            const rows = templates.filter((t) => t.table_name === table);
            if (!rows.length) return "";
            return `<article class="card" data-table-card="${esc(table)}">
              <header class="card-head">
                <h3>${esc(table)}</h3>
                <span class="chip tone-${windowTone()}">${esc(windowShort())}</span>
              </header>
              <ul class="version-list">
                ${rows
                  .map(
                    (t) => `<li>
                      <span class="chip">v${esc(t.version_no)} · ${esc(t.status)}</span>
                      <span class="muted">生效窗 ${esc(t.effective_from)} ~ ${esc(t.effective_to)}</span>
                      <span class="muted">列 ${esc(t.column_name)}</span>
                    </li>`
                  )
                  .join("")}
              </ul>
            </article>`;
          })
          .join("")}
      </div>`;
  }

  function renderBatches() {
    return `
      <h2>运行批次</h2>
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账。落标以顶栏窗界一次裁定。</p>
      <table>
        <thead><tr><th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>状态</th><th>版式</th><th>水印</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${esc(b.id)}</td>
            <td>${esc(b.table_name)}</td>
            <td>${esc(b.run_code)}</td>
            <td>${esc(b.biz_date)}</td>
            <td>${stampStateChip(b)}</td>
            <td>${b.template_version ? "v" + esc(b.template_version) : "-"}</td>
            <td>${esc(b.watermark_value || "-")}</td>
            <td>${
              b.status === "待盖印"
                ? `<button type="button" class="ghost" data-pick="${esc(b.id)}">落标</button>`
                : ""
            }</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      ${renderStampPanel()}`;
  }

  // 落标附页：读出与顶栏同一份窗界，附「读数芯片」并把裁定理由写清。
  function renderStampPanel() {
    if (!selected) return "";
    const biz = String(selected.biz_date || "");
    const inWindow = !!window && window.window_start <= biz && biz <= window.window_end;
    const allowed = !!window && window.can_stamp && inWindow;
    const reasons = [];
    if (!window) reasons.push("窗界未读到");
    else {
      if (window.expired) reasons.push("窗界已过期");
      if (window.not_started) reasons.push("窗界未生效");
      if (!inWindow) reasons.push("业务日不在窗界内");
    }
    return `<div class="cardish" id="stamp-panel">
        <h3>落标附页</h3>
        <p>${esc(selected.run_code)} · ${esc(selected.table_name)} · 业务日 ${esc(biz)}</p>
        <div class="chips">
          <span class="chip tone-${windowTone()}" data-stamp-window-chip="1">${esc(window ? window.status : "窗界未读到")}</span>
          <span class="chip" data-stamp-inwindow="1">${inWindow ? "业务日在窗内" : "业务日在窗外"}</span>
          <span class="chip ${allowed ? "tone-open" : "tone-expired"}" data-stamp-verdict="1">${allowed ? "页面预判：可落标" : "页面预判：不可落标"}</span>
        </div>
        <p class="muted">读数芯片：${esc(window ? window.window_start + " ~ " + window.window_end + " · 第 " + window.revision + " 版 · " + window.source_label : windowError || "未读到")}</p>
        ${allowed ? "" : `<p class="err" data-stamp-blocked="1">窗口读数：${esc(reasons.join("；") || "窗界不可落标")} · 提交仍会由后端复核</p>`}
        <label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
        <button type="button" id="btn-stamp">写入盖印</button>
      </div>`;
  }

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const rejected = (traceResult && traceResult.rejected_sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    const rejectedDown = (traceResult && traceResult.rejected_downstream) || [];
    return `
      <h2>下游追索</h2>
      <p class="muted">追索复验一轮：窗外补印或未生效版盖的印，不再被顺藤摸到。</p>
      <label>水印值<input id="trace-input" value="${esc(traceValue)}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        <h3>源批次</h3>
        <ul>${src.map((s) => `<li>${esc(s.run_code)} · ${esc(s.table_name)} · v${esc(s.template_version || "-")}</li>`).join("")}</ul>
        <h3>下游</h3>
        <ul>${down
          .map(
            (d) =>
              `<li>${esc(d.run_code)} · ${esc(d.status)} · ${esc(d.watermark_value || "未盖印")}</li>`
          )
          .join("")}</ul>
        ${
          rejected.length || rejectedDown.length
            ? `<h3>复验剔除</h3>
               <ul class="err">${rejected
                 .concat(rejectedDown)
                 .map(
                   (r) =>
                     `<li>${esc(r.run_code)} · ${esc((r.stamp_reasons || []).join("；"))}</li>`
                 )
                 .join("")}</ul>`
            : ""
        }
      </div>`
          : ""
      }`;
  }

  function renderGaps() {
    return `
      <h2>缺口对账</h2>
      <p class="muted">对账口径与落标同源：窗外批次单列，不混进缺口账。</p>
      <button type="button" id="btn-gap">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>应盖印</th><th>已盖印</th><th>缺口</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${esc(r.id)}</td>
            <td>${esc(r.as_of_date)}</td>
            <td>${esc(r.expected_count)}</td>
            <td>${esc(r.stamped_count)}</td>
            <td>${esc(r.gap_count)}</td>
            <td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else view.innerHTML = renderGaps();

    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = batches.find((b) => b.id === id) || null;
        if (selected) {
          stampValue = "WM-" + selected.run_code.toUpperCase();
        }
        render();
      };
    });

    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      const btn = document.getElementById("btn-stamp");
      btn.onclick = async () => {
        setMsg("");
        const targetId = selected.id;
        try {
          await api("/api/batches/" + targetId + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成");
          render();
        } catch (e) {
          setMsg(e.message);
          // 窗外静默成功是不允许的；一旦被拦，立刻重读现行窗并重绘读数。
          await refreshWindow();
          await refreshBatches();
          render();
        }
      };
    }

    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg("追索完成");
          render();
        } catch (e) {
          traceResult = null;
          setMsg(e.message);
          render();
        }
      };
    }

    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({
              as_of_date: (window && window.today) || "2026-11-16",
            }),
          });
          await refresh();
          setMsg(r.report.summary);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
  }

  async function refreshBatches() {
    batches = (await api("/api/batches")).items || [];
    if (selected) {
      selected = batches.find((b) => b.id === selected.id) || null;
    }
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncDock();
      render();
    };
  });

  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  // 打开页时冻住一次窗界快照（不随后台静默漂移），落标时再由后端按提交瞬间重读裁定。
  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message));
})();
