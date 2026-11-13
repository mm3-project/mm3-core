// <mm3-schema-form>: the in-house form renderer of the admin Form view.
// Implements CORE-37 (Core spec 0.22, §6.4 Web configuration editor): fields come
// from the JSON Schema; `ui` hints (widget, order, group, hidden) are honoured.
// Implements CORE-310 (Core spec 0.22, §6.4 Web configuration editor): labels and
// help from CONFIG_<PATH>_LABEL / _HELP; a missing label shows the field name; a
// LocalizedText field has one input per language plus "add language".
// Implements CORE-303 (Core spec 0.22, §6.4 Web configuration editor) and CORE-320
// (Core spec 0.25, §6.4 Web configuration editor): a secret shows as kept until a
// new value is typed; !env references are shown and edited as written.
// Implements CORE-35, CORE-206 (Core spec 0.38, §6.4 Web configuration editor):
// errors inline on the field and, for the section itself, at the top of the form.
//
// Properties: schema (the root, for $refs), node (this form's schema), value,
// texts ({key: text}), textPrefix (CONFIG_<SECTION> or CONFIG), records ({pointer:
// [message]}). Events: "mm3-change" {path, value, remove, label}.

import { html, nothing, render } from "/admin/static/lit-html.js";
import { t } from "/admin/static/admin.js";
import { escapePart, isTag } from "/admin/static/draft.js";

const LANGS = ["en", "it", "fr", "es", "de"];

function snake(name) {
  return String(name)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .toUpperCase();
}

function short(value) {
  if (value === undefined) return "–";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
}

export class SchemaForm extends HTMLElement {
  constructor() {
    super();
    this.schema = null;
    this.node = null;
    this.value = undefined;
    this.texts = {};
    this.textPrefix = "CONFIG";
    this.records = {};
    this.section = "";
    this.instance = false;
    this.secretEdit = new Set();
  }

  set data(d) {
    Object.assign(this, d);
    this.update();
  }

  // --- schema helpers ----------------------------------------------------------------

  deref(node) {
    let n = node;
    for (let i = 0; i < 20 && n && n.$ref; i += 1) {
      const name = n.$ref.split("/").pop();
      n = { ...this.schema.$defs[name], ...Object.fromEntries(Object.entries(n).filter(([k]) => k !== "$ref")) };
    }
    return n || {};
  }

  /** Strip the !env / !include wrapper of 6.4 and the null branch of optional fields. */
  unwrap(node) {
    let n = this.deref(node);
    let nullable = false;
    for (let i = 0; i < 6 && (n.anyOf || n.oneOf); i += 1) {
      const branches = (n.anyOf || n.oneOf).filter((b) => {
        const ref = b.$ref || "";
        if (ref.endsWith("/mm3_env") || ref.endsWith("/mm3_include")) return false;
        if (b.type === "null") {
          nullable = true;
          return false;
        }
        return true;
      });
      const meta = Object.fromEntries(Object.entries(n).filter(([k]) => k !== "anyOf" && k !== "oneOf"));
      if (branches.length === 1) n = { ...this.deref(branches[0]), ...meta };
      else {
        n = { ...meta, anyOf: branches };
        break;
      }
    }
    return { node: n, nullable };
  }

  isLocalized(node) {
    const b = node.anyOf || [];
    return (
      b.length === 2 &&
      b.some((x) => x.type === "string") &&
      b.some((x) => x.type === "object" && x.additionalProperties && x.additionalProperties.type === "string")
    );
  }

  isSecret(node) {
    return node.secret === true || node.format === "password" || node.writeOnly === true;
  }

  // --- texts ---------------------------------------------------------------------------

  textKey(names, kind) {
    // CORE-310: a module's own fields (under config) use its catalog without the
    // section; instance keys and engine sections use the core catalog, section first.
    let prefix = this.textPrefix;
    let rest = names.filter((n) => typeof n !== "number");
    if (this.instance) {
      if (rest[0] === "config") rest = rest.slice(1);
      else prefix = "CONFIG_MODULES";
    }
    const path = rest.map(snake).filter(Boolean).join("_");
    return `${prefix}${path ? `_${path}` : ""}_${kind}`;
  }

  label(names, node) {
    const key = this.textKey(names, "LABEL");
    return this.texts[key] || node.title || names[names.length - 1] || "";
  }

  help(names) {
    return this.texts[this.textKey(names, "HELP")] || "";
  }

  // --- changes -----------------------------------------------------------------------

  emit(path, value, old, names, remove = false) {
    const label = `${[this.section, ...names].filter(Boolean).join(".")} ${short(old)} → ${remove ? "–" : short(value)}`;
    this.dispatchEvent(new CustomEvent("mm3-change", { detail: { path, value, remove, label }, bubbles: true }));
  }

  // --- rendering -----------------------------------------------------------------------

  update() {
    if (!this.schema || !this.node) {
      render(nothing, this);
      return;
    }
    // ISS-010: records on the section itself (path "" here, such as CORE-33's missing
    // position on an instance) are shown at the top; no field would show them.
    render(html`<form class="schema-form" @submit=${(e) => e.preventDefault()}>
      ${this.errors("")}
      ${this.field([], [], this.node, this.value, true)}
    </form>`, this);
  }

  errors(path) {
    const list = this.records[path] || [];
    return list.length ? html`<div class="field-error" role="alert">${list.join(" · ")}</div>` : nothing;
  }

  field(parts, names, rawNode, value, root = false) {
    const { node, nullable } = this.unwrap(rawNode);
    const ui = node.ui || {};
    if (ui.hidden && !root) return nothing;
    const path = parts.map((p) => `/${escapePart(p)}`).join("");
    if (isTag(value)) return this.tagged(path, names, node, value);
    const label = root ? nothing : html`<span class="label">${this.label(names, node)}</span>`;
    const help = this.help(names);
    const wrap = (control) => html`<div class="field" data-path=${path}>
      <label>${label}${control}</label>
      ${help && !root ? html`<small class="muted">${help}</small>` : nothing}
      ${nullable && value !== undefined && value !== null && !root
        ? html`<button type="button" class="link" @click=${() => this.emit(path, null, value, names, true)}>${t("CORE_ADMIN_CLEAR")}</button>`
        : nothing}
      ${this.errors(path)}
    </div>`;

    if (this.isSecret(node)) return wrap(this.secret(path, names, value));
    if (this.isLocalized(node)) return wrap(this.localized(path, names, value));
    const type = node.type || (node.properties ? "object" : node.enum ? "string" : undefined);
    if (node.enum || node.const !== undefined) {
      const options = node.enum || [node.const];
      return wrap(html`<select @change=${(e) => this.emit(path, options[Number(e.target.value)], value, names)}>
        ${value === undefined ? html`<option selected disabled value="">–</option>` : nothing}
        ${options.map((o, i) => html`<option value=${i} ?selected=${equalish(o, value)}>${String(o)}</option>`)}
      </select>`);
    }
    if (node.anyOf) return wrap(this.choice(path, names, node, value));
    if (type === "object" && node.properties) return this.object(parts, names, node, value, root);
    if (type === "object") return wrap(this.mapping(path, names, node, value));
    if (type === "array") return this.array(parts, names, node, value);
    if (type === "boolean") {
      return wrap(html`<input type="checkbox" class="toggle" .checked=${value === true}
        @change=${(e) => this.emit(path, e.target.checked, value, names)}>`);
    }
    if (type === "integer" || type === "number") {
      const min = node.minimum ?? node.exclusiveMinimum;
      const max = node.maximum ?? node.exclusiveMaximum;
      return wrap(html`<input type="number" step=${type === "integer" ? "1" : "any"}
        min=${min ?? ""} max=${max ?? ""} .value=${value ?? ""} placeholder=${node.default ?? ""}
        @change=${(e) => this.number(e, path, value, names, type, min, max)}>`);
    }
    const inputType = node.format === "time" ? "time" : node.format === "date" ? "date" : "text";
    const placeholder = node.format === "duration" ? `${node.default ?? ""} (30 min, 1 h, PT10M)` : node.default ?? "";
    return wrap(html`<input type=${inputType} .value=${value ?? ""} placeholder=${placeholder}
      pattern=${node.pattern ?? ""} @change=${(e) => this.text(e, path, value, names, node)}>`);
  }

  number(e, path, old, names, type, min, max) {
    const raw = e.target.value;
    if (raw === "") {
      this.emit(path, null, old, names, true);
      return;
    }
    const v = type === "integer" ? Number.parseInt(raw, 10) : Number(raw);
    if (Number.isNaN(v) || (min !== undefined && v < min) || (max !== undefined && v > max)) {
      e.target.setCustomValidity(t("CORE_ADMIN_OUT_OF_RANGE", { min: min ?? "–", max: max ?? "–" }));
      e.target.reportValidity();
      return;
    }
    e.target.setCustomValidity("");
    this.emit(path, v, old, names);
  }

  text(e, path, old, names, node) {
    const v = e.target.value;
    if (node.pattern && v && !new RegExp(`^(?:${node.pattern})$`).test(v)) {
      e.target.setCustomValidity(t("CORE_ADMIN_BAD_FORMAT"));
      e.target.reportValidity();
      return;
    }
    e.target.setCustomValidity("");
    if (v === "" && old === undefined) return;
    if (v === "") this.emit(path, null, old, names, true);
    else this.emit(path, v, old, names);
  }

  object(parts, names, node, value, root) {
    const v = value && typeof value === "object" ? value : {};
    const props = Object.entries(node.properties).sort(
      ([, a], [, b]) => ((this.deref(a).ui || {}).order ?? 1e6) - ((this.deref(b).ui || {}).order ?? 1e6),
    );
    const groups = new Map();
    for (const [key, sub] of props) {
      const group = (this.unwrap(sub).node.ui || {}).group || "";
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push([key, sub]);
    }
    const body = [...groups].map(([group, entries]) => html`
      ${group ? html`<h4 class="group">${this.texts[`${this.textPrefix}_GROUP_${snake(group)}`] || group}</h4>` : nothing}
      ${entries.map(([key, sub]) => this.field([...parts, key], [...names, key], sub, v[key]))}`);
    if (root) return html`${body}`;
    const legend =
      this.instance && names.length === 1 && names[0] === "config"
        ? t("CORE_ADMIN_MODULE_SETTINGS")
        : this.label(names, node);
    return html`<fieldset class="nested"><legend>${legend}</legend>${body}
      ${this.errors(parts.map((p) => `/${escapePart(p)}`).join(""))}</fieldset>`;
  }

  array(parts, names, node, value) {
    const path = parts.map((p) => `/${escapePart(p)}`).join("");
    const list = Array.isArray(value) ? value : [];
    const item = node.items || {};
    const { node: itemNode } = this.unwrap(item);
    const blank = itemNode.type === "object" ? {} : itemNode.type === "integer" || itemNode.type === "number" ? 0 : "";
    return html`<fieldset class="nested list" data-path=${path}><legend>${this.label(names, node)}</legend>
      ${list.map((entry, i) => html`<div class="list-item">
        ${this.field([...parts, i], [...names], item, entry)}
        <button type="button" class="link" @click=${() => this.emit(`${path}/${i}`, undefined, entry, [...names, i], true)}>
          ${t("CORE_ADMIN_REMOVE")}</button>
      </div>`)}
      <button type="button" @click=${() => this.emit(value === undefined ? path : `${path}/-`, value === undefined ? [blank] : blank, undefined, names)}>
        ${t("CORE_ADMIN_ADD")}</button>
      ${this.errors(path)}
    </fieldset>`;
  }

  mapping(path, names, node, value) {
    const v = value && typeof value === "object" ? value : {};
    return html`<div class="mapping">
      ${Object.entries(v).map(([k, x]) => html`<div class="row"><span class="mono">${k}</span>
        <input .value=${typeof x === "string" ? x : JSON.stringify(x)}
          @change=${(e) => this.emit(`${path}/${escapePart(k)}`, e.target.value, x, [...names, k])}>
        <button type="button" class="link" @click=${() => this.emit(`${path}/${escapePart(k)}`, undefined, x, [...names, k], true)}>
          ${t("CORE_ADMIN_REMOVE")}</button></div>`)}
      <button type="button" @click=${() => {
        const k = window.prompt(t("CORE_ADMIN_KEY"));
        if (k) this.emit(value === undefined ? path : `${path}/${escapePart(k)}`, value === undefined ? { [k]: "" } : "", undefined, [...names, k]);
      }}>${t("CORE_ADMIN_ADD")}</button>
    </div>`;
  }

  choice(path, names, node, value) {
    // A union (for example a region preset or a grid rectangle): pick the branch.
    const branches = node.anyOf.map((b) => this.unwrap(b).node);
    const current = branches.findIndex((b) => matches(b, value));
    const blank = (b) =>
      b.const !== undefined ? b.const : b.enum ? b.enum[0] : b.type === "object" ? {} : b.type === "array" ? [] : b.type === "string" ? "" : b.type === "boolean" ? false : 0;
    return html`<div class="choice">
      <select @change=${(e) => this.emit(path, blank(branches[Number(e.target.value)]), value, names)}>
        ${branches.map((b, i) => html`<option value=${i} ?selected=${i === current}>${branchName(b)}</option>`)}
      </select>
      ${current >= 0 ? this.field(path.split("/").slice(1).map(unescape), names, branches[current], value, true) : nothing}
    </div>`;
  }

  localized(path, names, value) {
    if (typeof value === "string" || value === undefined || value === null) {
      return html`<span class="row"><input .value=${value ?? ""} @change=${(e) => this.emit(path, e.target.value, value, names)}>
        <button type="button" class="link" @click=${() => this.emit(path, { en: value || "" }, value, names)}>${t("CORE_ADMIN_ADD_LANGUAGE")}</button></span>`;
    }
    const missing = LANGS.filter((l) => !(l in value));
    return html`<div class="localized">
      ${Object.entries(value).map(([lang, text]) => html`<div class="row"><span class="mono">${lang}</span>
        <input .value=${text} @change=${(e) => this.emit(`${path}/${escapePart(lang)}`, e.target.value, text, [...names, lang])}>
        <button type="button" class="link" @click=${() => this.emit(`${path}/${escapePart(lang)}`, undefined, text, [...names, lang], true)}>
          ${t("CORE_ADMIN_REMOVE")}</button></div>`)}
      ${missing.length
        ? html`<select @change=${(e) => {
            if (e.target.value) this.emit(`${path}/${escapePart(e.target.value)}`, "", undefined, [...names, e.target.value]);
          }}><option value="">${t("CORE_ADMIN_ADD_LANGUAGE")}</option>${missing.map((l) => html`<option>${l}</option>`)}</select>`
        : nothing}
    </div>`;
  }

  secret(path, names, value) {
    const kept = isTag(value) && "!masked" in value;
    if (kept && !this.secretEdit.has(path)) {
      return html`<span class="row"><span class="muted">•••••••• ${t("CORE_ADMIN_SECRET_KEPT")}</span>
        <button type="button" @click=${() => {
          this.secretEdit.add(path);
          this.update();
        }}>${t("CORE_ADMIN_CHANGE")}</button>
        <button type="button" @click=${() => this.useEnv(path, value, names)}>${t("CORE_ADMIN_USE_ENV")}</button></span>`;
    }
    return html`<span class="row"><input type="password" autocomplete="new-password"
        @change=${(e) => {
          this.secretEdit.delete(path);
          this.emit(path, e.target.value, kept ? "••••" : undefined, names);
        }}>
      <button type="button" @click=${() => this.useEnv(path, value, names)}>${t("CORE_ADMIN_USE_ENV")}</button></span>`;
  }

  useEnv(path, value, names) {
    const name = window.prompt(t("CORE_ADMIN_ENV_NAME"));
    if (name) this.emit(path, { "!env": name.trim() }, isTag(value) ? "••••" : value, names);
  }

  tagged(path, names, node, value) {
    const label = html`<span class="label">${this.label(names, node)}</span>`;
    if ("!masked" in value) return html`<div class="field" data-path=${path}><label>${label}${this.secret(path, names, value)}</label>${this.errors(path)}</div>`;
    if ("!include" in value) {
      return html`<div class="field" data-path=${path}><label>${label}
        <span class="tag mono">!include ${value["!include"]}</span></label>
        <small class="muted">${t("CORE_ADMIN_INCLUDE_HINT")}</small></div>`;
    }
    const env = value["!env"];
    const written = Array.isArray(env) ? `${env[0]} (${env[1]})` : env;
    return html`<div class="field" data-path=${path}><label>${label}
      <span class="tag mono">!env ${written}</span>
      <button type="button" @click=${() => this.useEnv(path, value, names)}>${t("CORE_ADMIN_CHANGE")}</button>
      <button type="button" class="link" @click=${() => this.emit(path, null, value, names, true)}>${t("CORE_ADMIN_CLEAR")}</button>
    </label>${this.errors(path)}</div>`;
  }
}

function branchName(b) {
  if (b.const !== undefined) return String(b.const);
  if (b.enum) return t("CORE_ADMIN_PRESET");
  if (b.type === "array") return t("CORE_ADMIN_LIST");
  if (b.type === "boolean") return t("CORE_ADMIN_ON_OFF");
  if (b.type === "string") return t("CORE_ADMIN_TEXT");
  if (b.type === "integer" || b.type === "number") return t("CORE_ADMIN_NUMBER");
  return b.title || b.type || "?";
}

function equalish(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

function matches(node, value) {
  if (value === undefined || value === null) return false;
  if (node.const !== undefined) return equalish(node.const, value);
  if (node.enum) return node.enum.some((e) => equalish(e, value));
  if (node.type === "object") return typeof value === "object" && !Array.isArray(value);
  if (node.type === "array") return Array.isArray(value);
  if (node.type === "string") return typeof value === "string";
  if (node.type === "boolean") return typeof value === "boolean";
  if (node.type === "integer" || node.type === "number") return typeof value === "number";
  return false;
}

function unescape(part) {
  return part.replace(/~1/g, "/").replace(/~0/g, "~");
}

customElements.define("mm3-schema-form", SchemaForm);
