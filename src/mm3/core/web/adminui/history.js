// The History tab of the admin config page /admin/config (CORE-322).
// Implements CORE-322 (Core spec 0.25, §6.4 Web configuration editor): versions with
// their changed sections, diff between two versions, Restore all and Restore
// section after a confirmation that shows the diff.
// Implements CORE-321 (Core spec 0.25, §6.8 Configuration history, undo and redo):
// paged history, undo and redo of saves, restore with If-Match.
// Implements CORE-300 (Core spec 0.22, §6.4 Web configuration editor): writes carry
// the ETag; a conflict reloads it.
// Implements CORE-324 (Core spec 0.26, §6.8 Configuration history, undo and redo):
// Open in editor; the page loads the version as the draft (event "mm3-open").
import { html, nothing, render } from "/admin/static/lit-html.js";
import { ApiError, fmtTime, t } from "/admin/static/admin.js";

const PAGE = 50;

async function call(method, path, { body, etag } = {}) {
  const init = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (etag) init.headers["If-Match"] = etag;
  const r = await fetch(`/api/v1${path}`, init);
  const type = r.headers.get("content-type") || "";
  const data = type.includes("json") ? await r.json() : null;
  if (!r.ok) {
    if (r.status === 401) window.location.assign("/admin/login");
    throw new ApiError(data || { detail: r.statusText });
  }
  return data;
}

class ConfigHistory extends HTMLElement {
  constructor() {
    super();
    this.tab = "history";
    this.items = [];
    this.next = null;
    this.etag = null;
    this.a = null;
    this.b = null;
    this.diff = null;
    this.sectionFilter = "";
    this.confirm = null;
    this.message = "";
    this.records = [];
    this.busy = false;
  }

  async connectedCallback() {
    await this.reload();
  }

  // --- data -------------------------------------------------------------------------

  async reload() {
    try {
      const [config, page] = await Promise.all([
        call("GET", "/config"),
        call("GET", `/config/history?limit=${PAGE}`),
      ]);
      this.etag = config.etag;
      this.items = page.items;
      this.next = page.next;
      const current = this.items.find((i) => i.current);
      if (current && this.b === null) this.b = current.version;
    } catch (err) {
      this.message = err.message;
    }
    this.update();
  }

  async more() {
    if (!this.next) return;
    try {
      const page = await call("GET", `/config/history?limit=${PAGE}&before=${this.next}`);
      this.items = [...this.items, ...page.items];
      this.next = page.next;
    } catch (err) {
      this.message = err.message;
    }
    this.update();
  }

  async compare() {
    if (this.a === null || this.b === null) {
      this.message = t("CORE_ADMIN_SELECT_TWO");
      this.update();
      return;
    }
    try {
      this.diff = { a: this.a, b: this.b, sections: await call("GET", `/config/history/${this.a}/diff/${this.b}`) };
      this.sectionFilter = "";
      this.message = "";
    } catch (err) {
      this.message = err.message;
    }
    this.update();
  }

  currentVersion() {
    const current = this.items.find((i) => i.current);
    return current ? current.version : null;
  }

  async askRestore(version, section = null) {
    const current = this.currentVersion();
    try {
      const path = `/config/history/${current}/diff/${version}${section ? `?section=${encodeURIComponent(section)}` : ""}`;
      const sections = current !== null ? await call("GET", path) : [];
      this.confirm = { kind: "restore", version, section, sections };
    } catch (err) {
      this.message = err.message;
    }
    this.update();
    this.querySelector("dialog")?.showModal();
  }

  async act(kind, extra = {}) {
    this.busy = true;
    this.records = [];
    this.update();
    try {
      const body = kind === "restore" ? { version: extra.version, section: extra.section || null } : undefined;
      const result = await call("POST", `/config/${kind}`, { body, etag: this.etag });
      this.message = t("CORE_ADMIN_DONE_VERSION", { version: result.version });
      this.etag = result.etag;
      this.b = null;
      this.diff = null;
    } catch (err) {
      this.message = err.message;
      const problem = err.problem || {};
      if (problem.errors) this.records = problem.errors;
      if (problem.etag) this.etag = problem.etag;
    }
    this.busy = false;
    this.confirm = null;
    await this.reload();
    this.dispatchEvent(new CustomEvent("mm3-saved", { bubbles: true }));
  }

  open(version) {
    this.dispatchEvent(new CustomEvent("mm3-open", { detail: { version }, bubbles: true }));
  }

  close() {
    this.querySelector("dialog")?.close();
    this.confirm = null;
    this.update();
  }

  // --- rendering --------------------------------------------------------------------

  update() {
    render(this.view(), this);
  }

  view() {
    return html`
      <div class="toolbar">
        <button ?disabled=${this.busy} @click=${() => this.act("undo")}>${t("CORE_ADMIN_UNDO_SAVE")}</button>
        <button ?disabled=${this.busy} @click=${() => this.act("redo")}>${t("CORE_ADMIN_REDO_SAVE")}</button>
      </div>
      ${this.message ? html`<p class="alert warn" role="status">${this.message}</p>` : nothing}
      ${this.records.length
        ? html`<section class="card"><table><tbody>${this.records.map(
            (r) => html`<tr><td class="level-${r.severity}">${r.severity}</td><td class="mono">${r.path}</td>
              <td>${r.message}</td></tr>`,
          )}</tbody></table></section>`
        : nothing}
      ${this.historyView()} ${this.diffView()} ${this.dialog()}`;
  }

  historyView() {
    const radio = (name, version) => html`<input type="radio" name=${name}
      .checked=${this[name] === version} @change=${() => {
        this[name] = version;
        this.update();
      }}>`;
    return html`<section class="card">
      <div class="toolbar">
        <button class="primary" @click=${() => this.compare()}>${t("CORE_ADMIN_COMPARE")}</button>
      </div>
      <table>
        <thead><tr>
          <th>${t("CORE_ADMIN_VERSION")}</th><th>${t("CORE_ADMIN_FROM")}</th><th>${t("CORE_ADMIN_TO")}</th><th>${t("CORE_ADMIN_TIME")}</th>
          <th>${t("CORE_ADMIN_ORIGIN")}</th><th>${t("CORE_ADMIN_AUTHOR")}</th>
          <th>${t("CORE_ADMIN_SECTIONS")}</th><th></th>
        </tr></thead>
        <tbody>${this.items.map(
          (i) => html`<tr class=${i.current ? "current" : ""}>
            <td>${i.version}</td><td>${radio("a", i.version)}</td><td>${radio("b", i.version)}</td>
            <td class="nowrap">${fmtTime(i.time)}</td>
            <td>${t(`CORE_ADMIN_ORIGIN_${String(i.origin).toUpperCase()}`)}</td>
            <td class="mono">${i.author}</td>
            <td class="mono">${i.sections.length ? i.sections.join(", ") : html`<span class="muted">–</span>`}</td>
            <td class="nowrap">${i.current
              ? html`<span class="muted">${t("CORE_ADMIN_CURRENT")}</span>`
              : html`<button ?disabled=${this.busy} @click=${() => this.askRestore(i.version)}>${t("CORE_ADMIN_RESTORE_ALL")}</button>
                  <button ?disabled=${this.busy} @click=${() => this.open(i.version)}>${t("CORE_ADMIN_OPEN_IN_EDITOR")}</button>`}</td>
          </tr>`,
        )}</tbody>
      </table>
      ${this.next ? html`<button @click=${() => this.more()}>${t("CORE_ADMIN_LOAD_MORE")}</button>` : nothing}
    </section>`;
  }

  sectionsView(sections, restoreFrom = null, labels = null) {
    if (!sections.length) return html`<p class="muted">${t("CORE_ADMIN_NO_CHANGES")}</p>`;
    return html`<div class="diff">
      ${labels ? html`<div class="muted">${labels[0]}</div><div class="muted">${labels[1]}</div>` : nothing}
      ${sections.map(
      (d) => html`<h3>${d.section}
          ${restoreFrom !== null
            ? html` <button ?disabled=${this.busy} @click=${() => this.askRestore(restoreFrom, d.section)}>
                ${t("CORE_ADMIN_RESTORE_SECTION")}</button>`
            : nothing}</h3>
        <pre>${d.before}</pre><pre>${d.after}</pre>`,
    )}</div>`;
  }

  diffView() {
    const d = this.diff;
    if (!d) return nothing;
    const names = d.sections.map((s) => s.section);
    const shown = this.sectionFilter ? d.sections.filter((s) => s.section === this.sectionFilter) : d.sections;
    const current = this.currentVersion();
    return html`<section class="card">
      <h2>${t("CORE_ADMIN_DIFF_TITLE", { a: d.a, b: d.b })}</h2>
      <div class="toolbar">
        <label>${t("CORE_ADMIN_SECTIONS")}
          <select @change=${(ev) => {
            this.sectionFilter = ev.target.value;
            this.update();
          }}>
            <option value="">${t("CORE_ADMIN_ALL_SECTIONS")}</option>
            ${names.map((n) => html`<option value=${n} ?selected=${n === this.sectionFilter}>${n}</option>`)}
          </select>
        </label>
      </div>
      ${this.sectionsView(shown, d.a !== current ? d.a : null, [
        `${t("CORE_ADMIN_VERSION")} ${d.a}`,
        `${t("CORE_ADMIN_VERSION")} ${d.b}`,
      ])}
    </section>`;
  }

  dialog() {
    const c = this.confirm;
    if (!c) return html`<dialog></dialog>`;
    const question = c.section
      ? t("CORE_ADMIN_CONFIRM_RESTORE_SECTION", { section: c.section, version: c.version })
      : t("CORE_ADMIN_CONFIRM_RESTORE", { version: c.version });
    return html`<dialog @close=${() => this.close()}>
      <h2>${question}</h2>
      ${this.sectionsView(c.sections, null, [t("CORE_ADMIN_NOW"), t("CORE_ADMIN_AFTER_RESTORE")])}
      <div class="toolbar">
        <button @click=${() => this.close()}>${t("CORE_ADMIN_CANCEL")}</button>
        <button class="primary" ?disabled=${this.busy} @click=${() => {
          this.querySelector("dialog")?.close();
          this.act("restore", { version: c.version, section: c.section });
        }}>${t("CORE_ADMIN_CONFIRM")}</button>
      </div>
    </dialog>`;
  }
}

customElements.define("mm3-config-history", ConfigHistory);
