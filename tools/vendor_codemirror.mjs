#!/usr/bin/env node
// Build the vendored CodeMirror 6 bundle of the admin YAML view.
//
// Implements CORE-323 (Core spec 0.26, §6.4 Web configuration editor): CodeMirror 6
// is one ES-module bundle, built once with esbuild from pinned npm packages; the
// bundle and vendor.json (packages, versions, licenses, hashes) are committed.
// esbuild is used by this script only; it is never a dependency of mm3-core.
//
// Usage: node tools/vendor_codemirror.mjs   (needs npm and network access)
// Output: src/mm3/core/web/adminui/vendor/codemirror/{codemirror.js,LICENSE}
//         src/mm3/core/web/adminui/vendor/vendor.json

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGES = {
  "@codemirror/state": "6.7.6",
  "@codemirror/view": "6.43.15",
  "@codemirror/commands": "6.11.1",
  "@codemirror/language": "6.13.1",
  "@codemirror/lang-yaml": "6.1.3",
  "@codemirror/lint": "6.9.7",
  "@codemirror/autocomplete": "6.20.4",
  "@codemirror/search": "6.7.2",
};
const ESBUILD = "0.28.2";

// The names the admin page imports from /admin/static/codemirror.js.
const ENTRY = `
export { EditorState, StateEffect, StateField, Compartment } from "@codemirror/state";
export {
  EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter,
  drawSelection, highlightSpecialChars, ViewPlugin,
} from "@codemirror/view";
export { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
export {
  syntaxTree, indentOnInput, bracketMatching, foldGutter, foldKeymap,
  syntaxHighlighting, defaultHighlightStyle,
} from "@codemirror/language";
export { yaml } from "@codemirror/lang-yaml";
export { linter, lintGutter, setDiagnostics, forceLinting } from "@codemirror/lint";
export { autocompletion, completionKeymap } from "@codemirror/autocomplete";
export { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
`;

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = join(root, "src/mm3/core/web/adminui/vendor");
const work = mkdtempSync(join(tmpdir(), "mm3-codemirror-"));
try {
  writeFileSync(
    join(work, "package.json"),
    JSON.stringify({ private: true, dependencies: { ...PACKAGES, esbuild: ESBUILD } }, null, 2),
  );
  execFileSync("npm", ["install", "--no-audit", "--no-fund", "--ignore-scripts=false"], {
    cwd: work,
    stdio: "inherit",
  });
  writeFileSync(join(work, "entry.js"), ENTRY);
  mkdirSync(join(outDir, "codemirror"), { recursive: true });
  const bundle = join(outDir, "codemirror", "codemirror.js");
  execFileSync(
    join(work, "node_modules", ".bin", "esbuild"),
    ["entry.js", "--bundle", "--format=esm", "--minify", "--legal-comments=none", `--outfile=${bundle}`],
    { cwd: work, stdio: "inherit" },
  );
  const licenses = Object.keys(PACKAGES)
    .map((name) => {
      const text = readFileSync(join(work, "node_modules", name, "LICENSE"), "utf8");
      return `${name}@${PACKAGES[name]}\n\n${text.trim()}\n`;
    })
    .join("\n----------------------------------------------------------------------\n\n");
  writeFileSync(join(outDir, "codemirror", "LICENSE"), licenses);
  const sha = (p) => createHash("sha256").update(readFileSync(p)).digest("hex");
  const manifest = {
    vendored: [
      {
        name: "codemirror",
        version: Object.entries(PACKAGES).map(([n, v]) => `${n}@${v}`).join(", "),
        license: "MIT",
        source: "https://registry.npmjs.org/ (bundled with esbuild " + ESBUILD + " by tools/vendor_codemirror.mjs)",
        files: {
          "codemirror/LICENSE": sha(join(outDir, "codemirror", "LICENSE")),
          "codemirror/codemirror.js": sha(bundle),
        },
      },
    ],
  };
  writeFileSync(join(outDir, "vendor.json"), JSON.stringify(manifest, null, 2) + "\n");
  console.log(`wrote ${bundle}`);
} finally {
  rmSync(work, { recursive: true, force: true });
}
