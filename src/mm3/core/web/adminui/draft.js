// The editing draft of the admin config page: files, data, records and one
// undo history shared by the Form and YAML views (and the Layout view).
// Implements CORE-301 (Core spec 0.28, §6.4 Web configuration editor): the draft
// lives in the browser; every edit goes through POST /config/patch, which returns
// the changed files, the records, the documents and the draft's layout object.
// Implements CORE-301 (Core spec 0.38, §6.4 Web configuration editor): setOp builds
// operations that apply to the document, creating missing parents (ISS-012).
// Implements CORE-151 (Core spec 0.10, §6.8 Configuration history, undo and redo):
// one undo/redo history for the draft, each step a JSON Patch with a readable
// label, at least 200 steps; YAML typing is grouped per pause of 1 s.
// Implements CORE-152 (Core spec 0.39, §6.8 Configuration history, undo and redo):
// section undo marks its step as a revert of the step it undoes; each press reverts the
// latest step touching the section that is neither a revert nor already reverted.
// Implements CORE-377 (Core spec 0.39, §6.8 Configuration history, undo and redo): the
// history starts at the loaded version, is kept in the browser with the draft (a reload
// keeps both), is cleared by Save, Discard or loading another version, up to 500 steps.
// Implements CORE-300 (Core spec 0.22, §6.4 Web configuration editor): save with If-Match.
// Implements CORE-302 (Core spec 0.39, §6.4 Web configuration editor): validate returns
// the diff, shown on request in "Review changes"; Save does not wait for it.

import { ApiError } from "/admin/static/admin.js";

export const MAX_STEPS = 500;
const STORE_KEY = "mm3.admin.draft";
export const TOP_SECTIONS = ["engine", "display", "audio", "layout", "pages", "backup"];

// --- compact history (CORE-377) ------------------------------------------------------
// A step keeps the texts it changed, before and after. In the browser store it keeps
// only one splice per file (the span between the common start and end), so 500 steps
// stay far below the storage quota.

/** The splice that turns `a` into `b`: {start, del, ins}. */
export function splice(a, b) {
  const m = Math.min(a.length, b.length);
  let start = 0;
  while (start < m && a[start] === b[start]) start += 1;
  let tail = 0;
  while (tail < m - start && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail += 1;
  return { start, del: a.slice(start, a.length - tail), ins: b.slice(start, b.length - tail) };
}

/** Apply (or, reversed, undo) a splice; null when the text is not the one it was made on. */
export function applySplice(text, sp, reverse = false) {
  const [del, ins] = reverse ? [sp.ins, sp.del] : [sp.del, sp.ins];
  if (text.slice(sp.start, sp.start + del.length) !== del) return null;
  return text.slice(0, sp.start) + ins + text.slice(sp.start + del.length);
}

/** `{path: text}` limited to the paths whose text differs between `before` and `after`. */
function changedTexts(before, after) {
  const b = {};
  const a = {};
  for (const path of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if ((before[path] ?? "") !== (after[path] ?? "")) {
      b[path] = before[path] ?? "";
      a[path] = after[path] ?? "";
    }
  }
  return { before: b, after: a };
}

function pick(values, names) {
  const out = {};
  for (const n of names) if (n in values) out[n] = values[n];
  return out;
}

const serializeStep = (s) => ({
  id: s.id,
  label: s.label,
  revertOf: s.revertOf,
  sections: s.sections,
  secBefore: s.secBefore,
  patches: Object.keys(s.before).map((path) => ({ path, ...splice(s.before[path], s.after[path]) })),
});

/**
 * The steps of a stored history, with their texts rebuilt from the draft's current
 * texts: undo steps walk back from them, redo steps forward. Null when a splice does
 * not fit (the stored history is then dropped, never half-used).
 */
export function rebuildSteps(stored, texts) {
  const undo = [];
  let state = { ...texts };
  for (let i = (stored.undo || []).length - 1; i >= 0; i -= 1) {
    const s = stored.undo[i];
    const before = {};
    const after = {};
    for (const p of s.patches) {
      const cur = state[p.path] ?? "";
      const prior = applySplice(cur, p, true);
      if (prior === null) return null;
      after[p.path] = cur;
      before[p.path] = prior;
      state[p.path] = prior;
    }
    undo.unshift({ ...s, before, after });
  }
  const redo = [];
  state = { ...texts };
  for (let i = (stored.redo || []).length - 1; i >= 0; i -= 1) {
    const s = stored.redo[i];
    const before = {};
    const after = {};
    for (const p of s.patches) {
      const cur = state[p.path] ?? "";
      const next = applySplice(cur, p);
      if (next === null) return null;
      before[p.path] = cur;
      after[p.path] = next;
      state[p.path] = next;
    }
    redo.unshift({ ...s, before, after });
  }
  return { undo, redo };
}

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

const isContainer = (v) => v !== null && typeof v === "object" && !isTag(v);

/**
 * One JSON Patch op that sets `value` at `target` (parts) in `doc`, creating every
 * missing parent in the same op: an absent or null section such as `audio`, or a
 * list not yet written such as `pages.list` (ISS-012). A `-` part appends to a list.
 * Returns null when a parent is a scalar or a tagged node, which cannot hold a child.
 */
export function setOp(doc, target, value) {
  // The deepest existing container on the path; below it, nothing exists yet.
  let i = 0;
  while (i < target.length - 1 && target[i] !== "-" && isContainer(getAt(doc, target.slice(0, i + 1)))) i += 1;
  if (i < target.length - 1) {
    const at = target.slice(0, i + 1);
    const here = getAt(doc, at);
    if (here !== undefined && here !== null) return null; // a scalar or a tag in the way
    const tail = target.slice(i + 1);
    const built = tail.reduceRight((acc, key) => (key === "-" ? [acc] : { [key]: acc }), value);
    if (at[at.length - 1] === "-") return { op: "add", path: pointer(at), value: built };
    return { op: here === null ? "replace" : "add", path: pointer(at), value: built };
  }
  const last = target[target.length - 1];
  const parent = target.length > 1 ? getAt(doc, target.slice(0, -1)) : doc;
  if (target.length > 0 && !isContainer(parent)) return null;
  const exists = last !== "-" && getAt(doc, target) !== undefined;
  return { op: exists ? "replace" : "add", path: pointer(target), value };
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
    this.nextStepId = 1;
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
    // CORE-377: the history belongs to the draft and is kept with it; a draft made from
    // another version, or none, starts a new history.
    this.undoStack = [];
    this.redoStack = [];
    if (keep) this.restoreSteps(stored);
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

  restoreSteps(stored) {
    if (!stored || (!stored.undo?.length && !stored.redo?.length)) return;
    const rebuilt = rebuildSteps(stored, this.texts());
    if (!rebuilt) return;
    this.undoStack = rebuilt.undo.slice(-MAX_STEPS);
    this.redoStack = rebuilt.redo.slice(-MAX_STEPS);
    const ids = [...this.undoStack, ...this.redoStack].map((s) => s.id);
    this.nextStepId = Math.max(0, ...ids) + 1;
  }

  /** Keep the draft and its history in the browser (CORE-377); history is dropped before the draft. */
  persist() {
    try {
      const texts = Object.fromEntries(this.changedTexts());
      let undo = this.undoStack.map(serializeStep);
      let redo = this.redoStack.map(serializeStep);
      if (!Object.keys(texts).length && !undo.length && !redo.length) {
        localStorage.removeItem(STORE_KEY);
        return;
      }
      for (;;) {
        try {
          localStorage.setItem(STORE_KEY, JSON.stringify({ base: this.base, texts, undo, redo }));
          return;
        } catch (err) {
          // Over the quota: the oldest steps go first; the draft itself is never dropped here.
          if (!undo.length && !redo.length) throw err;
          undo = undo.slice(Math.ceil(undo.length / 2));
          redo = redo.slice(0, Math.floor(redo.length / 2));
        }
      }
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

  /**
   * One step from the state it started in: the texts it changed, the sections it touched
   * with their values before and after (CORE-216, CORE-152), and, for a section undo,
   * the step it reverts.
   */
  makeStep(label, before, docsBefore, revertOf = null) {
    const sections = changedSections(docsBefore, this.documents, this.main);
    return {
      id: this.nextStepId++,
      label,
      revertOf,
      sections,
      secBefore: clone(pick(sectionValues(docsBefore, this.main), sections)),
      ...changedTexts(before, this.texts()),
    };
  }

  push(step) {
    this.undoStack.push(step);
    if (this.undoStack.length > MAX_STEPS) this.undoStack.shift();
    this.redoStack = [];
    this.persist();
  }

  /** A Form or Layout edit: JSON Patch ops on one file, as one labelled step. */
  apply(file, patch, label, revertOf = null) {
    return this.applyOps([{ file, patch }], label, revertOf);
  }

  /** JSON Patch ops on several files, as one labelled step (Rescale, CORE-328). */
  async applyOps(ops, label, revertOf = null) {
    await this.flushTyping();
    const before = this.texts();
    const docsBefore = clone(this.documents);
    await this.refresh([...this.changedTexts().keys()], ops);
    this.push(this.makeStep(label, before, docsBefore, revertOf));
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
      this.push(this.makeStep(`${path}: YAML`, p.before, p.docsBefore));
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
    this.persist();
    this.changed();
    return step;
  }

  async redo() {
    await this.flushTyping();
    const step = this.redoStack.pop();
    if (!step) return null;
    await this.setTexts(step.after);
    this.undoStack.push(step);
    this.persist();
    this.changed();
    return step;
  }

  /**
   * CORE-152: the step "Undo last change here" would revert for a section: the latest step
   * touching it that is neither a revert nor already reverted. A revert that was undone
   * with CORE-151's undo is no longer in the stack, so its step can be reverted again.
   */
  lastStepFor(section) {
    const reverted = new Set(this.undoStack.map((s) => s.revertOf).filter((id) => id !== null));
    for (let i = this.undoStack.length - 1; i >= 0; i -= 1) {
      const step = this.undoStack[i];
      if (step.revertOf === null && !reverted.has(step.id) && step.sections.includes(section)) return step;
    }
    return null;
  }

  /** CORE-152: revert the step of `lastStepFor`, as a new step marked as its revert. */
  async undoSection(section, labelFor) {
    await this.flushTyping();
    const step = this.lastStepFor(section);
    if (!step) return null;
    const old = step.secBefore[section];
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
    // An empty patch still records the revert, so the next press goes one step further.
    await this.apply(loc.file, patch, labelFor(step, section), step.id);
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

  /**
   * Open in editor (CORE-324): a version's files become the draft. The draft history
   * starts again at that version (CORE-377), so there are no steps to undo.
   */
  async openVersion(version) {
    await this.flushTyping();
    const data = await call("GET", `/config/history/${version}`);
    const texts = {};
    for (const f of data.files) {
      texts[f.path] = f.content;
      if (!this.files.has(f.path)) this.files.set(f.path, { role: f.role, saved: "", text: "" });
    }
    this.undoStack = [];
    this.redoStack = [];
    await this.setTexts(texts);
    this.changed();
  }
}
