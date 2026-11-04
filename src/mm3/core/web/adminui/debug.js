// The admin debug page /admin/debug (core spec 10.5): overview, live logs and
// bus inspector, fed by /admin/ws and the REST API.
// Implements CORE-305 (Core spec 0.22, §9.1 Remote-control API): status with
// mismatches and failures highlighted.
// Implements CORE-112 (Core spec 0.23, §10.5 Diagnostics and debugging): the
// log-level control of each instance.
// Implements CORE-114, CORE-115 (Core spec 0.22, §10.5 Diagnostics and debugging):
// browser errors are shown; the bus is subscribed only while its tab is open.
// Implements CORE-316 (Core spec 0.24, §10.5 Diagnostics and debugging): the log
// filters of the live view.
import { html, nothing, render } from "/admin/static/lit-html.js";
import { AdminChannel, api, download, fmtDuration, fmtTime, loadTexts, t } from "/admin/static/admin.js";

const MAX_ROWS = 2000;
const LEVELS = ["debug", "info", "warning", "error"];
const BASE = new Set(["ts", "level", "module", "instance", "event", "trace_id"]);

function fields(record) {
  const out = {};
  for (const [k, v] of Object.entries(record)) if (!BASE.has(k)) out[k] = v;
  return out;
}

function short(value, n = 120) {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text && text.length > n ? `${text.slice(0, n)}…` : text || "";
}

class AdminDebug extends HTMLElement {
  constructor() {
    super();
    this.tab = "overview";
    this.status = null;
    this.modules = [];
    this.openRow = null;
    this.detail = null;
    this.browserErrors = [];
    this.loggers = [];
    this.logs = [];
    this.logFilter = { level: "info", module: "", instance: "", trace: "", text: "" };
    this.logPaused = false;
    this.bus = [];
    this.busFilter = { name: "", source: "" };
    this.busPaused = false;
    this.busSearch = "";
    this.selected = null;
    this.trace = null;
    this.dropped = {};
    this.live = false;
    this.message = "";
  }

  async connectedCallback() {
    await loadTexts();
    this.channel = new AdminChannel(
      (frame) => this.onFrame(frame),
      (on) => {
        this.live = on;
        this.update();
      },
    );
    this.channel.subscribe("status");
    await this.refresh();
  }

  // --- data -------------------------------------------------------------------------

  async refresh() {
    try {
      this.modules = await api("GET", "/modules");
      const page = await api("GET", "/debug/logs?limit=500&level=warning");
      this.browserErrors = page.items.filter((r) => r.source === "browser").slice(0, 20);
      if (this.openRow) await this.loadDetail(this.openRow);
    } catch (err) {
      this.message = err.message;
    }
    this.update();
  }

  async loadDetail(id) {
    const [health, page] = await Promise.all([
      api("GET", `/modules/${encodeURIComponent(id)}/health`),
      api("GET", `/debug/logs?limit=50&instance=${encodeURIComponent(id)}`),
    ]);
    this.detail = { health, logs: page.items };
  }

  onFrame(frame) {
    const { kind, data } = frame;
    if (kind === "status") {
      const before = this.status;
      this.status = data.status;
      if (before) this.refresh();
    } else if (kind === "log") {
      if (this.logPaused) return;
      this.logs.unshift(data.record);
      if (this.logs.length > MAX_ROWS) this.logs.length = MAX_ROWS;
    } else if (kind === "bus") {
      if (this.busPaused) return;
      this.bus.unshift(data.entry);
      if (this.bus.length > MAX_ROWS) this.bus.length = MAX_ROWS;
    } else if (kind === "dropped") {
      this.dropped[data.topic] = (this.dropped[data.topic] || 0) + data.count;
    }
    this.update();
  }

  setTab(tab) {
    if (tab === this.tab) return;
    if (this.tab === "logs") this.channel.unsubscribe("logs");
    if (this.tab === "bus") this.channel.unsubscribe("bus");
    this.tab = tab;
    if (tab === "logs") this.subscribeLogs();
    if (tab === "bus") this.subscribeBus();
    if (tab === "overview") this.refresh();
    this.update();
  }

  subscribeLogs() {
    this.logs = [];
    this.dropped.logs = 0;
    const filter = {};
    for (const [k, v] of Object.entries(this.logFilter)) if (v) filter[k] = v;
    this.channel.subscribe("logs", filter);
  }

  subscribeBus() {
    this.bus = [];
    this.dropped.bus = 0;
    const filter = {};
    for (const [k, v] of Object.entries(this.busFilter)) if (v) filter[k] = v;
    this.channel.subscribe("bus", filter);
  }

  async toggleRow(id) {
    if (this.openRow === id) {
      this.openRow = null;
      this.detail = null;
    } else {
      this.openRow = id;
      this.detail = null;
      try {
        await this.loadDetail(id);
      } catch (err) {
        this.message = err.message;
      }
    }
    this.update();
  }

  async setLevel(id, value) {
    try {
      await api("PUT", `/modules/${encodeURIComponent(id)}/log-level`, { level: value || null });
      this.message = "";
    } catch (err) {
      this.message = err.message;
    }
    this.update();
  }

  async restart(id) {
    try {
      await api("POST", `/modules/${encodeURIComponent(id)}/restart`);
      await this.refresh();
    } catch (err) {
      this.message = err.message;
      this.update();
    }
  }

  async followTrace(trace) {
    if (!trace) return;
    try {
      const page = await api("GET", `/debug/logs?limit=200&trace=${encodeURIComponent(trace)}`);
      this.trace = {
        id: trace,
        logs: page.items,
        entries: this.bus.filter((e) => e.trace === trace),
      };
    } catch (err) {
      this.message = err.message;
    }
    this.update();
  }

  async resend(entry) {
    try {
      await api("POST", "/notify", { name: entry.name, payload: entry.payload || {}, target: entry.target });
      this.message = t("CORE_ADMIN_RESENT");
    } catch (err) {
      this.message = err.message;
    }
    this.update();
  }

  // --- rendering --------------------------------------------------------------------

  update() {
    render(this.view(), this);
  }

  view() {
    const tab = (id, key) =>
      html`<button class=${this.tab === id ? "active" : ""} @click=${() => this.setTab(id)}>${t(key)}</button>`;
    return html`
      <nav class="tabs">
        ${tab("overview", "CORE_ADMIN_TAB_OVERVIEW")} ${tab("logs", "CORE_ADMIN_TAB_LOGS")}
        ${tab("bus", "CORE_ADMIN_TAB_BUS")}
        <span class="spacer"></span>
        <span class="live ${this.live ? "on" : "off"}">
          ${t(this.live ? "CORE_ADMIN_LIVE" : "CORE_ADMIN_RECONNECTING")}
        </span>
      </nav>
      ${this.message ? html`<p class="alert warn" role="status">${this.message}</p>` : nothing}
      ${this.tab === "overview" ? this.overview() : this.tab === "logs" ? this.logsView() : this.busView()}
    `;
  }

  alerts(s) {
    const out = [];
    if (s.config && s.config.disk_valid === false) out.push(t("CORE_ADMIN_DISK_INVALID"));
    const k = s.kiosk;
    if (k) {
      if (!k.connected) out.push(t("CORE_ADMIN_KIOSK_DISCONNECTED"));
      if (k.display && k.display.mismatch) out.push(t("CORE_ADMIN_DISPLAY_MISMATCH"));
      if (k.chromium && k.chromium.failed) out.push(t("CORE_ADMIN_CHROMIUM_FAILED"));
      if (k.audio && k.audio.mismatch) out.push(t("CORE_ADMIN_AUDIO_MISMATCH"));
      if (k.audio && k.audio.failure) out.push(`${t("CORE_ADMIN_AUDIO_FAILURE")}: ${k.audio.failure}`);
    }
    for (const m of this.modules) {
      if (m.state === "FAILED" || m.state === "DISABLED") out.push(`${m.id}: ${m.state} ${m.reason || ""}`);
    }
    return out;
  }

  overview() {
    const s = this.status;
    if (!s) return html`<p class="muted">…</p>`;
    const k = s.kiosk;
    const stat = (key, value) => html`<dt>${t(key)}</dt><dd>${value}</dd>`;
    return html`
      ${this.alerts(s).map((a) => html`<p class="alert" role="alert">${a}</p>`)}
      <div class="grid">
        <section class="card"><h2>${t("CORE_ADMIN_ENGINE")}</h2>
          <dl class="stat">
            ${stat("CORE_ADMIN_VERSION", `${s.version} (${s.engine_build})`)}
            ${stat("CORE_ADMIN_UPTIME", fmtDuration(s.uptime_s))}
            ${stat("CORE_ADMIN_CONFIG_VERSION", s.config ? s.config.version : "–")}
            ${stat("CORE_ADMIN_PAGE", `${s.page.id} (${s.page.index + 1}/${s.page.count})`)}
            ${s.suspended ? stat("CORE_ADMIN_SUSPENDED", "✓") : nothing}
          </dl>
          <a class="button" href="/api/v1/debug/bundle" download>${t("CORE_ADMIN_DOWNLOAD_BUNDLE")}</a>
        </section>
        <section class="card"><h2>${t("CORE_ADMIN_CLIENTS")}</h2>
          ${s.clients.length
            ? html`<table><tbody>${s.clients.map(
                (c) => html`<tr><td>${c.role}</td><td class="mono">${c.address || ""}</td><td>${fmtTime(c.since)}</td></tr>`,
              )}</tbody></table>`
            : html`<p class="muted">${t("CORE_ADMIN_NONE")}</p>`}
        </section>
        <section class="card"><h2>${t("CORE_ADMIN_KIOSK")}</h2>
          ${k
            ? html`<dl class="stat">
                ${stat("CORE_ADMIN_STATE", k.connected ? t("CORE_ADMIN_LIVE") : t("CORE_ADMIN_KIOSK_DISCONNECTED"))}
                ${k.display && k.display.viewport ? stat("CORE_ADMIN_VIEWPORT", k.display.viewport.join(" × ")) : nothing}
                ${k.chromium ? stat("CORE_ADMIN_CHROMIUM", `${k.chromium.state || "–"} · ${k.chromium.restarts}`) : nothing}
              </dl>`
            : html`<p class="muted">${t("CORE_ADMIN_KIOSK_NONE")}</p>`}
        </section>
      </div>
      <section class="card"><h2>${t("CORE_ADMIN_INSTANCES")}</h2>
        <table>
          <thead><tr>
            <th>${t("CORE_ADMIN_INSTANCE")}</th><th>${t("CORE_ADMIN_MODULE")}</th>
            <th>${t("CORE_ADMIN_STATE")}</th><th>${t("CORE_ADMIN_RESTARTS")}</th>
            <th>${t("CORE_ADMIN_LAST_ERROR")}</th>
          </tr></thead>
          <tbody>${this.modules.map((m) => this.instanceRows(m))}</tbody>
        </table>
      </section>
      <section class="card"><h2>${t("CORE_ADMIN_BROWSER_ERRORS")}</h2>
        ${this.browserErrors.length
          ? html`<table><tbody>${this.browserErrors.map(
              (r) => html`<tr>
                <td class="nowrap">${fmtTime(r.ts)}</td><td>${r.instance || ""}</td>
                <td>${r.message}${r.stack ? html`<pre>${r.stack}</pre>` : nothing}</td>
                <td class="muted">${short(r.user_agent, 60)}</td>
              </tr>`,
            )}</tbody></table>`
          : html`<p class="muted">${t("CORE_ADMIN_NONE")}</p>`}
      </section>`;
  }

  instanceRows(m) {
    const open = this.openRow === m.id;
    const err = m.health && m.health.last_error;
    const row = html`<tr class="clickable ${open ? "open" : ""}" @click=${() => this.toggleRow(m.id)}>
      <td>${m.id}</td><td>${m.module} <span class="muted">${m.version}</span></td>
      <td class="state-${m.state}">${m.state}${m.hidden.length ? html` <span class="muted">(${m.hidden.join(", ")})</span>` : nothing}</td>
      <td>${m.health ? m.health.restarts : 0}</td>
      <td>${err ? short(err.message, 80) : ""}</td>
    </tr>`;
    if (!open) return row;
    const d = this.detail;
    return html`${row}<tr><td colspan="5"><div class="detail">
      <div class="row">
        <label>${t("CORE_ADMIN_LOG_LEVEL")}
          <select @change=${(ev) => this.setLevel(m.id, ev.target.value)}>
            <option value="">${t("CORE_ADMIN_LEVEL_DEFAULT")}</option>
            ${LEVELS.map((l) => html`<option value=${l}>${l}</option>`)}
          </select>
        </label>
        <button @click=${() => this.restart(m.id)}>${t("CORE_ADMIN_RESTART")}</button>
      </div>
      ${err && err.traceback ? html`<h3>${t("CORE_ADMIN_LAST_ERROR")}</h3><pre>${err.traceback}</pre>` : nothing}
      ${d
        ? html`<h3>${t("CORE_ADMIN_HEALTH")}</h3><pre>${JSON.stringify(d.health, null, 2)}</pre>
            <h3>${t("CORE_ADMIN_RECENT_LOGS")}</h3>${this.logTable(d.logs)}`
        : html`<p class="muted">…</p>`}
    </div></td></tr>`;
  }

  logTable(records) {
    if (!records.length) return html`<p class="muted">${t("CORE_ADMIN_NONE")}</p>`;
    return html`<div class="scroll"><table>
      <thead><tr><th>${t("CORE_ADMIN_TIME")}</th><th>${t("CORE_ADMIN_FILTER_LEVEL")}</th>
        <th>${t("CORE_ADMIN_INSTANCE")}</th><th>${t("CORE_ADMIN_EVENT")}</th><th></th></tr></thead>
      <tbody>${records.map(
        (r) => html`<tr>
          <td class="nowrap mono">${fmtTime(r.ts)}</td>
          <td class="level-${r.level}">${r.level}</td>
          <td>${r.instance || r.module || ""}</td>
          <td class="mono">${r.event}</td>
          <td class="mono">${short(fields(r))}${r.exception ? html`<pre>${r.exception}</pre>` : nothing}</td>
        </tr>`,
      )}</tbody></table></div>`;
  }

  logsView() {
    const input = (key, label) => html`<label>${t(label)}
      <input .value=${this.logFilter[key]} @change=${(ev) => {
        this.logFilter[key] = ev.target.value.trim();
        this.subscribeLogs();
        this.update();
      }}></label>`;
    return html`
      <div class="toolbar">
        <label>${t("CORE_ADMIN_FILTER_LEVEL")}
          <select @change=${(ev) => {
            this.logFilter.level = ev.target.value;
            this.subscribeLogs();
            this.update();
          }}>${LEVELS.map((l) => html`<option value=${l} ?selected=${l === this.logFilter.level}>${l}</option>`)}</select>
        </label>
        ${input("module", "CORE_ADMIN_MODULE")} ${input("instance", "CORE_ADMIN_INSTANCE")}
        ${input("trace", "CORE_ADMIN_FILTER_TRACE")} ${input("text", "CORE_ADMIN_FILTER_TEXT")}
        <button @click=${() => {
          this.logPaused = !this.logPaused;
          this.update();
        }}>${t(this.logPaused ? "CORE_ADMIN_RESUME" : "CORE_ADMIN_PAUSE")}</button>
        <button @click=${() => download("mm3-logs.jsonl", [...this.logs].reverse().map((r) => JSON.stringify(r)).join("\n") + "\n")}>
          ${t("CORE_ADMIN_DOWNLOAD_JSONL")}</button>
      </div>
      ${this.dropped.logs ? html`<p class="alert warn">${t("CORE_ADMIN_DROPPED", { count: this.dropped.logs })}</p>` : nothing}
      <section class="card">${this.logTable(this.logs)}</section>`;
  }

  busView() {
    const input = (key, label) => html`<label>${t(label)}
      <input .value=${this.busFilter[key]} @change=${(ev) => {
        this.busFilter[key] = ev.target.value.trim();
        this.subscribeBus();
        this.update();
      }}></label>`;
    const needle = this.busSearch.toLowerCase();
    const rows = needle
      ? this.bus.filter((e) => JSON.stringify(e).toLowerCase().includes(needle))
      : this.bus;
    const sel = this.selected;
    return html`
      <div class="toolbar">
        ${input("name", "CORE_ADMIN_FILTER_NAME")} ${input("source", "CORE_ADMIN_FILTER_SOURCE")}
        <label>${t("CORE_ADMIN_SEARCH")}
          <input .value=${this.busSearch} @input=${(ev) => {
            this.busSearch = ev.target.value;
            this.update();
          }}></label>
        <button @click=${() => {
          this.busPaused = !this.busPaused;
          this.update();
        }}>${t(this.busPaused ? "CORE_ADMIN_RESUME" : "CORE_ADMIN_PAUSE")}</button>
      </div>
      ${this.dropped.bus ? html`<p class="alert warn">${t("CORE_ADMIN_DROPPED", { count: this.dropped.bus })}</p>` : nothing}
      <div class="split">
        <section class="card scroll"><table>
          <thead><tr><th>${t("CORE_ADMIN_TIME")}</th><th></th><th>${t("CORE_ADMIN_NAME")}</th>
            <th>${t("CORE_ADMIN_FILTER_SOURCE")} → </th><th>${t("CORE_ADMIN_SIZE")}</th></tr></thead>
          <tbody>${rows.map(
            (e) => html`<tr class="clickable ${sel === e ? "open" : ""}" @click=${() => {
              this.selected = e;
              this.update();
            }}>
              <td class="nowrap mono">${fmtTime(e.ts)}</td><td class="muted">${e.channel}</td>
              <td class="mono">${e.name}</td><td>${e.source} → ${e.target || "*"}</td><td>${e.size}</td>
            </tr>`,
          )}</tbody></table>
          ${rows.length ? nothing : html`<p class="muted">${t("CORE_ADMIN_NONE")}</p>`}
        </section>
        <section class="card">
          ${sel
            ? html`<h2>${t("CORE_ADMIN_PAYLOAD")}</h2>
                <pre>${JSON.stringify(sel.payload, null, 2)}</pre>
                <div class="row">
                  ${sel.trace ? html`<button @click=${() => this.followTrace(sel.trace)}>${t("CORE_ADMIN_FOLLOW_TRACE")}</button>` : nothing}
                  ${sel.channel === "bus" && !sel.name.startsWith("CORE_")
                    ? html`<button class="primary" @click=${() => this.resend(sel)}>${t("CORE_ADMIN_RESEND")}</button>`
                    : nothing}
                </div>`
            : html`<p class="muted">${t("CORE_ADMIN_NONE")}</p>`}
          ${this.trace
            ? html`<h2>${t("CORE_ADMIN_TRACE_TITLE", { trace: this.trace.id })}</h2>
                ${this.trace.entries.map((e) => html`<div class="mono">${fmtTime(e.ts)} ${e.name} ${e.source} → ${e.target || "*"}</div>`)}
                ${this.logTable(this.trace.logs)}
                <button @click=${() => {
                  this.trace = null;
                  this.update();
                }}>${t("CORE_ADMIN_CLOSE")}</button>`
            : nothing}
        </section>
      </div>`;
  }
}

customElements.define("mm3-admin-debug", AdminDebug);
