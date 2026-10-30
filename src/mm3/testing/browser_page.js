// Stand-alone shell of the browser harness (Module Contract 12.2): hosts one module
// element the way the real shell does, feeds it states and reports what it did.
// Implements MOD-32 (Module Contract 1.0 rev 11, §5.3 Rules)
import { bind } from "/core/mm3.js";

const report = { net: [], csp: [], errors: [], logs: [], sent: [], media: [] };
const nativeFetch = window.fetch.bind(window);

const refuse = (api, url) => {
  report.net.push({ api, url: String(url) });
};
window.fetch = (input) => {
  refuse("fetch", input && input.url ? input.url : input);
  return Promise.reject(new TypeError("network access is not allowed in a module element"));
};
const xhrOpen = XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open = function (method, url) {
  refuse("XMLHttpRequest", url);
  return xhrOpen.call(this, method, "data:,");
};
for (const name of ["WebSocket", "EventSource"]) {
  window[name] = class {
    constructor(url) {
      refuse(name, url);
      throw new TypeError("network access is not allowed in a module element");
    }
  };
}
if (navigator.sendBeacon) {
  navigator.sendBeacon = (url) => {
    refuse("sendBeacon", url);
    return false;
  };
}
if (navigator.mediaDevices) {
  navigator.mediaDevices.getUserMedia = (c) => {
    report.media.push(JSON.stringify(c || {}));
    return Promise.reject(new DOMException("not allowed", "NotAllowedError"));
  };
}
document.addEventListener("securitypolicyviolation", (e) => {
  report.csp.push({ directive: e.violatedDirective, blocked: e.blockedURI, source: e.sourceFile || "" });
});
window.addEventListener("error", (e) => report.errors.push(String(e.message)));
window.addEventListener("unhandledrejection", (e) => report.errors.push(String((e.reason && e.reason.message) || e.reason)));

let current = null;

const host = {
  send(kind, data) {
    if (kind === "log") report.logs.push({ ...data, seq: current ? current.states : 0 });
    else report.sent.push({ kind, data });
  },
  request: () => Promise.reject(new Error("no backend in the browser harness")),
  failed(_id, failed) {
    if (current) current.failed = failed;
  },
  rendered() {},
};

function variablesSheet(scale) {
  const css = new CSSStyleSheet();
  css.replaceSync(`:host { display: block; --mm3-scale: ${scale}; font-size: var(--mm3-font-size); }`);
  return css;
}

async function sheet(url) {
  try {
    const res = await nativeFetch(url);
    if (!res.ok) return null;
    const css = new CSSStyleSheet();
    css.replaceSync(await res.text());
    return css;
  } catch {
    return null;
  }
}

function normalise(root) {
  return root.innerHTML.replace(/<!--[\s\S]*?-->/g, "").replace(/\s+/g, " ").trim();
}

window.mm3test = {
  report,

  async mount(info) {
    const grid = document.getElementById("mm3-grid");
    grid.replaceChildren();
    const node = document.createElement("div");
    node.className = "mm3-cell";
    node.dataset.instance = info.id;
    const area = document.createElement("div");
    area.className = "mm3-area";
    area.dataset.overflow = "hidden";
    const fitBox = document.createElement("div");
    fitBox.className = "mm3-fit";
    area.append(fitBox);
    node.append(area);
    grid.append(node);
    current = { node, area, el: null, failed: false, states: 0, resizes: [] };
    const failures = [];
    for (const url of info.scripts) {
      try {
        await import(url);
      } catch (err) {
        failures.push(`${url}: ${err && err.message ? err.message : err}`);
      }
    }
    if (!customElements.get(info.element)) {
      return { ok: false, failures: [...failures, `${info.element} is not defined by the scripts`] };
    }
    const el = document.createElement(info.element);
    current.el = el;
    el[bind](host, {
      id: info.id,
      config: info.config,
      locale: info.locale,
      preview: info.preview,
      page: "",
      timezone: info.timezone,
      location: info.location,
      engineLocale: info.locale,
      catalog: info.catalog,
    });
    fitBox.append(el);
    const sheets = [variablesSheet(1)];
    for (const url of info.styles) {
      const s = await sheet(url);
      if (s) sheets.push(s);
    }
    el.shadow.adoptedStyleSheets = sheets;
    new ResizeObserver(() => {
      if (current && current.el === el) el.onResize(Math.round(area.clientWidth), Math.round(area.clientHeight));
    }).observe(area);
    return { ok: failures.length === 0, failures };
  },

  /** Render a state; returns the best of `repeat` render times and the rendered markup. */
  setState(state, repeat = 3) {
    const el = current.el;
    current.states += 1;
    current.failed = false;
    let error = null;
    let best = Infinity;
    try {
      for (let i = 0; i < repeat; i++) {
        const t0 = performance.now();
        el._setState(state);
        best = Math.min(best, performance.now() - t0);
      }
    } catch (err) {
      error = String(err && err.message ? err.message : err);
    }
    return { ms: best === Infinity ? null : best, failed: current.failed, error, html: normalise(el.shadow) };
  },

  /** Render a state with a copy, to compare two renders of the same state (MOD-34). */
  html() {
    return normalise(current.el.shadow);
  },

  async resize(width, height) {
    current.node.style.width = `${width}px`;
    current.node.style.height = `${height}px`;
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  },

  /** Content outside the cell (overflow hidden) and page-level scroll (MOD-51). */
  overflow() {
    const box = current.area.getBoundingClientRect();
    const out = [];
    const nodes = current.el.shadow.querySelectorAll("*");
    for (const n of nodes) {
      const r = n.getBoundingClientRect();
      if (!r.width && !r.height) continue;
      if (r.right > box.right + 1 || r.bottom > box.bottom + 1 || r.left < box.left - 1 || r.top < box.top - 1) {
        out.push(`${n.tagName.toLowerCase()}${n.className ? "." + String(n.className).split(" ")[0] : ""}`);
        if (out.length >= 3) break;
      }
    }
    const de = document.documentElement;
    return {
      content: out,
      page: de.scrollWidth > window.innerWidth || de.scrollHeight > window.innerHeight,
      cell: [Math.round(box.width), Math.round(box.height)],
    };
  },

  setLocale(locale, catalog) {
    const el = current.el;
    el.locale = locale;
    el.catalog = catalog;
    el.onLocaleChange(locale);
  },

  mediaInPreview() {
    return current.el.shadow.querySelectorAll("video, audio").length;
  },
};
