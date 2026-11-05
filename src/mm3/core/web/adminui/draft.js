// The editing draft of the admin config page: files, data, records and one
// undo history shared by the Form and YAML views (and the Layout view).
// Implements CORE-301 (Core spec 0.28, §6.4 Web configuration editor): the draft
// lives in the browser; every edit goes through POST /config/patch, which returns
// the changed files, the records, the documents and the draft's layout object.
// Implements CORE-151 (Core spec 0.10, §6.8 Configuration history, undo and redo):
// one undo/redo history for the draft, each step a JSON Patch with a readable
// label, at least 200 steps; YAML typing is grouped per pause of 1 s.
// Implements CORE-152 (Core spec 0.22, §6.8 Configuration history, undo and redo):
// section undo reverts only the latest step touching a section of CORE-216.
// Implements CORE-300 (Core spec 0.22, §6.4 Web configuration editor): save with If-Match.
// Implements CORE-302 (Core spec 0.28, §6.4 Web configuration editor): validate with the
// diff preview.

import { ApiError } from "/admin/static/admin.js";

export const MAX_STEPS = 500;
const STORE_KEY = "mm3.admin.draft";
export const TOP_SECTIONS = ["engine", "display", "audio", "layout", "pages", "backup"];

// --- JSON helpers --------------------------------------------------------------------

export const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));

export function equal(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function escapePart(part) {
  return String(part).replace(/~/g, "~0").replace(/\//g, "~1");
}

export function pointer(parts) {
  return parts.map((p) => `/${escapePart(p)}`).join("");
}

export function getAt(doc, parts) {
  let node = doc;
  for (const p of parts) {
    if (node === null || typeof node !== "object" || isTag(node)) return undefined;
    node = node[p];
  }
  return node;
}

export function isTag(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  return keys.length === 1 && keys[0].startsWith("!");
}

export function includeOf(value) {
  return isTag(value) && "!include" in value ? value["!include"] : null;
}

function resolvePath(base, rel) {
  const dir = base.includes("/") ? base.slice(0, base.lastIndexOf("/") + 1) : "";
  const parts = [];
  for (const seg of (dir + rel).split("/")) {
    if (seg === "..") parts.pop();
    else if (seg !== "." && seg !== "") parts.push(seg);
  }
  return parts.join("/");
}

/** Where a section's data lives: {file, parts} (parts = pointer inside that file). */
export function sectionLocation(documents, main, section) {
  const doc = documents[main];
  if (!doc || typeof doc !== "object") return null;
  if (TOP_SECTIONS.includes(section)) {
    const inc = includeOf(doc[section]);
    return inc ? { file: resolvePath(main, inc), parts: [] } : { file: main, parts: [section] };
  }
  if (section === "modules/_order" || section.startsWith("modules/")) {
    const inc = includeOf(doc.modules);
    const where = inc ? { file: resolvePath(main, inc), parts: [] } : { file: main, parts: ["modules"] };
    if (section === "modules/_order") return where;
    const list = getAt(documents[where.file], where.parts);
    const id = section.slice("modules/".length);
    const index = Array.isArray(list) ? list.findIndex((m) => instanceId(m) === id) : -1;
    return { ...where, list: where.parts, index, parts: index >= 0 ? [...where.parts, index] : null };
  }
  return null;
}

export function instanceId(item) {
  if (!item || typeof item !== "object") return null;
  return typeof item.id === "string" ? item.id : typeof item.module === "string" ? item.module : null;
}

/** The section values of CORE-216 in a set of documents: {section: value}. */
export function sectionValues(documents, main) {
  const out = {};
  const doc = documents[main];
  if (!doc || typeof doc !== "object") return out;
  for (const s of TOP_SECTIONS) {
    const loc = sectionLocation(documents, main, s);
    const v = loc ? getAt(documents[loc.file], loc.parts) : undefined;
    if (v !== undefined) out[s] = v;
  }
  const where = sectionLocation(documents, main, "modules/_order");
  const list = where ? getAt(documents[where.file], where.parts) : undefined;
  if (Array.isArray(list)) {
    out["modules/_order"] = list.map(instanceId);
    for (const item of list) {
      const id = instanceId(item);
      if (id) out[`modules/${id}`] = item;
    }
  }
  return out;
}

/** The smallest JSON Patch from `cur` to `old` below `parts`, so comments elsewhere survive. */
export function diffOps(parts, cur, old) {
  if (equal(cur, old)) return [];
  const plain = (v) => v && typeof v === "object" && !Array.isArray(v) && !isTag(v);
  if (plain(cur) && plain(old)) {
    const ops = [];
    for (const key of Object.keys(cur)) {
      if (!(key in old)) ops.push({ op: "remove", path: pointer([...parts, key]) });
    }
    for (const [key, value] of Object.entries(old)) {
      if (!(key in cur)) ops.push({ op: "add", path: pointer([...parts, key]), value });
      else ops.push(...diffOps([...parts, key], cur[key], value));
    }
    return ops;
  }
  if (Array.isArray(cur) && Array.isArray(old) && cur.length === old.length) {
    return old.flatMap((value, i) => diffOps([...parts, i], cur[i], value));
  }
  return [{ op: "replace", path: pointer(parts), value: old }];
}

export function changedSections(before, after, main) {
  const a = sectionValues(before, main);
  const b = sectionValues(after, main);
  const names = new Set([...Object.keys(a), ...Object.keys(b)]);
  return [...names].filter((n) => !equal(a[n], b[n]));
}

// --- the draft -----------------------------------------------------------------------

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

export class Draft extends EventTarget {
  constructor() {
    super();
    this.base = null; // ETag the draft started from
    this.version = null;
    this.main = "config.yaml";
    this.files = new Map(); // path -> {role, saved, text}
    this.documents = {};
    this.records = [];
    this.layout = null; // the draft's layout object (CORE-274, CORE-301)
    this.baseGrid = null; // the saved configuration's grid, for Rescale (CORE-328)
    this.undoStack = [];
    this.redoStack = [];
    this.pending = new Map(); // YAML typing not yet a step: path -> {before, timer}
    this.busy = false;
  }

  changed() {
    this.dispatchEvent(new Event("change"));
  }

  get dirty() {
    for (const f of this.files.values()) if (f.text !== f.saved) return true;
    return false;
  }

  /** Load the config set; keep a stored draft made from the same ETag. */
  async load() {
    const config = await call("GET", "/config");
    this.base = config.etag;
    this.version = config.version;
    this.files = new Map();
    for (const f of config.files) {
      if (f.role === "main") this.main = f.path;
      this.files.set(f.path, { role: f.role, saved: f.content, text: f.content });
    }
    const stored = this.restore();
    const keep = stored && stored.base === this.base;
    if (keep) {
      // The saved grid first, then the stored draft on top of it.
      await this.refresh([]);
      this.baseGrid = this.layout ? { ...this.layout.grid } : null;
    }
    if (keep) {
      for (const [path, text] of Object.entries(stored.texts)) {
        const f = this.files.get(path);
        if (f) f.text = text;
        else this.files.set(path, { role: "include", saved: "", text });
      }
    }
    this.undoStack = [];
    this.redoStack = [];
    await this.refresh([...this.changedTexts().keys()]);
    if (!keep) this.baseGrid = this.layout ? { ...this.layout.grid } : null;
  }

  restore() {
    try {
      return JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    } catch {
      return null;
    }
  }

  persist() {
    try {
      const texts = Object.fromEntries(this.changedTexts());
      if (Object.keys(texts).length) localStorage.setItem(STORE_KEY, JSON.stringify({ base: this.base, texts }));
      else localStorage.removeItem(STORE_KEY);
    } catch {
      // the draft stays in memory only
    }
  }

  changedTexts() {
    const out = new Map();
    for (const [path, f] of this.files) if (f.text !== f.saved) out.set(path, f.text);
    return out;
  }

  texts() {
    const out = {};
    for (const [path, f] of this.files) out[path] = f.text;
    return out;
  }

  draftFiles(paths = null) {
    const list = paths ?? [...this.changedTexts().keys()];
    return list.map((path) => ({ path, content: this.files.get(path).text }));
  }

  /**
   * Send the draft (and ops) to POST /config/patch; take files, records, documents.
   *
   * Text typed while the request is out is kept: a file without ops whose text
   * changed since it was sent keeps the newer text, and its own typing step
   * refreshes it again (CORE-151).
   */
  async refresh(paths = [], ops = []) {
    const files = this.draftFiles(paths);
    const sent = new Map(files.map((f) => [f.path, f.content]));
    const patched = new Set(ops.map((o) => o.file));
    const out = await call("POST", "/config/patch", { body: { base: this.base, files, ops } });
    for (const f of out.files) {
      const entry = this.files.get(f.path);
      if (!entry) this.files.set(f.path, { role: f.role, saved: "", text: f.content });
      else if (patched.has(f.path) || !sent.has(f.path) || entry.text === sent.get(f.path)) {
        entry.text = f.content;
      }
    }
    this.records = out.records;
    this.documents = out.documents;
    this.layout = out.layout ?? null;
    this.persist();
    this.changed();
    return out;
  }

  // --- steps -------------------------------------------------------------------------

  push(step) {
    this.undoStack.push(step);
    if (this.undoStack.length > MAX_STEPS) this.undoStack.shift();
    this.redoStack = [];
  }

  /** A Form or Layout edit: JSON Patch ops on one file, as one labelled step. */
  apply(file, patch, label) {
    return this.applyOps([{ file, patch }], label);
  }

  /** JSON Patch ops on several files, as one labelled step (Rescale, CORE-328). */
  async applyOps(ops, label) {
    await this.flushTyping();
    const before = this.texts();
    const docsBefore = clone(this.documents);
    await this.refresh([...this.changedTexts().keys()], ops);
    this.push({
      label,
      forward: ops,
      before,
      after: this.texts(),
      sections: changedSections(docsBefore, this.documents, this.main),
      docsBefore,
      docsAfter: clone(this.documents),
    });
    this.changed();
  }

  /** A YAML keystroke batch; grouped into one step per pause of 1 s or view switch. */
  typed(path, text) {
    const f = this.files.get(path);
    if (!f || f.text === text) return;
    let p = this.pending.get(path);
    if (!p) {
      p = { before: this.texts(), docsBefore: clone(this.documents), timer: null };
      this.pending.set(path, p);
    }
    f.text = text;
    clearTimeout(p.timer);
    p.timer = setTimeout(() => this.flushTyping(path), 1000);
    this.persist();
  }

  async flushTyping(only = null) {
    for (const [path, p] of [...this.pending]) {
      if (only && path !== only) continue;
      clearTimeout(p.timer);
      this.pending.delete(path);
      try {
        await this.refresh([...this.changedTexts().keys()]);
      } catch (err) {
        this.dispatchEvent(new CustomEvent("problem", { detail: err }));
      }
      this.push({
        label: `${path}: YAML`,
        forward: [{ file: path, text: this.files.get(path).text }],
        before: p.before,
        after: this.texts(),
        sections: changedSections(p.docsBefore, this.documents, this.main),
        docsBefore: p.docsBefore,
        docsAfter: clone(this.documents),
      });
    }
    this.changed();
  }

  async setTexts(texts) {
    const paths = [];
    for (const [path, text] of Object.entries(texts)) {
      const f = this.files.get(path);
      if (f && f.text !== text) {
        f.text = text;
        paths.push(path);
      }
    }
    await this.refresh([...new Set([...paths, ...this.changedTexts().keys()])]);
  }

  async undo() {
    await this.flushTyping();
    const step = this.undoStack.pop();
    if (!step) return null;
    await this.setTexts(step.before);
    this.redoStack.push(step);
    this.changed();
    return step;
  }

  async redo() {
    await this.flushTyping();
    const step = this.redoStack.pop();
    if (!step) return null;
    await this.setTexts(step.after);
    this.undoStack.push(step);
    this.changed();
    return step;
  }

  lastStepFor(section) {
    for (let i = this.undoStack.length - 1; i >= 0; i -= 1) {
      if (this.undoStack[i].sections.includes(section)) return this.undoStack[i];
    }
    return null;
  }

  /** CORE-152: revert only the latest step touching a section, as a new step. */
  async undoSection(section, labelFor) {
    await this.flushTyping();
    const step = this.lastStepFor(section);
    if (!step) return null;
    const old = sectionValues(step.docsBefore, this.main)[section];
    const loc = sectionLocation(this.documents, this.main, section);
    if (!loc) return null;
    let patch;
    if (section.startsWith("modules/") && section !== "modules/_order") {
      const listPtr = pointer(loc.list);
      if (old === undefined) patch = loc.index >= 0 ? [{ op: "remove", path: pointer(loc.parts) }] : [];
      else if (loc.index >= 0) patch = diffOps(loc.parts, getAt(this.documents[loc.file], loc.parts), old);
      else patch = [{ op: "add", path: `${listPtr}/-`, value: old }];
    } else if (section === "modules/_order") {
      const list = getAt(this.documents[loc.file], loc.parts) || [];
      const rank = (item) => {
        const i = (old || []).indexOf(instanceId(item));
        return i < 0 ? Number.MAX_SAFE_INTEGER : i;
      };
      const sorted = [...list].sort((a, b) => rank(a) - rank(b));
      patch = [{ op: "replace", path: pointer(loc.parts), value: sorted }];
    } else if (old === undefined) {
      patch = loc.parts.length ? [{ op: "remove", path: pointer(loc.parts) }] : [];
    } else {
      const cur = getAt(this.documents[loc.file], loc.parts);
      patch = cur === undefined ? [{ op: "add", path: pointer(loc.parts), value: old }] : diffOps(loc.parts, cur, old);
    }
    if (!patch.length) return null;
    await this.apply(loc.file, patch, labelFor(step, section));
    return step;
  }

  // --- validate and save ---------------------------------------------------------------

  async validate() {
    await this.flushTyping();
    return call("POST", "/config/validate", { body: { base: this.base, files: this.draftFiles() } });
  }

  async save() {
    await this.flushTyping();
    const out = await call("PUT", "/config", {
      body: { base: this.base, files: this.draftFiles() },
      etag: this.base,
    });
    try {
      localStorage.removeItem(STORE_KEY);
    } catch {
      // nothing stored
    }
    await this.load();
    return out;
  }

  async discard() {
    try {
      localStorage.removeItem(STORE_KEY);
    } catch {
      // nothing stored
    }
    await this.load();
  }

  /** Open in editor (CORE-324): a version's files become the draft, as one step. */
  async openVersion(version, label) {
    await this.flushTyping();
    const data = await call("GET", `/config/history/${version}`);
    const before = this.texts();
    const docsBefore = clone(this.documents);
    const texts = {};
    for (const f of data.files) {
      texts[f.path] = f.content;
      if (!this.files.has(f.path)) this.files.set(f.path, { role: f.role, saved: "", text: "" });
    }
    await this.setTexts(texts);
    this.push({
      label,
      forward: [],
      before,
      after: this.texts(),
      sections: changedSections(docsBefore, this.documents, this.main),
      docsBefore,
      docsAfter: clone(this.documents),
    });
    this.changed();
  }
}
