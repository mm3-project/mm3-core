// The Layout tab of /admin/config: the real mirror in a scaled preview iframe, with an
// editing overlay on top. Edits are draft steps like the Form and YAML views.
// Implements CORE-97 (Core spec 0.1, §6.6 Live layout preview): the preview shell,
// scaled to the admin window at the engine's viewport, with the overlay above it.
// Implements CORE-242 (Core spec 0.6, §6.9 Pages and visibility): the page selector.
// Implements CORE-325 (Core spec 0.28, §6.6 Live layout preview): the draft layout and
// the selected page go to the iframe with postMessage.
// Implements CORE-326 (Core spec 0.28, §6.6 Live layout preview): in-house overlay, drag
// and corner resize snapped to cells, red and refused when invalid, presets and stacks.
// Implements CORE-327 (Core spec 0.28, §6.6 Live layout preview): which key an edit writes.
// Implements CORE-328 (Core spec 0.28, §6.6 Live layout preview): the toolbar, Enable or
// Disable, Edit in Form, and Rescale proportionally (CORE-94).
// Implements CORE-94 (Core spec 0.1, §6.5 Screen layout): Rescale proportionally.
// Implements CORE-142 (Core spec 0.1, §6.7 Display output): sizes in pixels.
// Implements CORE-307 (Core spec 0.28, §6.6 Live layout preview): the Show on mirror toggle.
// Implements CORE-240 (Core spec 0.6, §6.9 Pages and visibility): Disable writes
// `disabled` through the normal save ("Save as disabled").
// Implements CORE-375 (Core spec 0.39, §6.6 Live layout preview): Delete on the overlay
// toolbar and on the disabled list, one undoable step with no confirmation.
// Implements CORE-376 (Core spec 0.39, §6.6 Live layout preview): New, Rename and Delete
// page beside the page selector, each one draft step.
// Implements CORE-374 (Core spec 0.40, §6.6 Live layout preview): Add module from the
// installed modules (CORE-380), on a preset, one draft step with the id always written.
// Implements CORE-375 (Core spec 0.40, §6.6 Live layout preview): Delete on the cards of
// the Services lane.

import { html, nothing, render } from "/admin/static/lit-html.js";
import { AdminChannel, api, sessionLocale, t } from "/admin/static/admin.js";
import { clone, diffOps, equal, getAt, instanceId, isTag, pointer, sectionLocation, sectionValues, setOp } from "/admin/static/draft.js";

export const PRESETS = [
  "top_bar",
  "top_left",
  "top_center",
  "top_right",
  "upper_third",
  "middle_center",
  "lower_third",
  "bottom_left",
  "bottom_center",
  "bottom_right",
  "bottom_bar",
  "fullscreen_below",
  "fullscreen_above",
];
const MIRROR_TTL_MS = 60000; // CORE-307: the engine reverts 60 s after the last push
const APPEARANCE = { scale: 1, align: "start", valign: "start", opacity: 1, overflow: "clip", header: false };

const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v) && !isTag(v);

/** Half up, as the presets round (CORE-92). */
const roundHalfUp = (x) => Math.floor(x + 0.5);

/** The rectangle of a placement, as a `position` value (z only when not 0). */
export function rectValue(r) {
  const v = { col: r.col, row: r.row, width: r.width, height: r.height };
  if (r.z) v.z = r.z;
  return v;
}

/** Whether `rect` fits the grid and overlaps nothing on its z (CORE-93, CORE-326). */
export function placementValid(rect, others, grid) {
  if (rect.col < 1 || rect.row < 1 || rect.width < 1 || rect.height < 1) return false;
  if (rect.col + rect.width - 1 > grid.columns || rect.row + rect.height - 1 > grid.rows) return false;
  for (const o of others) {
    if ((o.z || 0) !== (rect.z || 0)) continue;
    const apart =
      o.col + o.width <= rect.col ||
      rect.col + rect.width <= o.col ||
      o.row + o.height <= rect.row ||
      rect.row + rect.height <= o.row;
    if (!apart) return false;
  }
  return true;
}

/** The other rectangles of a page; a preset stack counts once (CORE-93). */
export function otherRects(placements, instance) {
  const out = [];
  const stacks = new Set();
  for (const p of placements) {
    if (p.instance === instance) continue;
    if (p.preset) {
      if (stacks.has(p.preset)) continue;
      stacks.add(p.preset);
    }
    out.push(p);
  }
  return out;
}

/**
 * The instance after writing `value` as its position on `page` (CORE-327): the
 * page's override when it has one; else `position` for `pages: all` or one page;
 * else a new override. Overrides equal to `position` are removed.
 */
export function withPosition(item, page, value) {
  const next = clone(item);
  const overrides = isObject(next.positions) ? next.positions : null;
  const pages = next.pages;
  const shared = pages === undefined || pages === "all" || (Array.isArray(pages) && pages.length === 1);
  if (overrides && page in overrides) overrides[page] = value;
  else if (shared) next.position = value;
  else next.positions = { ...(overrides || {}), [page]: value };
  if (isObject(next.positions)) {
    for (const [k, v] of Object.entries(next.positions)) if (equal(v, next.position)) delete next.positions[k];
    if (!Object.keys(next.positions).length) delete next.positions;
  }
  return next;
}

/** A 0-based cell boundary on a new grid size (CORE-94, CORE-328). */
function scaleEdge(b, from, to) {
  return roundHalfUp((b * to) / from);
}

/** A rectangle on a new grid size; each side at least one cell. */
export function rescaleRect(r, from, to) {
  let c0 = scaleEdge(r.col - 1, from.columns, to.columns);
  let c1 = Math.max(scaleEdge(r.col - 1 + r.width, from.columns, to.columns), c0 + 1);
  let r0 = scaleEdge(r.row - 1, from.rows, to.rows);
  let r1 = Math.max(scaleEdge(r.row - 1 + r.height, from.rows, to.rows), r0 + 1);
  if (c1 > to.columns) [c0, c1] = [to.columns - (c1 - c0), to.columns];
  if (r1 > to.rows) [r0, r1] = [to.rows - (r1 - r0), to.rows];
  const out = { ...r, col: Math.max(c0, 0) + 1, width: c1 - Math.max(c0, 0), row: Math.max(r0, 0) + 1, height: r1 - Math.max(r0, 0) };
  return out;
}

/** The instance with every rectangle position rescaled (presets stay). */
export function rescaledItem(item, from, to) {
  const next = clone(item);
  const fix = (v) => (isObject(v) && typeof v.col === "number" ? rescaleRect(v, from, to) : v);
  if (next.position !== undefined) next.position = fix(next.position);
  if (isObject(next.positions)) for (const k of Object.keys(next.positions)) next.positions[k] = fix(next.positions[k]);
  return next;
}

// --- pages (CORE-230, CORE-376) ------------------------------------------------------

const PAGE_ID = /^[a-z][a-z0-9_]{0,31}$/;
export const MAX_PAGES = 32;

/** The page ids of the draft's `pages` block; the implicit `main` without a list (CORE-230). */
export function pageIds(pages) {
  if (!isObject(pages) || !Array.isArray(pages.list)) return ["main"];
  return pages.list.map((e) => (isObject(e) ? e.id : e)).filter((id) => typeof id === "string");
}

/** The catalog key of what is wrong with a new page id, or null (checked as typed, CORE-376). */
export function pageIdProblem(id, ids) {
  if (!PAGE_ID.test(id)) return "CORE_ADMIN_PAGE_ID_INVALID";
  if (ids.includes(id)) return "CORE_ADMIN_PAGE_ID_TAKEN";
  return null;
}

/** A page title in `lang`: the text of a map, or the string that is the same in every language. */
export function titleIn(title, lang) {
  if (typeof title === "string") return title;
  return isObject(title) && typeof title[lang] === "string" ? title[lang] : "";
}

/**
 * A `LocalizedText` with `text` in `lang` only (CORE-376): other languages are kept; a plain
 * string, the same in every language, stays the English text.
 */
export function withTitle(title, lang, text) {
  if (isObject(title)) return { ...title, [lang]: text };
  if (typeof title === "string" && lang !== "en") return { en: title, [lang]: text };
  return { [lang]: text };
}

/** The id of a new instance: the module name, else `<module>_2`, `<module>_3`… (CORE-374). */
export function newInstanceId(module, ids) {
  if (!ids.includes(module)) return module;
  for (let n = 2; ; n += 1) if (!ids.includes(`${module}_${n}`)) return `${module}_${n}`;
}

/**
 * The instance after a page was deleted (CORE-376): the page leaves its `pages` list and its
 * `positions`; an instance left with no page is disabled.
 */
export function withoutPage(item, page) {
  const next = clone(item);
  if (Array.isArray(next.pages) && next.pages.includes(page)) {
    next.pages = next.pages.filter((p) => p !== page);
    if (!next.pages.length) next.disabled = true;
  }
  if (isObject(next.positions) && page in next.positions) {
    delete next.positions[page];
    if (!Object.keys(next.positions).length) delete next.positions;
  }
  return next;
}

class LayoutView extends HTMLElement {
  constructor() {
    super();
    this.host = null; // the AdminConfig page: draft, run(), openForm(), problem()
    this.page = null;
    this.selected = null;
    this.scale = 1;
    this.drag = null;
    this.mirrorOn = false;
    this.mirrorTimer = null;
    this.pushTimer = null;
    this.channel = null;
    this.sentLayout = null;
    this.dlg = null; // the New and Rename page dialog: {mode, id, title}
    this.installed = null; // GET /installed-modules (CORE-380), read when the tab opens
    this.addDlg = null; // the Add module dialog: {module, preset}
    this.resize = new ResizeObserver(() => this.fit());
  }

  async connectedCallback() {
    // The session keeps an /admin/ws open, so Show on mirror ends when the page closes.
    this.channel = new AdminChannel(() => {}, () => {});
    this.update();
    this.loadInstalled();
    try {
      const pages = await api("GET", "/pages");
      if (this.page === null && pages && pages.current) this.setPage(pages.current);
    } catch {
      // the page list is optional: the first page of the draft is used
    }
  }

  disconnectedCallback() {
    this.resize.disconnect();
    if (this.mirrorOn) this.setMirror(false);
    this.channel?.close();
    this.channel = null;
  }

  get draft() {
    return this.host.draft;
  }

  /** The installed modules (CORE-380); the Services lane needs their kind. */
  async loadInstalled() {
    try {
      this.installed = await api("GET", "/installed-modules");
    } catch (err) {
      this.installed = this.installed || [];
      this.host?.problem(err);
    }
    this.update();
  }

  /** `frontend` or `backend` for an installed module, null when it is not known (CORE-380). */
  kindOf(module) {
    const info = (this.installed || []).find((m) => m.name === module);
    return info ? info.kind : null;
  }

  get layout() {
    return this.draft.layout;
  }

  pages() {
    return this.layout ? Object.keys(this.layout.pages) : [];
  }

  placements() {
    return (this.layout && this.layout.pages[this.page]) || [];
  }

  item(id) {
    return sectionValues(this.draft.documents, this.draft.main)[`modules/${id}`];
  }

  // --- the iframe (CORE-325) ----------------------------------------------------------

  post(message) {
    const frame = this.querySelector("iframe");
    if (frame && frame.contentWindow) frame.contentWindow.postMessage(message, location.origin);
  }

  sendAll() {
    if (this.layout) this.post({ kind: "control", action: "layout", layout: this.layout });
    if (this.page !== null) this.post({ kind: "control", action: "page", page: this.page });
    this.sentLayout = this.layout;
  }

  setPage(page) {
    this.page = page;
    this.selected = null;
    this.post({ kind: "control", action: "page", page });
    this.update();
  }

  // --- editing ------------------------------------------------------------------------

  /** One draft step from a new instance value (CORE-301, CORE-326). */
  async commit(id, next, label) {
    const loc = sectionLocation(this.draft.documents, this.draft.main, `modules/${id}`);
    if (!loc || !loc.parts) return;
    const ops = diffOps(loc.parts, getAt(this.draft.documents[loc.file], loc.parts), next);
    if (!ops.length) {
      this.update();
      return;
    }
    await this.host.run(() => this.draft.apply(loc.file, ops, label));
  }

  placeAt(id, rect, label) {
    const item = this.item(id);
    if (!item) return undefined;
    return this.commit(id, withPosition(item, this.page, rectValue(rect)), label);
  }

  setPreset(id, preset) {
    const item = this.item(id);
    const p = this.placements().find((q) => q.instance === id);
    if (!item || !p) return undefined;
    const value = preset ? preset : rectValue(p);
    return this.commit(id, withPosition(item, this.page, value), t("CORE_ADMIN_STEP_CHANGED", { id, field: "position" }));
  }

  setZ(id, z) {
    const p = this.placements().find((q) => q.instance === id);
    if (!p || p.preset || !Number.isInteger(z)) return undefined;
    return this.placeAt(id, { ...p, z }, t("CORE_ADMIN_STEP_CHANGED", { id, field: "z" }));
  }

  setAppearance(id, key, value) {
    const item = this.item(id);
    if (!item) return undefined;
    const next = clone(item);
    const app = isObject(next.appearance) ? next.appearance : {};
    if (equal(value, APPEARANCE[key])) delete app[key];
    else app[key] = value;
    if (Object.keys(app).length) next.appearance = app;
    else delete next.appearance;
    return this.commit(id, next, t("CORE_ADMIN_STEP_CHANGED", { id, field: `appearance.${key}` }));
  }

  setEnabled(id, enabled) {
    const item = this.item(id);
    if (!item) return undefined;
    const next = clone(item);
    if (enabled) delete next.disabled;
    else next.disabled = true;
    if (!enabled && this.selected === id) this.selected = null;
    return this.commit(id, next, t(enabled ? "CORE_ADMIN_STEP_ENABLED" : "CORE_ADMIN_STEP_DISABLED", { id }));
  }

  /** Delete an instance: one undoable step removing its whole entry, no confirmation (CORE-375). */
  deleteInstance(id) {
    const loc = sectionLocation(this.draft.documents, this.draft.main, `modules/${id}`);
    if (!loc || !loc.parts) return undefined;
    if (this.selected === id) this.selected = null;
    const ops = [{ op: "remove", path: pointer(loc.parts) }];
    return this.host.run(() => this.draft.apply(loc.file, ops, t("CORE_ADMIN_STEP_DELETED", { id })));
  }

  // --- Add module (CORE-374) ----------------------------------------------------------

  async openAddDialog() {
    await this.loadInstalled();
    const usable = (this.installed || []).filter((m) => m.usable);
    this.addDlg = { module: usable[0] ? usable[0].name : "", preset: "top_left" };
    this.update();
    this.querySelector("dialog.module")?.showModal();
  }

  closeAddDialog() {
    this.querySelector("dialog.module")?.close();
  }

  /**
   * Add module: one draft step appending the instance to `modules` with its id written, a
   * front-end one on the chosen preset, `config: {}`, and `pages: [<selected page>]` when a
   * `pages` block exists. A sibling of the same module without an `id` gets its default
   * id in the same step.
   */
  async addModule(name, preset) {
    const info = (this.installed || []).find((m) => m.name === name && m.usable);
    const where = sectionLocation(this.draft.documents, this.draft.main, "modules/_order");
    if (!info || !where) return;
    const doc = this.draft.documents[where.file];
    const list = getAt(doc, where.parts);
    const items = Array.isArray(list) ? list : [];
    const id = newInstanceId(name, items.map(instanceId).filter(Boolean));
    const ops = [];
    items.forEach((item, i) => {
      if (isObject(item) && item.module === name && typeof item.id !== "string") {
        ops.push({ op: "add", path: pointer([...where.parts, i, "id"]), value: name });
      }
    });
    const value = { module: name, id };
    if (info.kind === "frontend") value.position = preset;
    const block = this.pagesBlock();
    if (block && isObject(block.value) && this.page !== null) value.pages = [this.page];
    value.config = {};
    const op = setOp(doc, [...where.parts, "-"], value);
    if (!op) return;
    ops.push(op);
    this.selected = info.kind === "frontend" ? id : null;
    await this.host.run(() => this.draft.apply(where.file, ops, t("CORE_ADMIN_STEP_ADDED", { id })));
  }

  async submitAddDialog() {
    const d = this.addDlg;
    if (!d || !d.module) return;
    this.closeAddDialog();
    await this.addModule(d.module, d.preset);
  }

  addDialog() {
    const d = this.addDlg;
    if (!d) return html`<dialog class="module"></dialog>`;
    const all = this.installed || [];
    const usable = all.filter((m) => m.usable);
    const refused = all.filter((m) => !m.usable);
    const info = usable.find((m) => m.name === d.module);
    return html`<dialog class="module" @close=${() => {
      this.addDlg = null;
      this.update();
    }}>
      <form @submit=${(e) => {
        e.preventDefault();
        this.submitAddDialog();
      }}>
        <h3>${t("CORE_ADMIN_ADD_MODULE")}</h3>
        ${usable.length
          ? html`<label>${t("CORE_ADMIN_MODULE")}
              <select name="module" @change=${(e) => {
                d.module = e.target.value;
                this.update();
              }}>
                ${usable.map(
                  (m) => html`<option value=${m.name} ?selected=${m.name === d.module}>${m.header} (${m.name}) · ${t(m.kind === "frontend" ? "CORE_ADMIN_KIND_FRONTEND" : "CORE_ADMIN_KIND_BACKEND")}</option>`,
                )}
              </select></label>`
          : html`<p class="alert no-modules">${t("CORE_ADMIN_NO_USABLE_MODULES")}</p>`}
        ${info && info.kind === "frontend"
          ? html`<label>${t("CORE_ADMIN_PRESET")}
              <select name="preset" @change=${(e) => {
                d.preset = e.target.value;
              }}>
                ${PRESETS.map((n) => html`<option value=${n} ?selected=${n === d.preset}>${n}</option>`)}
              </select></label>`
          : nothing}
        ${refused.length
          ? html`<div class="refused muted"><p>${t("CORE_ADMIN_REFUSED_MODULES")}</p>
              <ul>${refused.map((m) => html`<li><span class="mono">${m.name}</span>: ${m.error}</li>`)}</ul></div>`
          : nothing}
        <div class="row">
          <span class="spacer"></span>
          <button type="button" name="cancel" @click=${() => this.closeAddDialog()}>${t("CORE_ADMIN_CANCEL")}</button>
          <button type="submit" class="primary" name="ok" ?disabled=${!info}>${t("CORE_ADMIN_ADD")}</button>
        </div>
      </form>
    </dialog>`;
  }

  // --- pages (CORE-376) ---------------------------------------------------------------

  pagesBlock() {
    const loc = sectionLocation(this.draft.documents, this.draft.main, "pages");
    if (!loc) return null;
    const doc = this.draft.documents[loc.file];
    return { loc, doc, value: getAt(doc, loc.parts) };
  }

  pageIdList() {
    const block = this.pagesBlock();
    return pageIds(block ? block.value : undefined);
  }

  openPageDialog(mode) {
    const block = this.pagesBlock();
    let title = "";
    if (mode === "rename" && block && isObject(block.value) && Array.isArray(block.value.list)) {
      const entry = block.value.list.find((e) => isObject(e) && e.id === this.page);
      title = entry ? titleIn(entry.title, sessionLocale()) : "";
    }
    this.dlg = { mode, id: "", title };
    this.update();
    this.querySelector("dialog.page")?.showModal();
  }

  closePageDialog() {
    this.querySelector("dialog.page")?.close();
  }

  /** New page: appends to `pages.list`, creating the block with `main` when there is none. */
  async addPage(id, title) {
    const block = this.pagesBlock();
    if (!block) return;
    const { loc, doc, value } = block;
    const entry = { id, title: { [sessionLocale()]: title } };
    let op;
    if (!isObject(value)) op = setOp(doc, loc.parts, { list: [{ id: "main" }, entry], default: "main" });
    else if (!Array.isArray(value.list)) op = setOp(doc, [...loc.parts, "list"], [{ id: "main" }, entry]);
    else op = setOp(doc, [...loc.parts, "list", "-"], entry);
    if (!op) return;
    await this.host.run(() => this.draft.apply(loc.file, [op], t("CORE_ADMIN_STEP_PAGE_ADDED", { page: id })));
    this.setPage(id);
  }

  /** Rename page: the title in the session language only, never the id. */
  async renamePage(id, title) {
    const block = this.pagesBlock();
    if (!block) return;
    const { loc, doc, value } = block;
    const lang = sessionLocale();
    const index = isObject(value) && Array.isArray(value.list) ? value.list.findIndex((e) => isObject(e) && e.id === id) : -1;
    let op;
    if (index < 0) op = setOp(doc, loc.parts, { list: [{ id, title: withTitle(undefined, lang, title) }] });
    else op = setOp(doc, [...loc.parts, "list", index, "title"], withTitle(value.list[index].title, lang, title));
    if (!op) return;
    await this.host.run(() => this.draft.apply(loc.file, [op], t("CORE_ADMIN_STEP_PAGE_RENAMED", { page: id })));
  }

  /**
   * Delete page: not the last one. The page leaves `pages.list`, every instance's `pages` and
   * `positions`; an instance left with no page is disabled; `default` moves to the first
   * remaining page when it was the deleted one. One draft step; the selector then shows
   * the default page.
   */
  async deletePage(id) {
    const block = this.pagesBlock();
    const ids = this.pageIdList();
    if (!block || ids.length < 2 || !ids.includes(id) || !isObject(block.value) || !Array.isArray(block.value.list)) return;
    const { loc, value } = block;
    const remaining = ids.filter((p) => p !== id);
    const index = value.list.findIndex((e) => (isObject(e) ? e.id : e) === id);
    const ops = new Map();
    const add = (file, list) => ops.set(file, [...(ops.get(file) || []), ...list]);
    const pageOps = [{ op: "remove", path: pointer([...loc.parts, "list", index]) }];
    let fallback = typeof value.default === "string" && remaining.includes(value.default) ? value.default : remaining[0];
    if (value.default === id) {
      pageOps.push({ op: "replace", path: pointer([...loc.parts, "default"]), value: remaining[0] });
      fallback = remaining[0];
    }
    add(loc.file, pageOps);
    const where = sectionLocation(this.draft.documents, this.draft.main, "modules/_order");
    const list = where ? getAt(this.draft.documents[where.file], where.parts) : null;
    if (Array.isArray(list)) {
      list.forEach((item, i) => {
        if (isObject(item)) add(where.file, diffOps([...where.parts, i], item, withoutPage(item, id)));
      });
    }
    const steps = [...ops].map(([file, patch]) => ({ file, patch }));
    await this.host.run(() => this.draft.applyOps(steps, t("CORE_ADMIN_STEP_PAGE_DELETED", { page: id })));
    this.setPage(fallback);
  }

  async submitPageDialog() {
    const d = this.dlg;
    if (!d) return;
    const title = d.title.trim();
    this.closePageDialog();
    if (d.mode === "new") await this.addPage(d.id, title);
    else await this.renamePage(this.page, title);
  }

  pageDialog() {
    const d = this.dlg;
    if (!d) return html`<dialog class="page"></dialog>`;
    const problem = d.mode === "new" ? (d.id === "" ? null : pageIdProblem(d.id, this.pageIdList())) : null;
    const valid = d.title.trim() !== "" && (d.mode === "rename" || (d.id !== "" && !problem));
    return html`<dialog class="page" @close=${() => {
      this.dlg = null;
      this.update();
    }}>
      <form @submit=${(e) => {
        e.preventDefault();
        if (valid) this.submitPageDialog();
      }}>
        <h3>${t(d.mode === "new" ? "CORE_ADMIN_NEW_PAGE" : "CORE_ADMIN_RENAME_PAGE")}</h3>
        ${d.mode === "new"
          ? html`<label>${t("CORE_ADMIN_PAGE_ID")}
              <input type="text" name="page-id" maxlength="32" autocomplete="off" .value=${d.id}
                @input=${(e) => {
                  d.id = e.target.value;
                  this.update();
                }}></label>
              <p class="alert page-id-problem" ?hidden=${!problem}>${problem ? t(problem) : ""}</p>`
          : html`<p class="muted mono">${this.page}</p>`}
        <label>${t("CORE_ADMIN_PAGE_TITLE", { language: sessionLocale() })}
          <input type="text" name="page-title" autocomplete="off" .value=${d.title}
            @input=${(e) => {
              d.title = e.target.value;
              this.update();
            }}></label>
        <div class="row">
          <span class="spacer"></span>
          <button type="button" name="cancel" @click=${() => this.closePageDialog()}>${t("CORE_ADMIN_CANCEL")}</button>
          <button type="submit" class="primary" name="ok" ?disabled=${!valid}>${t(d.mode === "new" ? "CORE_ADMIN_ADD" : "CORE_ADMIN_RENAME")}</button>
        </div>
      </form>
    </dialog>`;
  }

  /** Every rectangle from the saved grid to the draft's (CORE-94), as one step. */
  async rescale() {
    const from = this.draft.baseGrid;
    const to = this.layout && this.layout.grid;
    const where = sectionLocation(this.draft.documents, this.draft.main, "modules/_order");
    if (!from || !to || !where) return;
    const list = getAt(this.draft.documents[where.file], where.parts);
    if (!Array.isArray(list)) return;
    const ops = [];
    list.forEach((item, i) => {
      if (isObject(item)) ops.push(...diffOps([...where.parts, i], item, rescaledItem(item, from, to)));
    });
    if (!ops.length) return;
    await this.host.run(() =>
      this.draft.applyOps([{ file: where.file, patch: ops }], t("CORE_ADMIN_STEP_RESCALED", { columns: to.columns, rows: to.rows })),
    );
  }

  canRescale() {
    const from = this.draft.baseGrid;
    const to = this.layout && this.layout.grid;
    if (!from || !to || (from.columns === to.columns && from.rows === to.rows)) return false;
    return this.draft.records.some((r) => r.code === "CORE_CFG_OUT_OF_GRID");
  }

  // --- Show on mirror (CORE-307) ------------------------------------------------------

  async setMirror(on) {
    clearTimeout(this.mirrorTimer);
    clearTimeout(this.pushTimer);
    this.mirrorOn = on;
    this.update();
    try {
      if (on) await this.pushMirror();
      else await api("DELETE", "/preview/mirror");
    } catch (err) {
      this.mirrorOn = false;
      this.host.problem(err);
    }
    this.update();
  }

  async pushMirror() {
    if (!this.layout) return;
    await api("POST", "/preview/mirror", this.layout);
    clearTimeout(this.mirrorTimer);
    this.mirrorTimer = setTimeout(() => {
      // The engine has reverted the kiosk by now (CORE-307).
      this.mirrorOn = false;
      this.update();
    }, MIRROR_TTL_MS);
  }

  /** The draft changed: tell the iframe, and the kiosk when Show on mirror is on. */
  draftChanged() {
    if (this.layout !== this.sentLayout) {
      if (this.layout) this.post({ kind: "control", action: "layout", layout: this.layout });
      this.sentLayout = this.layout;
      if (this.mirrorOn) {
        clearTimeout(this.pushTimer);
        this.pushTimer = setTimeout(() => this.pushMirror().catch((err) => this.host.problem(err)), 300);
      }
    }
    this.update();
  }

  // --- geometry -----------------------------------------------------------------------

  /** The CSS-pixel box of cells on the viewport (CORE-142, CORE-213). */
  box(r) {
    const { cell, grid } = this.layout;
    const [cw, ch] = cell;
    const x = grid.padding + (r.col - 1) * (cw + grid.gap);
    const y = grid.padding + (r.row - 1) * (ch + grid.gap);
    return { x, y, w: r.width * cw + (r.width - 1) * grid.gap, h: r.height * ch + (r.height - 1) * grid.gap };
  }

  fit() {
    const stage = this.querySelector(".stage");
    const frame = this.querySelector(".frame");
    if (!stage || !frame || !this.layout) return;
    const [vw, vh] = this.layout.viewport;
    const maxH = Math.max(window.innerHeight * 0.7, 200);
    this.scale = Math.min(1, stage.clientWidth / vw, maxH / vh);
    frame.style.width = `${Math.floor(vw * this.scale)}px`;
    frame.style.height = `${Math.floor(vh * this.scale)}px`;
    for (const el of [this.querySelector("iframe"), this.querySelector(".overlay")]) {
      if (!el) continue;
      el.style.width = `${vw}px`;
      el.style.height = `${vh}px`;
      el.style.transform = `scale(${this.scale})`;
    }
  }

  // --- the overlay (CORE-326) ---------------------------------------------------------

  drawOverlay() {
    const overlay = this.querySelector(".overlay");
    if (!overlay || !this.layout) return;
    overlay.replaceChildren();
    const { grid } = this.layout;
    for (let c = 1; c <= grid.columns; c += 1) {
      const b = this.box({ col: c, row: 1, width: 1, height: grid.rows });
      overlay.append(this.div("strip col", b));
    }
    for (let r = 1; r <= grid.rows; r += 1) {
      const b = this.box({ col: 1, row: r, width: grid.columns, height: 1 });
      overlay.append(this.div("strip row", b));
    }
    const placements = this.placements();
    for (const p of [...placements].sort((a, b) => a.z - b.z)) {
      const b = this.box(p);
      if (p.preset && p.stack_size > 1) {
        b.h = b.h / p.stack_size;
        b.y += b.h * p.stack_index;
      }
      const el = this.div(`box${p.instance === this.selected ? " selected" : ""}${p.preset ? " preset" : ""}`, b);
      el.dataset.instance = p.instance;
      const label = document.createElement("span");
      label.className = "label";
      label.textContent = p.instance;
      el.append(label);
      if (p.instance === this.selected) {
        for (const corner of ["nw", "ne", "sw", "se"]) {
          const h = document.createElement("span");
          h.className = `handle ${corner}`;
          h.dataset.corner = corner;
          el.append(h);
        }
      }
      overlay.append(el);
    }
  }

  div(cls, b) {
    const el = document.createElement("div");
    el.className = cls;
    el.style.left = `${b.x}px`;
    el.style.top = `${b.y}px`;
    el.style.width = `${b.w}px`;
    el.style.height = `${b.h}px`;
    return el;
  }

  point(ev) {
    const r = this.querySelector(".overlay").getBoundingClientRect();
    return { x: (ev.clientX - r.left) / this.scale, y: (ev.clientY - r.top) / this.scale };
  }

  down(ev) {
    const target = ev.target.closest(".box");
    if (!target) {
      if (this.selected !== null) {
        this.selected = null;
        this.update();
      }
      return;
    }
    const id = target.dataset.instance;
    const p = this.placements().find((q) => q.instance === id);
    if (!p) return;
    ev.preventDefault();
    if (this.selected !== id) {
      this.selected = id;
      this.update();
    }
    const handle = ev.target.closest(".handle");
    this.drag = {
      id,
      mode: handle ? handle.dataset.corner : "move",
      start: this.point(ev),
      orig: { col: p.col, row: p.row, width: p.width, height: p.height, z: p.z },
      rect: null,
      pointer: ev.pointerId,
    };
    this.querySelector(".overlay").setPointerCapture(ev.pointerId);
  }

  move(ev) {
    const d = this.drag;
    if (!d || ev.pointerId !== d.pointer) return;
    const { cell, grid } = this.layout;
    const now = this.point(ev);
    const dc = Math.round((now.x - d.start.x) / (cell[0] + grid.gap));
    const dr = Math.round((now.y - d.start.y) / (cell[1] + grid.gap));
    const o = d.orig;
    let r = { ...o };
    if (d.mode === "move") r = { ...o, col: o.col + dc, row: o.row + dr };
    else {
      if (d.mode.includes("e")) r.width = o.width + dc;
      if (d.mode.includes("s")) r.height = o.height + dr;
      if (d.mode.includes("w")) r = { ...r, col: o.col + dc, width: o.width - dc };
      if (d.mode.includes("n")) r = { ...r, row: o.row + dr, height: o.height - dr };
    }
    if (r.width < 1 || r.height < 1) return;
    d.rect = r;
    const valid = placementValid(r, otherRects(this.placements(), d.id), grid);
    d.valid = valid;
    const el = this.querySelector(`.overlay .box[data-instance="${CSS.escape(d.id)}"]`);
    if (!el) return;
    const b = this.box(r);
    el.style.left = `${b.x}px`;
    el.style.top = `${b.y}px`;
    el.style.width = `${b.w}px`;
    el.style.height = `${b.h}px`;
    el.classList.toggle("invalid", !valid);
  }

  up(ev) {
    const d = this.drag;
    if (!d || ev.pointerId !== d.pointer) return;
    this.drag = null;
    const changed = d.rect && ["col", "row", "width", "height"].some((k) => d.rect[k] !== d.orig[k]);
    if (!changed || !d.valid) {
      this.drawOverlay(); // invalid placements are not applied (CORE-326)
      return;
    }
    const key = d.mode === "move" ? "CORE_ADMIN_STEP_MOVED" : "CORE_ADMIN_STEP_RESIZED";
    this.placeAt(d.id, d.rect, t(key, { id: d.id }));
  }

  // --- the view -----------------------------------------------------------------------

  toolbar() {
    const id = this.selected;
    const p = id && this.placements().find((q) => q.instance === id);
    const item = id && this.item(id);
    if (!p || !item) return html`<p class="muted">${t("CORE_ADMIN_SELECT_INSTANCE")}</p>`;
    const app = isObject(item.appearance) ? item.appearance : {};
    const val = (k) => (k in app ? app[k] : APPEARANCE[k]);
    const num = (k, min, max, step) => html`<label>${t(`CORE_ADMIN_APPEARANCE_${k.toUpperCase()}`)}
      <input type="number" name=${k} min=${min} max=${max} step=${step} .value=${String(val(k))}
        @change=${(e) => {
          const v = Number(e.target.value);
          if (Number.isFinite(v) && v >= min && v <= max) this.setAppearance(id, k, v);
        }}></label>`;
    const pick = (k, options) => html`<label>${t(`CORE_ADMIN_APPEARANCE_${k.toUpperCase()}`)}
      <select name=${k} @change=${(e) => this.setAppearance(id, k, e.target.value)}>
        ${options.map((o) => html`<option value=${o} ?selected=${val(k) === o}>${o}</option>`)}
      </select></label>`;
    const header = val("header");
    return html`<div class="inst-toolbar">
      <div class="toolbar">
        <h3>${id}</h3><span class="muted">${item.module}</span>
        <span class="muted size">${t("CORE_ADMIN_LAYOUT_SIZE", {
          columns: p.width,
          rows: p.height,
          width: Math.floor(p.area[0]),
          height: Math.floor(p.area[1]),
        })}</span>
        <span class="spacer"></span>
        <button @click=${() => this.host.openForm(`modules/${id}`)}>${t("CORE_ADMIN_EDIT_IN_FORM")}</button>
        <button name="disable" @click=${() => this.setEnabled(id, false)}>${t("CORE_ADMIN_DISABLE")}</button>
        <button name="delete" @click=${() => this.deleteInstance(id)}>${t("CORE_ADMIN_DELETE")}</button>
      </div>
      <div class="fields">
        <label>${t("CORE_ADMIN_PRESET")}
          <select name="preset" @change=${(e) => this.setPreset(id, e.target.value)}>
            <option value="" ?selected=${!p.preset}>${t("CORE_ADMIN_PRESET_NONE")}</option>
            ${PRESETS.map((n) => html`<option value=${n} ?selected=${p.preset === n}>${n}</option>`)}
          </select></label>
        <label>${t("CORE_ADMIN_LAYER")}
          <input type="number" name="z" step="1" min="-1000" max="1000" .value=${String(p.z)} ?disabled=${Boolean(p.preset)}
            @change=${(e) => this.setZ(id, Number(e.target.value))}></label>
        ${num("scale", 0.5, 3, 0.1)} ${pick("align", ["start", "center", "end"])}
        ${pick("valign", ["start", "center", "end"])} ${num("opacity", 0, 1, 0.05)}
        ${pick("overflow", ["clip", "fit", "scroll"])}
        <label>${t("CORE_ADMIN_APPEARANCE_HEADER")}
          <input type="checkbox" name="header" .checked=${header === true || isObject(header)} ?disabled=${isObject(header)}
            @change=${(e) => this.setAppearance(id, "header", e.target.checked)}></label>
      </div>
    </div>`;
  }

  disabledList() {
    const values = sectionValues(this.draft.documents, this.draft.main);
    const ids = (values["modules/_order"] || []).filter(
      (id) => id && values[`modules/${id}`]?.disabled === true && this.kindOf(values[`modules/${id}`].module) !== "backend",
    );
    if (!ids.length) return nothing;
    return html`<section class="card disabled-list"><h3>${t("CORE_ADMIN_DISABLED_LIST")}</h3>
      <ul>${ids.map(
        (id) => html`<li><span class="mono">${id}</span>
          <button name="enable" @click=${() => this.setEnabled(id, true)}>${t("CORE_ADMIN_ENABLE")}</button>
          <button name="delete" @click=${() => this.deleteInstance(id)}>${t("CORE_ADMIN_DELETE")}</button></li>`,
      )}</ul></section>`;
  }

  /** The Services lane: a card per backend-only instance, enabled or not (CORE-375). */
  servicesLane() {
    const values = sectionValues(this.draft.documents, this.draft.main);
    const ids = (values["modules/_order"] || []).filter((id) => id && this.kindOf(values[`modules/${id}`]?.module) === "backend");
    if (!ids.length) return nothing;
    const header = (module) => (this.installed || []).find((m) => m.name === module)?.header || module;
    return html`<section class="card services-lane"><h3>${t("CORE_ADMIN_SERVICES")}</h3>
      <ul>${ids.map((id) => {
        const item = values[`modules/${id}`];
        const off = item.disabled === true;
        return html`<li data-instance=${id}><span class="mono">${id}</span>
          <span class="muted">${header(item.module)}</span>
          ${off ? html`<span class="muted">${t("CORE_ADMIN_DISABLED_STATE")}</span>` : nothing}
          <span class="spacer"></span>
          <button name="edit" @click=${() => this.host.openForm(`modules/${id}`)}>${t("CORE_ADMIN_EDIT_IN_FORM")}</button>
          <button name=${off ? "enable" : "disable"} @click=${() => this.setEnabled(id, off)}>${t(off ? "CORE_ADMIN_ENABLE" : "CORE_ADMIN_DISABLE")}</button>
          <button name="delete" @click=${() => this.deleteInstance(id)}>${t("CORE_ADMIN_DELETE")}</button></li>`;
      })}</ul></section>`;
  }

  update() {
    if (!this.host) return;
    const layout = this.layout;
    const pages = this.pages();
    const pageCount = this.pageIdList().length;
    if (layout && (this.page === null || !pages.includes(this.page))) this.page = pages[0] || null;
    render(
      html`<div class="layout-view">
        <div class="toolbar">
          <label>${t("CORE_ADMIN_PAGE")}
            <select class="page" @change=${(e) => this.setPage(e.target.value)}>
              ${pages.map((p) => html`<option value=${p} ?selected=${p === this.page}>${p}</option>`)}
            </select></label>
          <button name="new-page" ?disabled=${pageCount >= MAX_PAGES} @click=${() => this.openPageDialog("new")}>${t("CORE_ADMIN_NEW_PAGE")}</button>
          <button name="rename-page" ?disabled=${this.page === null} @click=${() => this.openPageDialog("rename")}>${t("CORE_ADMIN_RENAME_PAGE")}</button>
          <button name="delete-page" ?disabled=${pageCount < 2 || this.page === null} @click=${() => this.deletePage(this.page)}>${t("CORE_ADMIN_DELETE_PAGE")}</button>
          <button name="add-module" @click=${() => this.openAddDialog()}>${t("CORE_ADMIN_ADD_MODULE")}</button>
          ${layout ? html`<span class="muted">${layout.viewport[0]} × ${layout.viewport[1]} px</span>` : nothing}
          <span class="spacer"></span>
          ${this.canRescale()
            ? html`<button class="rescale" title=${t("CORE_ADMIN_RESCALE_HELP")} @click=${() => this.rescale()}>
                ${t("CORE_ADMIN_RESCALE")}</button>`
            : nothing}
          <label class="toggle"><input type="checkbox" class="mirror" .checked=${this.mirrorOn}
            @change=${(e) => this.setMirror(e.target.checked)}> ${t("CORE_ADMIN_SHOW_ON_MIRROR")}</label>
        </div>
        ${layout ? nothing : html`<p class="alert warn">${t("CORE_ADMIN_NO_LAYOUT")}</p>`}
        <div class="stage">
          <div class="frame">
            <iframe src="/admin/preview" title=${t("CORE_ADMIN_PREVIEW")} tabindex="-1"
              @load=${() => this.sendAll()}></iframe>
            <div class="overlay"
              @pointerdown=${(e) => this.down(e)}
              @pointermove=${(e) => this.move(e)}
              @pointerup=${(e) => this.up(e)}
              @pointercancel=${() => {
                this.drag = null;
                this.drawOverlay();
              }}></div>
          </div>
        </div>
        <section class="card">${this.toolbar()}</section>
        ${this.servicesLane()} ${this.disabledList()} ${this.pageDialog()} ${this.addDialog()}
      </div>`,
      this,
    );
    const stage = this.querySelector(".stage");
    if (stage && !this.observed) {
      this.resize.observe(stage);
      this.observed = true;
    }
    this.fit();
    if (!this.drag) this.drawOverlay();
  }
}

customElements.define("mm3-layout-view", LayoutView);
