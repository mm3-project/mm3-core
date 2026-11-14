// The Catalog page /admin/catalog: browse the signed index, see a module's badge, check
// report, capabilities and grant scopes, and install, update or remove it through the
// installer service.
// Implements CORE-120 (Core spec 0.44, §10.6 Module catalog): installation from the admin UI
// through the installer service, with the equivalent CLI command shown as an alternative.
// Implements CORE-133 (Core spec 0.44, §10.7 Installer service): before the confirmation the
// dialog shows the module's badge, check report, requested capabilities and grant scopes.
// Implements CORE-132 (Core spec 0.44, §10.7 Installer service): every change asks for the
// installer passphrase, and the TOTP code when it is enabled.
// Implements CORE-356 (Core spec 0.44, §10.7 Installer service): reads GET /api/v1/catalog,
// /catalog/{name}, POST /catalog/refresh, POST /installer/operations and the `installer`
// topic of /admin/ws; the passphrase and the code are sent once and never kept.
// Implements CORE-135 (Core spec 0.44, §10.7 Installer service): the "Restore previous
// version" action.
// Implements CORE-384, CORE-385, CORE-386, CORE-387 (Core spec 0.44, §9.1 Remote-control
// API): the dialogs, action rows and banners of the shared patterns in ui.js.
import { html, nothing, render } from "/admin/static/lit-html.js";
import { AdminChannel, ApiError, api, fmtTime, loadTexts, t } from "/admin/static/admin.js";
import { Messages, actionRow, banner, closeOnBackdrop } from "/admin/static/ui.js";

/** The states in which an operation is over (CORE-355). */
export const FINAL_STATES = ["done", "rolled_back", "failed"];

const SEARCH_DELAY_MS = 250;

/** `true` when the operation has reached a final state. */
export const isFinal = (op) => !!op && FINAL_STATES.includes(op.state);

/** The name of a module as pip compares it (PEP 503). */
export const normalize = (name) => String(name).toLowerCase().replace(/[-_.]+/g, "-");

/**
 * The equivalent `mm3` command of an action (CORE-120): shown next to the buttons as the
 * alternative that needs no browser.
 */
export function cliCommand(action, name, version = null) {
  if (action === "install") return `mm3 modules install ${name}${version ? `==${version}` : ""}`;
  if (action === "restore_previous") return "mm3 modules restore-previous";
  return `mm3 modules ${action} ${name}`;
}

const upper = (s) => String(s).toUpperCase();

/** The name of an action in the admin language. */
export const actionName = (action) => t(`CORE_ADMIN_CATALOG_ACTION_${upper(action)}`);

/**
 * Why an action cannot run now, as `{key, params}`, or null when it can (CORE-386). The
 * installer's state comes first, then what is true of the module. `installer` is the answer
 * of `GET /api/v1/installer` (or the `status` of an `installer` frame); `item` is a catalog
 * item and is null for `restore_previous`; `mine` is true while an operation started here
 * has not ended.
 */
export function reasonFor(action, item, installer, mine = false, now = Date.now()) {
  if (!installer) return { key: "CORE_ADMIN_CATALOG_REASON_CHECKING", params: {} };
  if (!installer.available) return { key: "CORE_ADMIN_CATALOG_REASON_AWAY", params: {} };
  const until = installer.locked_until ? Date.parse(installer.locked_until) : 0;
  if (until > now) {
    return { key: "CORE_ADMIN_CATALOG_REASON_LOCKED", params: { time: fmtTime(installer.locked_until) } };
  }
  if (installer.busy || mine) return { key: "CORE_ADMIN_CATALOG_REASON_BUSY", params: {} };
  if (action === "restore_previous") return null;
  if (action === "install") {
    if (item.installed) return { key: "CORE_ADMIN_CATALOG_REASON_INSTALLED", params: {} };
    if (item.unlisted || !item.compatible) return { key: "CORE_ADMIN_CATALOG_REASON_INCOMPATIBLE", params: {} };
  } else if (!item.installed) {
    return { key: "CORE_ADMIN_CATALOG_REASON_NOT_INSTALLED", params: {} };
  } else if (action === "update" && !item.update_available) {
    return { key: "CORE_ADMIN_CATALOG_REASON_UP_TO_DATE", params: {} };
  }
  return null;
}

/**
 * The banner for an operation as `{kind, text}` (CORE-387): info while it runs, done, a
 * warning when it was rolled back, an error with the installer's reason when it failed.
 */
export function operationBanner(op) {
  const params = { action: actionName(op.action), name: op.name || "" };
  const text = (key, extra = {}) => t(key, { ...params, ...extra }).replace(/\s+([.:])/g, "$1").trim();
  if (op.state === "done") return { kind: "ok", text: text("CORE_ADMIN_CATALOG_OP_DONE") };
  if (op.state === "rolled_back") return { kind: "warn", text: text("CORE_ADMIN_CATALOG_OP_ROLLED_BACK") };
  if (op.state === "failed") {
    return { kind: "error", text: text("CORE_ADMIN_CATALOG_OP_FAILED", { error: op.error || "" }) };
  }
  return {
    kind: "info",
    text: text("CORE_ADMIN_CATALOG_OP_RUNNING", { state: t(`CORE_ADMIN_CATALOG_STATE_${upper(op.state)}`) }),
  };
}

/**
 * The text of a failed request for the dialog or a banner: the catalog text of the problem
 * code (CORE-298); a lock adds the time it ends (CORE-356).
 */
export function problemText(err) {
  if (!(err instanceof ApiError)) return String((err && err.message) || err);
  const p = err.problem || {};
  if (p.code === "CORE_API_INSTALLER_LOCKED" && p.locked_until) {
    return `${err.message} ${t("CORE_ADMIN_CATALOG_LOCKED_UNTIL", { time: fmtTime(p.locked_until) })}`;
  }
  return err.message;
}

/** The release of a detail by version, or the newest compatible one when none is named. */
export function pickRelease(detail, version = null) {
  const releases = (detail && detail.releases) || [];
  if (version) return releases.find((r) => r.version === version) || null;
  return releases.find((r) => r.compatible) || null;
}

const badge = (item) =>
  item.badge ? html`<span class="badge ${item.badge}">${t(`CORE_ADMIN_CATALOG_BADGE_${upper(item.badge)}`)}</span>` : nothing;

const none = () => html`<span class="muted">${t("CORE_ADMIN_CATALOG_NONE")}</span>`;

const list = (values) => (values && values.length ? values.map((v) => html`<span class="tag">${v}</span> `) : none());

const grants = (map) => {
  const entries = Object.entries(map || {});
  return entries.length
    ? entries.map(([key, scopes]) => html`<div><span class="mono">${key}</span>: ${list([].concat(scopes))}</div>`)
    : none();
};

/** The check report of a release: its result and a link the user follows (CORE-133). */
const checkReport = (check) =>
  check
    ? html`${check.result === "pass" ? t("CORE_ADMIN_CATALOG_CHECK_PASS") : t("CORE_ADMIN_CATALOG_CHECK_OTHER", { result: check.result })}
        ${check.report_url
          ? html` · <a href=${check.report_url} target="_blank" rel="noopener noreferrer">${t("CORE_ADMIN_CATALOG_OPEN_REPORT")}</a>`
          : nothing}`
    : none();

/** The element of the page. */
class AdminCatalog extends (globalThis.HTMLElement || class {}) {
  async connectedCallback() {
    await loadTexts();
    this.messages = new Messages(() => this.update());
    this.items = [];
    this.categories = [];
    this.catalog = null;
    this.installer = null; // {available, busy, locked_until}
    this.query = "";
    this.category = "";
    this.view = "all";
    this.selected = null;
    this.detail = null;
    this.dialog = null;
    this.op = null; // the operation shown: {id, action, name, state, steps, error}
    this.mine = null; // the id of an operation started here that has not ended
    this.error = null;
    this.timers = new Set();
    this.channel = new AdminChannel(
      (frame) => frame.kind === "installer" && this.onInstaller(frame.data),
      (on) => this.onChannel(on),
    );
    this.channel.subscribe("installer");
    await this.load();
  }

  disconnectedCallback() {
    if (this.channel) this.channel.close();
    for (const timer of this.timers) clearTimeout(timer);
  }

  later(fn, ms) {
    const timer = setTimeout(() => {
      this.timers.delete(timer);
      fn();
    }, ms);
    this.timers.add(timer);
    return timer;
  }

  /** Read the list with the search and the category the server filters by (CORE-356). */
  async load() {
    const params = new URLSearchParams();
    if (this.query.trim()) params.set("q", this.query.trim());
    if (this.category) params.set("category", this.category);
    const qs = params.toString();
    try {
      const data = await api("GET", `/catalog${qs ? `?${qs}` : ""}`);
      this.items = data.items;
      this.catalog = data.catalog;
      if (!this.installer) this.installer = { available: data.installer_available, busy: false, locked_until: null };
      if (!this.category && !this.query.trim()) {
        this.categories = [...new Set(data.items.map((i) => i.category).filter(Boolean))].sort();
      }
      this.error = null;
    } catch (err) {
      this.error = problemText(err);
      this.items = [];
    }
    this.update();
  }

  async refresh() {
    try {
      await api("POST", "/catalog/refresh");
      this.messages.set("ok", t("CORE_ADMIN_CATALOG_REFRESHED"));
    } catch (err) {
      this.messages.set("error", problemText(err));
    }
    await this.load();
  }

  search(value) {
    this.query = value;
    clearTimeout(this.searchTimer);
    this.searchTimer = this.later(() => this.load(), SEARCH_DELAY_MS);
  }

  async select(name) {
    this.selected = name;
    this.detail = null;
    this.update();
    try {
      const detail = await api("GET", `/catalog/${encodeURIComponent(name)}`);
      if (this.selected === name) this.detail = detail;
    } catch (err) {
      this.messages.set("error", problemText(err));
    }
    this.update();
  }

  // --- the installer topic (CORE-356) --------------------------------------------------

  onInstaller({ status, operation }) {
    if (status) this.installer = status;
    if (operation) this.follow(operation);
    this.update();
  }

  /** Show an operation of this page, or the newest one that is still running elsewhere. */
  follow(op) {
    const tracked = this.op && this.op.id === op.id;
    if (!tracked && (isFinal(op) || (this.op && !isFinal(this.op)))) return;
    const was = this.op && this.op.state;
    this.op = op;
    if (isFinal(op)) {
      if (was !== op.state) this.ended(op);
    } else if (was !== op.state) {
      this.messages.set("info", operationBanner(op).text);
    }
  }

  /** An operation ended: say how, and read the list again (the installed versions changed). */
  ended(op) {
    if (this.mine === op.id) this.mine = null;
    const b = operationBanner(op);
    const dismiss = { label: t("CORE_ADMIN_DISMISS"), onClick: () => this.messages.clear() };
    this.messages.set(b.kind, b.text, b.kind === "ok" ? null : dismiss);
    this.load();
  }

  /**
   * After the admin channel was lost, the engine may have restarted under an operation: read
   * the operation by id, which also gives the end state of one that finished meanwhile.
   */
  async onChannel(on) {
    if (!on || !this.op || isFinal(this.op)) return;
    try {
      this.follow(await api("GET", `/installer/operations/${encodeURIComponent(this.op.id)}`));
      this.update();
    } catch {
      // The engine is not back yet; the next connection tries again.
    }
  }

  // --- the dialog of an action (CORE-133, CORE-132) -------------------------------------

  /** Open the dialog of `action` for `item`: it loads what the user is shown before confirming. */
  async ask(action, item = null) {
    const dialog = { action, item, version: null, detail: null, instances: null, error: null, sending: false };
    this.dialog = dialog;
    this.update();
    if (item && !item.unlisted) {
      try {
        dialog.detail = this.detail && this.detail.name === item.name ? this.detail : await api("GET", `/catalog/${encodeURIComponent(item.name)}`);
      } catch (err) {
        dialog.error = problemText(err);
      }
      const release = pickRelease(dialog.detail);
      dialog.version = action === "remove" ? item.installed : release ? release.version : null;
    }
    if (action === "remove") {
      try {
        const modules = await api("GET", "/installed-modules");
        const mine = modules.filter((m) => m.distribution && normalize(m.distribution) === normalize(item.name));
        dialog.instances = mine.flatMap((m) => m.instances);
      } catch {
        dialog.instances = null;
      }
    }
    this.update();
  }

  closeDialog() {
    this.dialog = null;
    this.update();
  }

  /** Send the operation: the passphrase and the code go out once and are not kept (CORE-356). */
  async submit(form) {
    const d = this.dialog;
    if (!d || d.sending) return;
    const body = { action: d.action, passphrase: form.passphrase.value };
    const code = form.totp.value.trim();
    if (code) body.totp = code;
    if (d.action !== "restore_previous") body.name = d.item.name;
    if (d.action === "install") body.version = d.version;
    form.passphrase.value = "";
    form.totp.value = "";
    d.sending = true;
    d.error = null;
    this.update();
    try {
      const { id } = await api("POST", "/installer/operations", body);
      this.mine = id;
      this.op = { id, action: d.action, name: d.item ? d.item.name : null, state: "queued", steps: [], error: null };
      this.dialog = null;
      this.messages.set("info", operationBanner(this.op).text);
      this.onChannel(true);
    } catch (err) {
      d.sending = false;
      d.error = problemText(err);
    }
    this.update();
  }

  // --- views ------------------------------------------------------------------------

  visible() {
    return this.items.filter((i) => {
      if (this.view === "installed") return !!i.installed;
      if (this.view === "updates") return i.update_available;
      return true;
    });
  }

  status(item) {
    if (item.update_available) return t("CORE_ADMIN_CATALOG_STATUS_UPDATE", { version: item.latest });
    if (item.installed) return t("CORE_ADMIN_CATALOG_STATUS_INSTALLED", { version: item.installed });
    if (item.compatible === false) return t("CORE_ADMIN_CATALOG_STATUS_INCOMPATIBLE");
    return t("CORE_ADMIN_CATALOG_STATUS_AVAILABLE", { version: item.latest });
  }

  rows() {
    const items = this.visible();
    if (!items.length) return html`<p class="muted">${t("CORE_ADMIN_CATALOG_EMPTY")}</p>`;
    return html`<table class="catalog-list">
      <thead><tr>
        <th>${t("CORE_ADMIN_CATALOG_COL_MODULE")}</th><th>${t("CORE_ADMIN_CATALOG_COL_BADGE")}</th>
        <th>${t("CORE_ADMIN_CATALOG_COL_CATEGORY")}</th><th>${t("CORE_ADMIN_CATALOG_COL_DESCRIPTION")}</th>
        <th>${t("CORE_ADMIN_CATALOG_COL_STATUS")}</th>
      </tr></thead>
      <tbody>${items.map(
        (i) => html`<tr class="clickable ${this.selected === i.name ? "open" : ""}" data-name=${i.name}
          tabindex="0" @click=${() => this.select(i.name)} @keydown=${(ev) => ev.key === "Enter" && this.select(i.name)}>
          <td class="mono">${i.name}${i.unlisted ? html` <span class="badge unlisted">${t("CORE_ADMIN_CATALOG_UNLISTED")}</span>` : nothing}</td>
          <td>${badge(i)}</td><td>${i.category || nothing}</td><td>${i.description || nothing}</td>
          <td class=${i.update_available ? "update" : i.compatible === false ? "muted" : ""}>${this.status(i)}</td>
        </tr>`,
      )}</tbody></table>`;
  }

  releases(detail) {
    if (!detail.releases.length) return nothing;
    return html`<table class="releases">
      <thead><tr>
        <th>${t("CORE_ADMIN_CATALOG_COL_VERSION")}</th><th>${t("CORE_ADMIN_CATALOG_COL_API")}</th>
        <th>${t("CORE_ADMIN_CATALOG_COL_CHECK")}</th><th>${t("CORE_ADMIN_CATALOG_COL_CAPABILITIES")}</th>
        <th>${t("CORE_ADMIN_CATALOG_COL_GRANTS")}</th>
      </tr></thead>
      <tbody>${detail.releases.map(
        (r) => html`<tr class=${r.compatible ? "" : "muted"}>
          <td class="mono">${r.version}</td>
          <td class="mono">${r.api}${r.compatible ? nothing : html`<div>${t("CORE_ADMIN_CATALOG_INCOMPATIBLE_RELEASE")}</div>`}</td>
          <td>${checkReport(r.check)}</td><td>${list(r.capabilities)}</td><td>${grants(r.grants)}</td>
        </tr>`,
      )}</tbody></table>`;
  }

  /** The action rows of the chosen module, each disabled with its reason (CORE-386). */
  actions(item) {
    const mine = this.mine !== null;
    const row = (action, effect) => {
      const reason = reasonFor(action, item, this.installer, mine);
      return actionRow({
        name: actionName(action),
        effect: t(effect, { version: item.latest || "" }),
        label: actionName(action),
        reason: reason ? t(reason.key, reason.params) : null,
        id: action,
        cls: action === "remove" ? "" : "primary",
        onClick: () => this.ask(action, item),
        control: html`<code class="cli">${cliCommand(action, item.name, action === "install" ? item.latest : null)}</code>`,
      });
    };
    return html`${row("install", "CORE_ADMIN_CATALOG_INSTALL_EFFECT")}
      ${row("update", "CORE_ADMIN_CATALOG_UPDATE_EFFECT")}${row("remove", "CORE_ADMIN_CATALOG_REMOVE_EFFECT")}`;
  }

  detailCard() {
    if (!this.selected) return nothing;
    const d = this.detail;
    const item = d || this.items.find((i) => i.name === this.selected);
    if (!item) return nothing;
    return html`<section class="card detail-card" aria-labelledby="detail-title">
      <h2 id="detail-title">${item.name}</h2>
      <p>${item.description || nothing}</p>
      <dl class="facts">
        <dt>${t("CORE_ADMIN_CATALOG_COL_BADGE")}</dt><dd>${item.badge ? badge(item) : none()}</dd>
        <dt>${t("CORE_ADMIN_CATALOG_COL_CATEGORY")}</dt><dd>${item.category || none()}</dd>
        <dt>${t("CORE_ADMIN_CATALOG_LICENSE")}</dt><dd>${item.license || none()}</dd>
        <dt>${t("CORE_ADMIN_CATALOG_REPOSITORY")}</dt><dd>${item.repository
          ? html`<a href=${item.repository} target="_blank" rel="noopener noreferrer">${item.repository}</a>`
          : none()}</dd>
        ${d && d.publisher
          ? html`<dt>${t("CORE_ADMIN_CATALOG_PUBLISHER")}</dt><dd class="mono">${d.publisher.repository} · ${d.publisher.workflow}</dd>`
          : nothing}
        <dt>${t("CORE_ADMIN_CATALOG_COL_STATUS")}</dt><dd>${this.status(item)}</dd>
      </dl>
      ${item.compatible === false ? banner("warn", t("CORE_ADMIN_CATALOG_INCOMPATIBLE_NOTE")) : nothing}
      ${d ? this.releases(d) : html`<p class="muted">…</p>`}
      <h3>${t("CORE_ADMIN_CATALOG_ACTIONS")}</h3>
      ${this.actions(item)}
    </section>`;
  }

  operationCard() {
    const op = this.op;
    if (!op) return nothing;
    return html`<section class="card operation" aria-labelledby="operation-title">
      <h2 id="operation-title">${t("CORE_ADMIN_CATALOG_OPERATION")}</h2>
      <p>${operationBanner(op).text}</p>
      ${(op.steps || []).length
        ? html`<ol class="steps">${op.steps.map(
            (s) => html`<li><span class="mono">${fmtTime(s.at)}</span> ${t(`CORE_ADMIN_CATALOG_STATE_${upper(s.state)}`)}${s.detail ? html` — ${s.detail}` : nothing}</li>`,
          )}</ol>`
        : nothing}
    </section>`;
  }

  installerCard() {
    const reason = reasonFor("restore_previous", null, this.installer, this.mine !== null);
    return html`<section class="card installer" aria-labelledby="installer-title">
      <h2 id="installer-title">${t("CORE_ADMIN_CATALOG_INSTALLER")}</h2>
      ${actionRow({
        name: actionName("restore_previous"),
        effect: t("CORE_ADMIN_CATALOG_RESTORE_EFFECT"),
        label: actionName("restore_previous"),
        reason: reason ? t(reason.key, reason.params) : null,
        id: "restore_previous",
        onClick: () => this.ask("restore_previous"),
        control: html`<code class="cli">${cliCommand("restore_previous")}</code>`,
      })}
    </section>`;
  }

  /** The dialog of an action: what will be installed, the passphrase, Cancel and the action. */
  dialogView() {
    const d = this.dialog;
    if (!d) return html`<dialog class="catalog-dialog"></dialog>`;
    const { action, item } = d;
    const release = d.detail && d.version ? pickRelease(d.detail, d.version) : null;
    const title = item
      ? t(`CORE_ADMIN_CATALOG_DLG_${upper(action)}_TITLE`, { name: item.name, version: d.version || "" })
      : t("CORE_ADMIN_CATALOG_DLG_RESTORE_PREVIOUS_TITLE");
    const compatible = d.detail ? d.detail.releases.filter((r) => r.compatible) : [];
    return html`<dialog class="catalog-dialog" aria-labelledby="catalog-dialog-title" @close=${() => this.dialog && this.closeDialog()}>
      <form method="dialog" @submit=${(ev) => { ev.preventDefault(); this.submit(ev.target); }}>
        <h2 id="catalog-dialog-title">${title}</h2>
        <p>${t(`CORE_ADMIN_CATALOG_DLG_${upper(action)}_TEXT`)}</p>
        ${d.error ? banner("error", d.error) : nothing}
        ${item && !item.unlisted
          ? html`<dl class="facts">
              <dt>${t("CORE_ADMIN_CATALOG_COL_BADGE")}</dt><dd>${item.badge ? badge(item) : none()}</dd>
              ${action === "install" && compatible.length > 1
                ? html`<dt>${t("CORE_ADMIN_CATALOG_COL_VERSION")}</dt><dd><select name="version" @change=${(ev) => { d.version = ev.target.value; this.update(); }}>
                    ${compatible.map((r) => html`<option value=${r.version} ?selected=${r.version === d.version}>${r.version}</option>`)}
                  </select></dd>`
                : html`<dt>${t("CORE_ADMIN_CATALOG_COL_VERSION")}</dt><dd class="mono">${d.version || none()}</dd>`}
              ${release
                ? html`<dt>${t("CORE_ADMIN_CATALOG_COL_CHECK")}</dt><dd>${checkReport(release.check)}</dd>
                    <dt>${t("CORE_ADMIN_CATALOG_COL_CAPABILITIES")}</dt><dd>${list(release.capabilities)}</dd>
                    <dt>${t("CORE_ADMIN_CATALOG_COL_GRANTS")}</dt><dd>${grants(release.grants)}</dd>`
                : nothing}
            </dl>
            ${release ? html`<p class="muted">${t("CORE_ADMIN_CATALOG_GRANTS_NOTE")}</p>` : nothing}`
          : nothing}
        ${action === "remove" && d.instances
          ? html`<p class="instances">${d.instances.length
              ? t("CORE_ADMIN_CATALOG_REMOVE_INSTANCES", { ids: d.instances.join(", ") })
              : t("CORE_ADMIN_CATALOG_REMOVE_NO_INSTANCES")}</p>`
          : nothing}
        <label class="field">${t("CORE_ADMIN_CATALOG_PASSPHRASE")}
          <input type="password" name="passphrase" autocomplete="off" required autofocus></label>
        <label class="field">${t("CORE_ADMIN_CATALOG_TOTP")}
          <input type="text" name="totp" autocomplete="off" inputmode="numeric" pattern="[0-9 ]*"></label>
        <p class="muted">${t("CORE_ADMIN_CATALOG_CLI_ALTERNATIVE")}
          <code class="cli">${cliCommand(action, item ? item.name : null, action === "install" ? d.version : null)}</code></p>
        <div class="dialog-actions">
          <button type="button" class="cancel" @click=${() => this.closeDialog()}>${t("CORE_ADMIN_CANCEL")}</button>
          <button type="submit" class="primary confirm" aria-disabled=${d.sending ? "true" : "false"}>${actionName(action)}</button>
        </div>
      </form>
    </dialog>`;
  }

  controls() {
    const info = this.catalog;
    const updates = this.items.filter((i) => i.update_available).length;
    return html`<div class="toolbar">
      <label>${t("CORE_ADMIN_CATALOG_SEARCH")}
        <input type="search" name="q" .value=${this.query} @input=${(ev) => this.search(ev.target.value)}></label>
      <label>${t("CORE_ADMIN_CATALOG_COL_CATEGORY")}
        <select name="category" @change=${(ev) => { this.category = ev.target.value; this.load(); }}>
          <option value="">${t("CORE_ADMIN_CATALOG_ALL_CATEGORIES")}</option>
          ${this.categories.map((c) => html`<option value=${c} ?selected=${c === this.category}>${c}</option>`)}
        </select></label>
      <label>${t("CORE_ADMIN_CATALOG_VIEW")}
        <select name="view" @change=${(ev) => { this.view = ev.target.value; this.update(); }}>
          ${["all", "installed", "updates"].map(
            (v) => html`<option value=${v} ?selected=${v === this.view}>${t(`CORE_ADMIN_CATALOG_VIEW_${upper(v)}`)}</option>`,
          )}
        </select></label>
      <span class="spacer"></span>
      ${info ? html`<span class="muted index-info">${t("CORE_ADMIN_CATALOG_INDEX", { serial: info.serial, time: fmtTime(info.fetched_at) })}</span>` : nothing}
      <button type="button" data-action="refresh" @click=${() => this.refresh()}>${t("CORE_ADMIN_CATALOG_REFRESH")}</button>
      ${updates ? html`<span class="update-count">${t("CORE_ADMIN_CATALOG_UPDATES", { count: updates })}</span>` : nothing}
    </div>`;
  }

  update() {
    const info = this.catalog;
    const inst = this.installer;
    render(
      html`
        ${this.messages.view()}
        <h1>${t("CORE_ADMIN_CATALOG")}</h1>
        ${this.error ? banner("error", this.error, { label: t("CORE_ADMIN_CATALOG_REFRESH"), onClick: () => this.refresh() }) : nothing}
        ${info && info.stale ? banner("warn", t("CORE_ADMIN_CATALOG_STALE", { time: fmtTime(info.expires) }), { label: t("CORE_ADMIN_CATALOG_REFRESH"), onClick: () => this.refresh() }) : nothing}
        ${inst && !inst.available ? banner("warn", t("CORE_ADMIN_CATALOG_REASON_AWAY")) : nothing}
        ${inst && inst.available && inst.locked_until && Date.parse(inst.locked_until) > Date.now()
          ? banner("warn", t("CORE_ADMIN_CATALOG_REASON_LOCKED", { time: fmtTime(inst.locked_until) }))
          : nothing}
        ${this.controls()}
        <div class="split catalog">
          <div>${this.rows()}</div>
          <div>${this.detailCard()}${this.operationCard()}${this.installerCard()}</div>
        </div>
        ${this.dialogView()}
      `,
      this,
    );
    this.syncDialog();
  }

  /** Open or close the dialog element to match the state, after the page has rendered. */
  syncDialog() {
    const dialog = this.querySelector("dialog.catalog-dialog");
    if (!dialog) return;
    if (!dialog.dataset.backdrop) {
      dialog.dataset.backdrop = "1";
      closeOnBackdrop(dialog, () => this.closeDialog());
    }
    if (this.dialog && !dialog.open) dialog.showModal();
    if (!this.dialog && dialog.open) dialog.close();
  }
}

if (typeof customElements !== "undefined") customElements.define("mm3-admin-catalog", AdminCatalog);
