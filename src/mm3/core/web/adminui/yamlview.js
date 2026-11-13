// The YAML view of the admin config page: one CodeMirror editor per file of the set.
// Implements CORE-323 (Core spec 0.26, §6.4 Web configuration editor): syntax errors
// from the CodeMirror parser at once; the schema check is POST /config/validate
// 300 ms after the last keystroke, its records in the gutter; key and enum
// completion in-house from /config/schema.
// Implements CORE-301 (Core spec 0.28, §6.4 Web configuration editor): one tab per
// file, showing the true file content.
// Implements CORE-151 (Core spec 0.10, §6.8 Configuration history, undo and redo):
// Ctrl+Z, Ctrl+Shift+Z and Ctrl+Y go to the shared draft history.
// Implements CORE-35, CORE-206 (Core spec 0.38, §6.4 Web configuration editor):
// gutter messages are localized.

import {
  EditorState,
  EditorView,
  autocompletion,
  bracketMatching,
  defaultHighlightStyle,
  defaultKeymap,
  drawSelection,
  foldGutter,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  indentOnInput,
  indentWithTab,
  keymap,
  lineNumbers,
  lintGutter,
  linter,
  setDiagnostics,
  searchKeymap,
  syntaxHighlighting,
  syntaxTree,
  yaml,
} from "/admin/static/codemirror.js";
import { recordText } from "/admin/static/admin.js";

const LINT_DELAY_MS = 300;

/** Syntax errors from the parse tree, shown at once. */
function syntaxDiagnostics(view) {
  const out = [];
  syntaxTree(view.state).iterate({
    enter: (node) => {
      if (node.type.isError) {
        const from = node.from;
        const to = Math.max(node.to, Math.min(from + 1, view.state.doc.length));
        out.push({ from, to, severity: "error", message: "YAML syntax" });
      }
    },
  });
  return out;
}

/** The key path (from the document root) at a position, by indentation. */
export function keyPath(text, pos) {
  const lines = text.slice(0, pos).split("\n");
  const current = lines.pop();
  const indentOf = (l) => l.length - l.trimStart().length;
  let indent = indentOf(current);
  const path = [];
  if (current.trimStart().startsWith("- ")) path.unshift(0);
  for (let i = lines.length - 1; i >= 0 && indent > 0; i -= 1) {
    const line = lines[i];
    const body = line.trim();
    if (!body || body.startsWith("#")) continue;
    const ind = indentOf(line);
    if (ind >= indent) continue;
    const seq = body.startsWith("- ");
    const rest = seq ? body.slice(2) : body;
    const m = rest.match(/^([^:#'"]+):\s*(.*)$/);
    if (seq) {
      if (m && !m[2] && indent > ind + 2) path.unshift(m[1].trim());
      path.unshift(0);
    } else if (m) {
      path.unshift(m[1].trim());
    }
    indent = ind;
  }
  const valueMatch = current.trim().replace(/^- /, "").match(/^([A-Za-z0-9_-]+):\s*(\S*)$/);
  return { path, key: valueMatch ? valueMatch[1] : null, line: current };
}

export class YamlView {
  /**
   * @param {HTMLElement} host
   * @param {object} hooks {draft, schema(), onUndo, onRedo, recordsFor(path)}
   */
  constructor(host, hooks) {
    this.host = host;
    this.hooks = hooks;
    this.path = null;
    this.view = null;
    this.timer = null;
    this.records = [];
  }

  open(path) {
    const draft = this.hooks.draft;
    const file = draft.files.get(path);
    if (!file) return;
    this.path = path;
    if (this.view) this.view.destroy();
    const hooks = this.hooks;
    const self = this;
    const shared = keymap.of([
      { key: "Mod-z", run: () => (hooks.onUndo(), true), preventDefault: true },
      { key: "Mod-Shift-z", run: () => (hooks.onRedo(), true), preventDefault: true },
      { key: "Mod-y", run: () => (hooks.onRedo(), true), preventDefault: true },
    ]);
    // CORE-63: CodeMirror's styles must be constructable stylesheets, which it uses
    // only inside a shadow root; in the document it would insert <style> elements.
    if (!this.root) {
      this.root = this.host.shadowRoot || this.host.attachShadow({ mode: "open" });
    }
    this.view = new EditorView({
      parent: this.root,
      root: this.root,
      state: EditorState.create({
        doc: file.text,
        extensions: [
          shared,
          lineNumbers(),
          foldGutter(),
          highlightSpecialChars(),
          drawSelection(),
          indentOnInput(),
          bracketMatching(),
          highlightActiveLine(),
          highlightActiveLineGutter(),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          yaml(),
          lintGutter(),
          linter((view) => [...syntaxDiagnostics(view), ...self.recordDiagnostics(view)], { delay: 0 }),
          autocompletion({ override: [(ctx) => self.complete(ctx)] }),
          keymap.of([...defaultKeymap, ...searchKeymap, indentWithTab]),
          EditorView.updateListener.of((u) => {
            if (u.docChanged) self.typed(u.state.doc.toString());
          }),
          EditorView.lineWrapping,
          EditorView.theme({
            "&": { minHeight: "300px", maxHeight: "70vh", fontSize: "13px" },
            ".cm-scroller": { overflow: "auto", fontFamily: "ui-monospace, 'DejaVu Sans Mono', Menlo, monospace" },
          }),
        ],
      }),
    });
  }

  /** The text changed in the draft (undo, form edit): show it without a new step. */
  sync() {
    if (!this.view || !this.path) return;
    const file = this.hooks.draft.files.get(this.path);
    if (!file) return;
    const now = this.view.state.doc.toString();
    if (now !== file.text) {
      this.syncing = true;
      this.view.dispatch({ changes: { from: 0, to: now.length, insert: file.text } });
      this.syncing = false;
    }
    this.records = this.hooks.recordsFor(this.path);
    this.showDiagnostics();
  }

  /** Put the parser's and the server's diagnostics in the gutter now. */
  showDiagnostics() {
    if (!this.view) return;
    const diagnostics = [...syntaxDiagnostics(this.view), ...this.recordDiagnostics(this.view)];
    this.view.dispatch(setDiagnostics(this.view.state, diagnostics));
  }

  typed(text) {
    if (this.syncing) return;
    this.hooks.draft.typed(this.path, text);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.check(), LINT_DELAY_MS);
  }

  async check() {
    try {
      const out = await this.hooks.draft.validate();
      this.hooks.draft.records = out.records;
      this.records = this.hooks.recordsFor(this.path);
      this.showDiagnostics();
      this.hooks.onRecords?.(out.records);
    } catch (err) {
      this.hooks.onProblem?.(err);
    }
  }

  recordDiagnostics(view) {
    const doc = view.state.doc;
    return (this.records || [])
      .filter((r) => r.line)
      .map((r) => {
        const line = doc.line(Math.min(Math.max(r.line, 1), doc.lines));
        const from = Math.min(line.from + Math.max((r.col || 1) - 1, 0), line.to);
        return {
          from,
          to: line.to > from ? line.to : from,
          severity: r.severity === "error" ? "error" : "warning",
          message: recordText(r),
        };
      });
  }

  /** Keys and enum values from the schema at the cursor's key path (in-house completion). */
  complete(ctx) {
    const word = ctx.matchBefore(/[A-Za-z0-9_-]*/);
    if (!word || (word.from === word.to && !ctx.explicit)) return null;
    const schema = this.hooks.schema();
    if (!schema) return null;
    const text = ctx.state.doc.toString();
    const { path, key, line } = keyPath(text, ctx.pos);
    const prefix = this.hooks.pathPrefix ? this.hooks.pathPrefix(this.path) : [];
    let node = resolve(schema, schema, [...prefix, ...path]);
    if (!node) return null;
    if (key && line.trim().replace(/^- /, "").startsWith(`${key}:`)) {
      node = resolve(schema, node, [key]);
      const values = enumOf(schema, node);
      if (!values.length) return null;
      return { from: word.from, options: values.map((v) => ({ label: String(v), type: "enum" })) };
    }
    const props = propsOf(schema, node);
    return {
      from: word.from,
      options: Object.keys(props).map((k) => ({ label: k, type: "property", apply: `${k}: ` })),
    };
  }

  destroy() {
    if (this.view) this.view.destroy();
    this.view = null;
  }
}

function deref(schema, node) {
  let n = node;
  for (let i = 0; i < 20 && n && n.$ref; i += 1) n = schema.$defs[n.$ref.split("/").pop()];
  return n || {};
}

function branches(schema, node) {
  const n = deref(schema, node);
  const out = [];
  for (const b of n.anyOf || n.oneOf || []) out.push(...branches(schema, b));
  if (!n.anyOf && !n.oneOf) out.push(n);
  return out;
}

function propsOf(schema, node) {
  const props = {};
  for (const b of branches(schema, node)) Object.assign(props, b.properties || {});
  return props;
}

function enumOf(schema, node) {
  const out = [];
  for (const b of branches(schema, node)) {
    if (b.enum) out.push(...b.enum);
    if (b.const !== undefined) out.push(b.const);
    if (b.type === "boolean") out.push(true, false);
  }
  return out;
}

function resolve(schema, node, path) {
  let n = node;
  for (const part of path) {
    if (!n) return null;
    if (typeof part === "number") {
      const items = branches(schema, n).map((b) => b.items).find(Boolean);
      n = items || null;
    } else {
      n = propsOf(schema, n)[part] || null;
    }
  }
  return n;
}
