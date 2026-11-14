"""ENH-012: the shared pieces of the admin pages (Core spec 0.43, §9.1): banners and messages."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mm3.core.web.apps import ADMIN_STATIC_DIR

_STUBS = {
    "lit.mjs": (
        "export const html=(s,...v)=>({s,v});export const nothing=Symbol('nothing');"
        "export const render=()=>{};"
    ),
    "admin.mjs": "export const t=(k)=>k;",
}

_NODE_TEST = r"""
import assert from 'node:assert/strict';
const timers = []; let now = 0;
globalThis.setTimeout = (fn, ms) => { const h = {at: now + ms, fn}; timers.push(h); return h; };
globalThis.clearTimeout = (h) => { const i = timers.indexOf(h); if (i >= 0) timers.splice(i, 1); };
const advance = (ms) => { now += ms; for (const h of [...timers].filter((x) => x.at <= now)) {
  timers.splice(timers.indexOf(h), 1); h.fn(); } };
const { BANNER_ROLES, Messages, OK_SHOWN_MS } = await import(process.argv[2]);

// Four kinds: an error is announced, the others are status (CORE-387).
assert.deepEqual(BANNER_ROLES, {error: 'alert', warn: 'status', info: 'status', ok: 'status'});
assert.equal(OK_SHOWN_MS, 8000);

let changes = 0;
const m = new Messages(() => { changes += 1; });
// A done banner disappears after 8 s.
m.set('ok', 'Saved'); assert.equal(m.current.kind, 'ok');
advance(7999); assert.notEqual(m.current, null);
advance(2); assert.equal(m.current, null);
// Error and warning banners stay while their cause lasts.
m.set('error', 'Broken'); advance(60000); assert.equal(m.current.text, 'Broken');
m.set('warn', 'Conflict', {label: 'Reload', onClick() {}}); advance(60000);
assert.equal(m.current.kind, 'warn'); assert.equal(m.current.action.label, 'Reload');
// A new message replaces the old one and cancels its timer.
m.set('ok', 'One'); advance(4000); m.set('ok', 'Two'); advance(5000);
assert.equal(m.current.text, 'Two'); advance(4000); assert.equal(m.current, null);
m.set('info', 'Waiting'); m.clear(); assert.equal(m.current, null);
console.log('ok');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_core_387_banner_kinds_and_the_done_banner_clears_after_8_s(tmp_path: Path) -> None:
    source = (ADMIN_STATIC_DIR / "ui.js").read_text()
    source = source.replace('"/admin/static/lit-html.js"', '"./lit.mjs"')
    source = source.replace('"/admin/static/admin.js"', '"./admin.mjs"')
    assert "/admin/static/" not in source
    (tmp_path / "ui.mjs").write_text(source)
    for name, text in _STUBS.items():
        (tmp_path / name).write_text(text)
    (tmp_path / "check.mjs").write_text(_NODE_TEST)
    out = subprocess.run(  # noqa: S603
        [
            shutil.which("node") or "node",
            str(tmp_path / "check.mjs"),
            (tmp_path / "ui.mjs").as_uri(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip() == "ok"
