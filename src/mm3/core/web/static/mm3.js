// The module-facing API of the front-end contract (Module Contract section 5):
// `html`, `svg`, `nothing` and `MirrorElement`. lit-html is an engine detail
// behind this file (MOD-37).
// Implements CORE-64 (Core spec 0.14, §10.1 Security)
// Implements CORE-95, CORE-255, CORE-259 (Core spec 0.14, §6.5 Screen layout)
// Implements CORE-276 (Core spec 0.14, §5.6 WebSocket protocol)
// Implements CORE-265, CORE-266 (Core spec 0.15, §8.5 Catalogs at run time)
import { html, svg, nothing, render as litRender } from "/core/vendor/lit-html/lit-html.js";

export { html, svg, nothing };

const BIND = Symbol("mm3.bind");
export const bind = BIND;

/**
 * `this.location` (CORE-288): `{lat, lon, name}` or null. The name is resolved along
 * the locale chain of core spec 8.2: the element's locale, the engine locale, each
 * followed by its base language, then `en`; otherwise the first entry.
 */
function placeOf(place, locale, engineLocale) {
  if (!place) return null;
  let name = place.name ?? null;
  if (name !== null && typeof name === "object") {
    const byTag = new Map(Object.entries(name).map(([k, v]) => [k.toLowerCase(), v]));
    const chain = [];
    for (const tag of [locale, engineLocale, "en"]) {
      if (!tag) continue;
      const lower = String(tag).toLowerCase();
      for (const t of [lower, lower.split("-")[0]]) if (!chain.includes(t)) chain.push(t);
    }
    const hit = chain.find((t) => byTag.has(t));
    name = hit !== undefined ? byTag.get(hit) : (Object.values(name)[0] ?? null);
  }
  return { lat: place.lat, lon: place.lon, name };
}

/** Base class of every module element. The shell binds it to its instance. */
export class MirrorElement extends HTMLElement {
  constructor() {
    super();
    this.shadow = this.attachShadow({ mode: "open" });
    this.state = {};
    this.config = {};
    this.instanceId = "";
    this.locale = "en";
    this.preview = false;
    this.page = "";
    this._timers = new Set();
    this._host = null;
    this._rendered = false;
    this._failed = false;
    this._pending = false;
    this._suspended = false;
    this.log = {
      debug: (m, f) => this._log("debug", m, f),
      info: (m, f) => this._log("info", m, f),
      warn: (m, f) => this._log("warn", m, f),
      error: (m, f) => this._log("error", m, f),
    };
  }

  /** Called by the shell once, before the element is connected. */
  [BIND](host, info) {
    this._host = host;
    this.instanceId = info.id;
    this.config = info.config;
    this.locale = info.locale;
    this.preview = info.preview;
    this.page = info.page;
    this.timezone = info.timezone;
    this.location = placeOf(info.location, info.locale, info.engineLocale);
    this.catalog = info.catalog || null;
  }

  disconnectedCallback() {
    for (const t of this._timers) {
      clearInterval(t);
      clearTimeout(t);
    }
    this._timers.clear();
  }

  // --- provided to modules ---------------------------------------------------------

  t(key, vars = {}) {
    let entry = this.catalog ? this.catalog[key] : undefined;
    if (entry === undefined) {
      this._once(`key:${key}`, `catalog key ${key} is missing`);
      return key;
    }
    if (typeof entry === "object") {
      const count = vars.count;
      const category = typeof count === "number" ? new Intl.PluralRules(this.locale).select(count) : "other";
      entry = entry[category] ?? entry.other ?? Object.values(entry)[0] ?? key;
    }
    return String(entry).replace(/\{(\w+)\}/g, (m, name) => {
      if (name in vars) return String(vars[name]);
      this._once(`var:${key}:${name}`, `catalog key ${key} needs {${name}}`);
      return m;
    });
  }

  fmtDate(date, style = "medium") {
    const d = date instanceof Date ? date : new Date(date);
    let options;
    if (typeof style === "object" && style) options = style;
    else if (style === "time") options = { timeStyle: "short" };
    else options = { dateStyle: style };
    return new Intl.DateTimeFormat(this.locale, { timeZone: this.timezone, ...options }).format(d);
  }

  fmtNumber(n, unit) {
    const options = unit ? { style: "unit", unit, unitDisplay: "short" } : {};
    try {
      return new Intl.NumberFormat(this.locale, options).format(n);
    } catch {
      return new Intl.NumberFormat(this.locale).format(n);
    }
  }

  send(type, payload = {}) {
    this._host?.send("client", { type, payload }, this.instanceId);
  }

  notify(name, payload = {}) {
    this._host?.send("notify", { name, payload }, this.instanceId);
  }

  request(name, payload = {}, { target = null, timeout = 5 } = {}) {
    return this._host.request(this.instanceId, name, payload, target, timeout);
  }

  hide({ fade } = {}) {
    this._host?.send("visibility", { hidden: true, ...(fade === undefined ? {} : { fade }) }, this.instanceId);
  }

  show({ fade } = {}) {
    this._host?.send("visibility", { hidden: false, ...(fade === undefined ? {} : { fade }) }, this.instanceId);
  }

  setInterval(fn, ms) {
    const t = setInterval(() => this._guard("setInterval", fn), ms);
    this._timers.add(t);
    return t;
  }

  setTimeout(fn, ms) {
    const t = setTimeout(() => {
      this._timers.delete(t);
      this._guard("setTimeout", fn);
    }, ms);
    this._timers.add(t);
    return t;
  }

  requestRender() {
    if (this._pending) return;
    this._pending = true;
    queueMicrotask(() => {
      this._pending = false;
      this._renderNow();
    });
  }

  // --- callbacks (defaults) --------------------------------------------------------

  render() {
    return nothing;
  }
  onMessage() {}
  onNotification() {}
  onResize() {}
  onSuspend() {}
  onResume() {}
  onLocaleChange() {
    this.requestRender();
  }
  onPageChange() {}

  // --- used by the shell -----------------------------------------------------------

  _once(id, message) {
    if (!this._warned) this._warned = new Set();
    if (this._warned.has(id)) return;
    this._warned.add(id);
    this._log("warn", message, {});
  }

  _log(level, message, fields) {
    this._host?.send("log", { level, message: String(message), fields: fields || {} }, this.instanceId);
  }

  _guard(where, fn, ...args) {
    try {
      return fn.apply(this, args);
    } catch (err) {
      this._fail(where, err);
    }
    return undefined;
  }

  _fail(where, err) {
    this._failed = true;
    this._host?.failed(this.instanceId, true);
    this._log("error", `${where}: ${err && err.message ? err.message : err}`, {});
  }

  _renderNow() {
    try {
      litRender(this.render(this.state), this.shadow);
      this._rendered = true;
      if (this._failed) {
        this._failed = false;
        this._host?.failed(this.instanceId, false);
      }
      this._host?.rendered(this.instanceId);
    } catch (err) {
      this._fail("render", err);
    }
  }

  _setState(state) {
    this.state = state;
    this._renderNow();
  }
}
