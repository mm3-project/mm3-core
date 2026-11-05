// The admin page of a module, /admin/ext/<module>: loads the module's admin scripts,
// then creates its admin element with the API helper and its texts.
// Implements CORE-329 (Core spec 0.29, §11.1 Engine additions): the page loads the
// module's admin_scripts from /admin/ext/<module>/<version>/ and creates one admin_element.
// Implements CORE-330 (Core spec 0.29, §11.1 Engine additions): the element gets api,
// module, instances, locale and t() before it is connected.
// Implements CORE-331 (Core spec 0.29, §11.1 Engine additions): the API helper.
// Implements CORE-102 (Core spec 0.1, §11.1 Engine additions): admin extensions.

import { loadTexts, t as coreText } from "/admin/static/admin.js";

/** A failed API call: the status, the problem code (CORE-298) and the body. */
export class ApiError extends Error {
  constructor(status, body) {
    const code = body && typeof body === "object" && typeof body.code === "string" ? body.code : null;
    super(code ? coreText(code) : `HTTP ${status}`);
    this.status = status;
    this.code = code;
    this.problem = body;
  }
}

/** The API helper rooted at `prefix` (CORE-331); paths are relative to it. */
export function apiHelper(prefix = "/api/v1/") {
  async function call(method, path, body) {
    const init = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const r = await fetch(prefix + String(path).replace(/^\/+/, ""), init);
    if (r.status === 401) window.location.assign("/admin/login");
    const type = r.headers.get("content-type") || "";
    let data = null;
    if (r.status !== 204) data = type.includes("json") ? await r.json() : await r.text();
    if (!r.ok) throw new ApiError(r.status, data);
    return data;
  }
  return Object.freeze({
    get: (path) => call("GET", path),
    post: (path, body) => call("POST", path, body),
    put: (path, body) => call("PUT", path, body),
    patch: (path, body) => call("PATCH", path, body),
    delete: (path) => call("DELETE", path),
    module: (id) => apiHelper(`/api/v1/modules/${encodeURIComponent(id)}/`),
  });
}

/** `t(key, params)` over a merged catalog, plurals by `count` (as MirrorElement.t). */
function translator(catalog, locale) {
  return (key, params = {}) => {
    let entry = catalog[key];
    if (entry === undefined) return key;
    if (entry && typeof entry === "object") {
      const n = params.count;
      const category = typeof n === "number" ? new Intl.PluralRules(locale).select(n) : "other";
      entry = entry[category] ?? entry.other ?? Object.values(entry)[0] ?? key;
    }
    return String(entry).replace(/\{(\w+)\}/g, (m, name) => (name in params ? String(params[name]) : m));
  };
}

class AdminExt extends HTMLElement {
  async connectedCallback() {
    if (this.started) return;
    this.started = true;
    const data = JSON.parse(this.dataset.ext || "{}");
    const texts = await loadTexts();
    const locale = (texts && texts.locale) || "en";
    try {
      const r = await fetch(`/admin/ext/${encodeURIComponent(data.module)}/i18n.json`, {
        credentials: "same-origin",
      });
      const catalog = r.ok ? await r.json() : {};
      for (const script of data.scripts || []) await import(data.base + script);
      if (!customElements.get(data.element)) throw new Error(`${data.element} is not defined`);
      const el = document.createElement(data.element);
      el.api = apiHelper();
      el.module = data.module;
      el.instances = data.instances || [];
      el.locale = locale;
      el.t = translator(catalog, locale);
      this.replaceChildren(el);
    } catch (err) {
      console.error(err);
      const p = document.createElement("p");
      p.className = "alert warn";
      p.textContent = coreText("CORE_ADMIN_EXT_FAILED", { module: data.module || "" });
      this.replaceChildren(p);
    }
  }
}

customElements.define("mm3-admin-ext", AdminExt);
