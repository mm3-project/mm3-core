// The admin config page /admin/config: Form, YAML and History tabs over one draft.
// Implements CORE-322 (Core spec 0.25, §6.4 Web configuration editor): one page with
// the editor views and the History tab (the Layout tab joins in part D2).
// Implements CORE-151 (Core spec 0.10, §6.8 Configuration history, undo and redo):
// toolbar Undo/Redo with the step label, Ctrl+Z, Ctrl+Shift+Z and Ctrl+Y.
// Implements CORE-152 (Core spec 0.22, §6.8 Configuration history, undo and redo):
// "Undo last change here" on each section of the Form view.
// Implements CORE-302, CORE-35, CORE-300 (Core spec 0.22, §6.4 Web configuration editor):
// Save validates and shows the diff first; Save is disabled while an error remains;
// a conflict offers to reload.
// Implements CORE-324 (Core spec 0.26, §6.8 Configuration history, undo and redo):
// Open in editor loads a version as the draft.

import { html, nothing, render } from "/admin/static/lit-html.js";
import { loadTexts, t } from "/admin/static/admin.js";
import {
  Draft,
  TOP_SECTIONS,
  escapePart,
  getAt,
  instanceId,
  pointer,
  sectionLocation,
  sectionValues,
} from "/admin/static/draft.js";
import "/admin/static/schemaform.js";
import "/admin/static/history.js";
import { YamlView } from "/admin/static/yamlview.js";

function unescapePart(p) {
  return p.replace(/~1/g, "/").replace(/~0/g, "~");
}

class AdminConfig extends HTMLElement {
  constructor() {
    super();
    this.tab = "form";
    this.draft = new Draft();
    this.schema = null;
    this.forms = {};
    this.section = "engine";
    this.file = null;
    this.message = "";
    this.preview = null;
    this.busy = false;
    this.yaml = null;
    this.draft.addEventListener("change", () => this.update());
    this.draft.addEventListener("problem", (e) => this.problem(e.detail));
  }

  async connectedCallback() {
    const texts = await loadTexts();
    this.forms = (texts && texts.forms) || {};
    document.addEventListener("keydown", (e) => this.keys(e));
    try {
      [this.schema] = await Promise.all([
        fetch("/api/v1/config/schema", { credentials: "same-origin" }).then((r) => r.json()),
        this.draft.load(),
      ]);
    } catch (err) {
      this.problem(err);
    }
    this.file = this.draft.main;
    this.update();
  }

  problem(err) {
    const p = (err && err.problem) || {};
    this.message = err && err.message ? err.message : String(err);
    this.conflict = p.code === "CORE_API_CONFLICT" || p.code === "CORE_API_DISK_INVALID";
    this.update();
  }

  keys(e) {
    if (!(e.ctrlKey || e.metaKey) || this.tab === "history") return;
    if (e.target && e.target.closest && e.target.closest(".cm-editor")) return; // the editor's own keymap
    const k = e.key.toLowerCase();
    if (k === "z" && !e.shiftKey) {
      e.preventDefault();
      this.undo();
    } else if ((k === "z" && e.shiftKey) || k === "y") {
      e.preventDefault();
      this.redo();
    }
  }

  async run(fn) {
    this.busy = true;
    this.update();
    try {
      await fn();
      this.message = "";
      this.conflict = false;
    } catch (err) {
      this.problem(err);
    }
    this.busy = false;
    this.update();
  }

  undo() {
    return this.run(async () => {
      await this.draft.undo();
      this.yaml?.sync();
    });
  }

  redo() {
    return this.run(async () => {
      await this.draft.redo();
      this.yaml?.sync();
    });
  }

  setTab(tab) {
    if (tab === this.tab) return;
    this.draft.flushTyping();
    if (this.yaml) {
      this.yaml.destroy();
      this.yaml = null;
    }
    this.tab = tab;
    this.update();
  }

  // --- the Form view ------------------------------------------------------------------

  sections() {
    const values = sectionValues(this.draft.documents, this.draft.main);
    const out = TOP_SECTIONS.map((s) => ({ id: s, label: s, present: s in values }));
    for (const id of values["modules/_order"] || []) if (id) out.push({ id: `modules/${id}`, label: id, present: true });
    return out;
  }

  sectionSchema(section) {
    const props = this.schema && this.schema.properties ? this.schema.properties : {};
    if (TOP_SECTIONS.includes(section)) return props[section];
    const item = (props.modules && props.modules.anyOf ? props.modules.anyOf[0] : props.modules || {}).items || {};
    const value = this.sectionValue(section);
    const variants = item.oneOf || [item];
    const variant = variants.find((v) => {
      const m = v.properties && v.properties.module;
      const c = m && (m.const ?? (m.anyOf && m.anyOf[0].const));
      return c === (value && value.module);
    });
    return variant || variants[0];
  }

  sectionValue(section) {
    return sectionValues(this.draft.documents, this.draft.main)[section];
  }

  formTexts(section) {
    const core = this.forms[""] || {};
    if (!section.startsWith("modules/")) return { texts: core, textPrefix: `CONFIG_${section.toUpperCase()}` };
    const value = this.sectionValue(section) || {};
    return { texts: { ...core, ...(this.forms[value.module] || {}) }, textPrefix: "CONFIG", module: value.module };
  }

  formRecords(section) {
    const loc = sectionLocation(this.draft.documents, this.draft.main, section);
    const out = {};
    if (!loc || !loc.parts) return out;
    const base = pointer(loc.parts);
    for (const r of this.draft.records) {
      if (!r.path || !r.path.startsWith(base)) continue;
      const rel = r.path.slice(base.length);
      (out[rel] = out[rel] || []).push(r.message);
    }
    return out;
  }

  /** A form change: one JSON Patch op on the file that holds the section (CORE-301). */
  async formChange(detail) {
    const section = this.section;
    const loc = sectionLocation(this.draft.documents, this.draft.main, section);
    if (!loc) return;
    let parts = loc.parts;
    if (!parts) return;
    const rel = detail.path ? detail.path.split("/").slice(1).map(unescapePart) : [];
    const doc = this.draft.documents[loc.file];
    let target = [...parts, ...rel];
    let value = detail.value;
    let op;
    if (detail.remove) {
      op = getAt(doc, target) === undefined ? null : { op: "remove", path: pointer(target) };
    } else {
      // Create missing parents (for example a config block that is not written yet).
      let i = parts.length;
      while (i < target.length - 1 && getAt(doc, target.slice(0, i + 1)) !== undefined) i += 1;
      if (i < target.length - 1) {
        const tail = target.slice(i + 1);
        target = target.slice(0, i + 1);
        value = tail.reduceRight((acc, key) => (key === "-" ? [acc] : { [key]: acc }), value);
      }
      const last = target[target.length - 1];
      const exists = last !== "-" && getAt(doc, target) !== undefined;
      op = { op: exists ? "replace" : "add", path: pointer(target), value };
    }
    if (!op) return;
    await this.run(() => this.draft.apply(loc.file, [op], detail.label));
  }

  async sectionUndo(section) {
    await this.run(() =>
      this.draft.undoSection(section, (step) => t("CORE_ADMIN_UNDO_STEP", { label: step.label })),
    );
    this.yaml?.sync();
  }

  formView() {
    const sections = this.sections();
    if (!sections.some((s) => s.id === this.section)) this.section = "engine";
    const section = this.section;
    const value = this.sectionValue(section);
    const { texts, textPrefix } = this.formTexts(section);
    const last = this.draft.lastStepFor(section);
    return html`<div class="editor">
      <nav class="sections">
        ${sections.map(
          (s) => html`<button class=${[s.id === section ? "active" : "", s.present ? "" : "muted", this.hasError(s.id) ? "has-error" : ""].join(" ")}
            @click=${() => {
              this.section = s.id;
              this.update();
            }}>${s.id.startsWith("modules/") ? html`<span class="muted">▸</span> ${s.label}` : s.label}</button>`,
        )}
      </nav>
      <section class="card form">
        <div class="toolbar">
          <h2>${section}</h2>
          <span class="spacer"></span>
          <button ?disabled=${!last || this.busy} title=${last ? last.label : ""} @click=${() => this.sectionUndo(section)}>
            ${t("CORE_ADMIN_UNDO_HERE")}</button>
        </div>
        <mm3-schema-form
          .data=${{
            schema: this.schema,
            node: this.sectionSchema(section) || {},
            value,
            texts,
            textPrefix,
            section,
            instance: section.startsWith("modules/"),
            records: this.formRecords(section),
          }}
          @mm3-change=${(e) => this.formChange(e.detail)}></mm3-schema-form>
      </section>
    </div>`;
  }

  hasError(section) {
    const loc = sectionLocation(this.draft.documents, this.draft.main, section);
    if (!loc || !loc.parts) return false;
    const base = pointer(loc.parts);
    return this.draft.records.some((r) => r.severity === "error" && r.path && r.path.startsWith(base));
  }

  // --- the YAML view ------------------------------------------------------------------

  yamlView() {
    const files = [...this.draft.files.keys()];
    if (!this.file || !this.draft.files.has(this.file)) this.file = this.draft.main;
    return html`<div class="yaml">
      <nav class="tabs files">${files.map(
        (f) => html`<button class=${f === this.file ? "active" : ""} @click=${() => this.openFile(f)}>
          ${f}${this.draft.files.get(f).text !== this.draft.files.get(f).saved ? " •" : ""}</button>`,
      )}</nav>
      <div class="cm-host"></div>
      ${this.recordList(this.draft.records.filter((r) => !r.file || r.file.endsWith(this.file)))}
    </div>`;
  }

  openFile(path) {
    this.draft.flushTyping();
    this.file = path;
    this.update();
    this.yaml?.open(path);
    this.yaml?.sync();
  }

  mountYaml() {
    const host = this.querySelector(".cm-host");
    if (!host) return;
    if (!this.yaml) {
      this.yaml = new YamlView(host, {
        draft: this.draft,
        schema: () => this.schema,
        onUndo: () => this.undo(),
        onRedo: () => this.redo(),
        recordsFor: (path) => this.draft.records.filter((r) => r.file && r.file.endsWith(path)),
        onRecords: () => this.update(),
        onProblem: (err) => this.problem(err),
        pathPrefix: (path) => this.pathPrefix(path),
      });
      this.yaml.open(this.file);
      this.yaml.sync();
    }
  }

  /** Where an included file sits in the main document (for completion). */
  pathPrefix(path) {
    if (path === this.draft.main) return [];
    for (const s of [...TOP_SECTIONS, "modules"]) {
      const loc = sectionLocation(this.draft.documents, this.draft.main, s === "modules" ? "modules/_order" : s);
      if (loc && loc.file === path) return [s];
    }
    return [];
  }

  recordList(records) {
    if (!records.length) return nothing;
    return html`<section class="card"><table><tbody>${records.map(
      (r) => html`<tr><td class="level-${r.severity}">${r.severity}</td><td class="mono nowrap">${r.line ? `${r.line}:${r.col}` : ""}</td>
        <td class="mono">${r.path}</td><td>${r.message}</td></tr>`,
    )}</tbody></table></section>`;
  }

  // --- save ---------------------------------------------------------------------------

  async askSave() {
    await this.run(async () => {
      this.preview = await this.draft.validate();
    });
    this.update();
    this.querySelector("dialog.save")?.showModal();
  }

  async save() {
    this.querySelector("dialog.save")?.close();
    let saved = null;
    await this.run(async () => {
      saved = await this.draft.save();
    });
    if (saved) this.message = t("CORE_ADMIN_SAVED_VERSION", { version: saved.version });
    this.preview = null;
    this.yaml?.destroy();
    this.yaml = null;
    this.querySelector("mm3-config-history")?.reload();
    this.update();
  }

  async reloadDraft(keep = false) {
    await this.run(() => (keep ? this.draft.load() : this.draft.discard()));
    this.yaml?.destroy();
    this.yaml = null;
    this.update();
  }

  saveDialog() {
    const p = this.preview;
    if (!p) return html`<dialog class="save"></dialog>`;
    const errors = p.records.filter((r) => r.severity === "error");
    return html`<dialog class="save">
      <h2>${t("CORE_ADMIN_SAVE_TITLE")}</h2>
      ${this.recordList(p.records)}
      ${p.diff.length
        ? html`<div class="diff">${p.diff.map(
            (d) => html`<h3>${d.section}</h3><pre>${d.before}</pre><pre>${d.after}</pre>`,
          )}</div>`
        : html`<p class="muted">${t("CORE_ADMIN_NO_CHANGES")}</p>`}
      <div class="toolbar">
        <button @click=${() => this.querySelector("dialog.save")?.close()}>${t("CORE_ADMIN_CANCEL")}</button>
        <button class="primary" ?disabled=${errors.length > 0 || this.busy} @click=${() => this.save()}>
          ${t("CORE_ADMIN_SAVE")}</button>
      </div>
    </dialog>`;
  }

  // --- the page -----------------------------------------------------------------------

  update() {
    render(this.view(), this);
    if (this.tab === "yaml") this.mountYaml();
  }

  view() {
    const d = this.draft;
    const undo = d.undoStack[d.undoStack.length - 1];
    const redo = d.redoStack[d.redoStack.length - 1];
    const errors = d.records.filter((r) => r.severity === "error").length;
    const tab = (id, key) =>
      html`<button class=${this.tab === id ? "active" : ""} @click=${() => this.setTab(id)}>${t(key)}</button>`;
    return html`
      <nav class="tabs">
        ${tab("form", "CORE_ADMIN_TAB_FORM")} ${tab("yaml", "CORE_ADMIN_TAB_YAML")} ${tab("history", "CORE_ADMIN_TAB_HISTORY")}
        <span class="spacer"></span>
        ${this.tab !== "history"
          ? html`
              <button ?disabled=${!undo || this.busy} title=${undo ? undo.label : ""} @click=${() => this.undo()}>
                ${t("CORE_ADMIN_UNDO")}${undo ? html` <span class="muted">${undo.label}</span>` : nothing}</button>
              <button ?disabled=${!redo || this.busy} title=${redo ? redo.label : ""} @click=${() => this.redo()}>
                ${t("CORE_ADMIN_REDO")}</button>`
          : nothing}
      </nav>
      ${this.message
        ? html`<p class="alert warn" role="status">${this.message}
            ${this.conflict ? html` <button @click=${() => this.reloadDraft()}>${t("CORE_ADMIN_RELOAD")}</button>` : nothing}</p>`
        : nothing}
      ${d.dirty
        ? html`<div class="savebar">
            <span>${t("CORE_ADMIN_UNSAVED")}</span>
            ${errors ? html`<span class="level-error">${t("CORE_ADMIN_ERRORS", { count: errors })}</span>` : nothing}
            <span class="spacer"></span>
            <button @click=${() => this.reloadDraft()}>${t("CORE_ADMIN_DISCARD")}</button>
            <button class="primary" ?disabled=${errors > 0 || this.busy} @click=${() => this.askSave()}>${t("CORE_ADMIN_SAVE")}</button>
          </div>`
        : nothing}
      ${this.tab === "form" ? this.formView() : nothing}
      ${this.tab === "yaml" ? this.yamlView() : nothing}
      ${this.tab === "history"
        ? html`<mm3-config-history
            @mm3-open=${(e) => this.openVersion(e.detail.version)}
            @mm3-saved=${() => this.reloadDraft(true)}></mm3-config-history>`
        : nothing}
      ${this.saveDialog()}`;
  }

  async openVersion(version) {
    if (this.draft.dirty && !window.confirm(t("CORE_ADMIN_REPLACE_DRAFT"))) return;
    await this.run(() => this.draft.openVersion(version, t("CORE_ADMIN_OPENED_VERSION", { version })));
    this.setTab("form");
  }
}

customElements.define("mm3-admin-config", AdminConfig);
export { escapePart, instanceId };
