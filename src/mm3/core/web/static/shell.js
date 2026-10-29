// The shell: connects to the engine, builds the grid from `welcome`, creates one
// element per instance, and follows `state`, `client`, `notify`, `reply` and `control`.
// Implements CORE-250, CORE-275, CORE-276 (Core spec 0.14, §5.6 WebSocket protocol)
// Implements CORE-95, CORE-255, CORE-256, CORE-257, CORE-258, CORE-259 (Core spec 0.14, §6.5 Screen layout)
// Implements CORE-260 (Core spec 0.14, §6.9 Pages and visibility)
// Implements CORE-262 (Core spec 0.14, §6.10 Theme and custom CSS)
// Implements CORE-117 (Core spec 0.14, §10.5 Diagnostics and debugging)
// Implements CORE-103 (Core spec 0.14, §11.1 Engine additions)
// Implements CORE-235 (Core spec 0.15, §6.9 Pages and visibility)
// Implements CORE-264, CORE-267 (Core spec 0.15, §8.5 Catalogs at run time)
import { bind } from "/core/mm3.js";

const mode = document.documentElement.dataset.mode === "preview" ? "preview" : "mirror";
const WS_PATH = mode === "preview" ? "/admin/preview/ws" : "/ws";
const ROLE = mode === "preview" ? "preview" : "browser";
const FADE_MS = 300;
const BACKOFF = { first: 500, max: 10000, jitter: 0.2 };

const grid = document.getElementById("mm3-grid");
const cells = new Map(); // instance id -> Cell
let socket = null;
let welcome = null;
let current = null; // {page, visible: Set, transition}
let attempt = 0;
let build = null;
let customLink = null;
let nextRid = 1;
let inbox = Promise.resolve();
const pendingRequests = new Map();

function clientId() {
  try {
    let id = sessionStorage.getItem("mm3.client_id");
    if (!id) {
      id = crypto.randomUUID();
      sessionStorage.setItem("mm3.client_id", id);
    }
    return id;
  } catch {
    return crypto.randomUUID();
  }
}
const CLIENT_ID = clientId();

// --- transport ---------------------------------------------------------------------

const host = {
  send(kind, data, instance) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const frame = { v: 1, kind };
    if (instance) frame.instance = instance;
    frame.data = data;
    socket.send(JSON.stringify(frame));
  },
  request(instance, name, payload, target, timeout) {
    return new Promise((resolve, reject) => {
      const rid = String(nextRid++);
      const timer = setTimeout(() => {
        pendingRequests.delete(rid);
        reject(new Error("timeout"));
      }, (timeout + 1) * 1000);
      pendingRequests.set(rid, { resolve, reject, timer });
      const data = { rid, name, payload, timeout };
      if (target) data.target = target;
      this.send("request", data, instance);
    });
  },
  failed(instance, failed) {
    const cell = cells.get(instance);
    if (cell) cell.node.dataset.failed = failed ? "true" : "false";
  },
  rendered(instance) {
    const cell = cells.get(instance);
    if (cell) fit(cell);
  },
};

function viewport() {
  return [Math.round(window.innerWidth), Math.round(window.innerHeight)];
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}${WS_PATH}`);
  socket = ws;
  ws.addEventListener("open", () => {
    ws.send(
      JSON.stringify({
        v: 1,
        kind: "hello",
        data: {
          protocol: 1,
          client_id: CLIENT_ID,
          role: ROLE,
          viewport: viewport(),
          dpr: window.devicePixelRatio || 1,
        },
      }),
    );
  });
  ws.addEventListener("message", (ev) => {
    let frame;
    try {
      frame = JSON.parse(ev.data);
    } catch {
      return;
    }
    // Frames are handled in order; building from `welcome` loads scripts first.
    inbox = inbox.then(() => (socket === ws ? handle(frame) : undefined)).catch((err) => logError("shell", err));
  });
  ws.addEventListener("close", (ev) => {
    if (socket !== ws) return;
    for (const [rid, p] of pendingRequests) {
      clearTimeout(p.timer);
      p.reject(new Error("closed"));
      pendingRequests.delete(rid);
    }
    scheduleReconnect(ev.code);
  });
  ws.addEventListener("error", () => ws.close());
}

function scheduleReconnect() {
  const base = Math.min(BACKOFF.first * 2 ** attempt, BACKOFF.max);
  attempt += 1;
  const jitter = 1 + (Math.random() * 2 - 1) * BACKOFF.jitter;
  setTimeout(connect, base * jitter);
}

function logError(where, err) {
  host.send("log", {
    level: "error",
    message: `${where}: ${err && err.message ? err.message : err}`,
    stack: err && err.stack ? String(err.stack) : undefined,
    user_agent: navigator.userAgent,
  });
}
window.addEventListener("error", (ev) => logError("window", ev.error || ev.message));
window.addEventListener("unhandledrejection", (ev) => logError("promise", ev.reason));

// --- frames ------------------------------------------------------------------------

function handle(frame) {
  const { kind, instance, data } = frame;
  if (kind === "welcome") return onWelcome(data);
  if (kind === "state") return onState(instance, data);
  if (kind === "client") return cells.get(instance)?.el._guard("onMessage", cells.get(instance).el.onMessage, data.type, data.payload);
  if (kind === "notify") return onNotify(data);
  if (kind === "reply") return onReply(data);
  if (kind === "control") return onControl(instance, data);
  if (kind === "error") return console.warn("mm3 error", data);
  return undefined;
}

function onReply(data) {
  const p = pendingRequests.get(String(data.rid));
  if (!p) return;
  pendingRequests.delete(String(data.rid));
  clearTimeout(p.timer);
  if (data.error) p.reject(Object.assign(new Error(data.error.code), data.error));
  else p.resolve(data.payload);
}

function onNotify(data) {
  const n = data.notification;
  for (const id of data.instances) {
    const el = cells.get(id)?.el;
    if (el) el._guard("onNotification", el.onNotification, n.name, n.payload);
  }
}

function onState(instance, data) {
  const cell = cells.get(instance);
  if (!cell) return;
  cell.state = data.delta ? { ...cell.state, ...data.state } : data.state;
  if (cell.el) cell.el._setState(cell.state);
}

// --- welcome / build ---------------------------------------------------------------

async function onWelcome(data) {
  attempt = 0;
  if (build !== null && data.engine_build !== build) {
    location.reload();
    return;
  }
  build = data.engine_build;
  welcome = data;
  document.documentElement.lang = data.locale;
  // Pointer events reach elements only with engine.input.touch (CORE-103).
  document.documentElement.dataset.touch = data.touch ? "true" : "false";
  applyCustomCss(data.custom_css);
  applyLayout(data.layout);
  const wanted = new Set(data.instances.map((i) => i.id));
  for (const [id, cell] of cells) {
    const info = data.instances.find((i) => i.id === id);
    if (!info || cell.key !== keyOf(info)) removeCell(id);
  }
  for (const info of data.instances) {
    const old = cells.get(info.id);
    if (old) updateCell(old, info);
    else await addCell(info);
  }
  for (const id of [...cells.keys()]) if (!wanted.has(id)) removeCell(id);
  current = { page: data.page, visible: new Set(data.instances.map((i) => i.id)), transition: "none" };
  place();
}

const keyOf = (info) => `${info.module}|${info.element}|${info.scripts.join(",")}`;

function applyLayout(layout) {
  welcome.layout = layout;
  const g = layout.grid;
  grid.style.gridTemplateColumns = `repeat(${g.columns}, minmax(0, 1fr))`;
  grid.style.gridTemplateRows = `repeat(${g.rows}, minmax(0, 1fr))`;
  grid.style.gap = `${g.gap}px`;
  grid.style.padding = `${g.padding}px`;
  document.documentElement.style.setProperty("--mm3-gap", `${g.gap}px`);
}

async function fetchCatalog(info) {
  if (!info.catalog) return null;
  try {
    const res = await fetch(`/i18n/${encodeURIComponent(info.id)}.json?v=${encodeURIComponent(info.catalog)}`);
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

async function sheet(url) {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    const css = new CSSStyleSheet();
    css.replaceSync(await res.text());
    return css;
  } catch {
    return null;
  }
}

function variablesSheet(appearance) {
  const css = new CSSStyleSheet();
  css.replaceSync(
    `:host { display: block; --mm3-scale: ${Number(appearance.scale) || 1}; font-size: var(--mm3-font-size); }`,
  );
  return css;
}

async function addCell(info) {
  const node = document.createElement("div");
  node.className = "mm3-cell";
  node.dataset.instance = info.id;
  node.dataset.status = info.status;
  node.dataset.gone = "true";
  node.dataset.hidden = "true";
  const header = document.createElement("div");
  header.className = "mm3-header";
  const area = document.createElement("div");
  area.className = "mm3-area";
  const fitBox = document.createElement("div");
  fitBox.className = "mm3-fit";
  const failure = document.createElement("div");
  failure.className = "mm3-failure";
  area.append(fitBox);
  node.append(header, area, failure);
  grid.append(node);
  const cell = { id: info.id, key: keyOf(info), node, header, area, fitBox, failure, el: null, info, state: {} };
  cells.set(info.id, cell);
  const [catalog] = await Promise.all([
    fetchCatalog(info),
    ...info.scripts.map((u) => import(u).catch((err) => logError(u, err))),
  ]);
  cell.catalog = catalog;
  failure.textContent = unavailableText(cell);
  if (!customElements.get(info.element)) {
    node.dataset.status = "unavailable";
    return;
  }
  const el = document.createElement(info.element);
  cell.el = el;
  el[bind](host, {
    id: info.id,
    config: info.config,
    locale: info.locale || welcome.locale,
    preview: mode === "preview",
    page: welcome.page,
    timezone: welcome.timezone,
    catalog,
  });
  fitBox.append(el);
  new ResizeObserver(() => scheduleResize(cell)).observe(area);
  await adoptStyles(cell, info);
  updateCell(cell, info);
  el._setState(cell.state); // `{}` until a snapshot arrives (MOD-36)
}

async function adoptStyles(cell, info) {
  const sheets = [variablesSheet(info.appearance)];
  for (const url of info.styles) {
    const s = await sheet(url);
    if (s) sheets.push(s);
  }
  if (info.appearance.css) {
    const s = await sheet(info.appearance.css);
    if (s) sheets.push(s);
  }
  cell.el.shadow.adoptedStyleSheets = sheets;
}

function updateCell(cell, info) {
  cell.info = info;
  const a = info.appearance;
  const n = cell.node;
  n.dataset.status = info.status;
  n.dataset.align = a.align || "";
  n.dataset.valign = a.valign || "";
  n.style.setProperty("--mm3-opacity", String(a.opacity));
  n.style.setProperty("--mm3-scale", String(a.scale));
  n.classList.add(...(a.classes || []));
  cell.area.dataset.overflow = a.overflow;
  cell.header.textContent = a.header || "";
  cell.header.style.display = a.header ? "" : "none";
  cell.failure.textContent = unavailableText(cell);
  cell.fitBox.style.removeProperty("transform");
}

function unavailableText(cell) {
  // The placeholder shows only localized text, never ids or errors (CORE-117).
  const text = cell.catalog && cell.catalog.CORE_SHELL_UNAVAILABLE;
  return typeof text === "string" ? text : "";
}

function removeCell(id) {
  const cell = cells.get(id);
  if (!cell) return;
  cell.node.remove();
  cells.delete(id);
}

// --- placement, visibility, pages ----------------------------------------------------

function place() {
  if (!welcome || !current) return;
  const placements = welcome.layout.pages[current.page] || [];
  const onPage = new Map(placements.map((p) => [p.instance, p]));
  const stacks = new Map();
  for (const p of placements) {
    if (p.preset && p.stack_size > 1 && current.visible.has(p.instance)) {
      const key = p.preset;
      if (!stacks.has(key)) stacks.set(key, []);
      stacks.get(key).push(p);
    }
  }
  for (const list of stacks.values()) list.sort((a, b) => a.stack_index - b.stack_index);
  for (const [id, cell] of cells) {
    const p = onPage.get(id);
    const n = cell.node;
    const shown = Boolean(p) && current.visible.has(id);
    if (p) {
      n.style.gridArea = `${p.row} / ${p.col} / span ${p.height} / span ${p.width}`;
      n.style.zIndex = String(p.z);
      const list = p.preset ? stacks.get(p.preset) : null;
      if (list && list.length > 1) {
        const rank = list.findIndex((q) => q.instance === id);
        n.style.height = rank < 0 ? "" : `${100 / list.length}%`;
        n.style.transform = rank < 0 ? "" : `translateY(${rank * 100}%)`;
        n.style.alignSelf = "start";
      } else {
        n.style.height = "";
        n.style.transform = "";
        n.style.alignSelf = "";
      }
    }
    setShown(cell, shown, FADE_MS);
    if (cell.el) cell.el.page = current.page;
  }
}

function setShown(cell, shown, fade) {
  const n = cell.node;
  const was = n.dataset.gone !== "true" && n.dataset.hidden !== "true";
  n.style.setProperty("--mm3-fade", `${fade}ms`);
  clearTimeout(cell.goneTimer);
  if (shown) {
    n.dataset.gone = "false";
    // Next frame, so that the opacity transition runs from 0.
    requestAnimationFrame(() => {
      n.dataset.hidden = "false";
      scheduleResize(cell);
    });
    if (!was && cell.el) cell.el._guard("onResume", cell.el.onResume);
  } else {
    n.dataset.hidden = "true";
    cell.goneTimer = setTimeout(() => {
      n.dataset.gone = "true";
    }, fade);
    if (was && cell.el) cell.el._guard("onSuspend", cell.el.onSuspend);
  }
}

function onControl(instance, data) {
  switch (data.action) {
    case "page":
      return changePage(data);
    case "show":
    case "hide": {
      const cell = cells.get(instance);
      if (!cell || !current) return undefined;
      if (data.action === "show") current.visible.add(instance);
      else current.visible.delete(instance);
      place();
      return undefined;
    }
    case "status": {
      const cell = cells.get(instance);
      if (cell) cell.node.dataset.status = data.status;
      return undefined;
    }
    case "locale":
      return changeLocale(instance, data);
    case "styles":
      return applyCustomCss(data.custom_css);
    case "layout":
      if (welcome) {
        applyLayout(data.layout);
        place();
      }
      return undefined;
    case "reload":
      if (data.scope === "page") location.reload();
      else resync();
      return undefined;
    default:
      return undefined;
  }
}

async function changeLocale(instance, data) {
  // CORE-267: new catalog, config resolved for the new locale, then onLocaleChange.
  const cell = cells.get(instance);
  if (!cell) return;
  cell.info = { ...cell.info, locale: data.locale, config: data.config, catalog: data.catalog };
  cell.catalog = await fetchCatalog(cell.info);
  cell.failure.textContent = unavailableText(cell);
  if (!cell.el) return;
  cell.el.locale = data.locale;
  cell.el.config = data.config;
  cell.el.catalog = cell.catalog;
  cell.el._guard("onLocaleChange", cell.el.onLocaleChange, data.locale);
}

function resync() {
  // Rebuild from a new welcome on the same connection: close, then reconnect at once.
  attempt = 0;
  const ws = socket;
  socket = null;
  ws?.close();
  connect();
}

async function changePage(data) {
  if (!welcome) return;
  const visible = new Set(data.visible);
  const apply = () => {
    welcome.page = data.page;
    current = { page: data.page, visible, transition: data.transition };
    place();
    for (const cell of cells.values()) {
      if (cell.el && visible.has(cell.id)) cell.el._guard("onPageChange", cell.el.onPageChange, data.page);
    }
  };
  if (data.transition === "fade" && current && current.page !== data.page) {
    grid.dataset.pageFade = "true";
    grid.dataset.switching = "true";
    await new Promise((r) => setTimeout(r, FADE_MS));
    apply();
    requestAnimationFrame(() => {
      grid.dataset.switching = "false";
    });
  } else {
    apply();
  }
}

function applyCustomCss(version) {
  if (!version) {
    customLink?.remove();
    customLink = null;
    return;
  }
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = `/custom.css?v=${encodeURIComponent(version)}`;
  link.addEventListener("load", () => {
    customLink?.remove();
    customLink = link;
  });
  document.head.append(link);
}

// --- sizes, fit ----------------------------------------------------------------------

function scheduleResize(cell) {
  if (cell.raf) return;
  cell.raf = requestAnimationFrame(() => {
    cell.raf = 0;
    if (!cell.el || cell.node.dataset.gone === "true") return;
    const w = Math.round(cell.area.clientWidth);
    const h = Math.round(cell.area.clientHeight);
    if (w === cell.w && h === cell.h) return;
    cell.w = w;
    cell.h = h;
    cell.el._guard("onResize", cell.el.onResize, w, h);
    fit(cell);
  });
}

function fit(cell) {
  const box = cell.fitBox;
  if (cell.area.dataset.overflow !== "fit") {
    box.style.removeProperty("transform");
    return;
  }
  box.style.removeProperty("transform");
  const w = box.scrollWidth;
  const h = box.scrollHeight;
  const aw = cell.area.clientWidth;
  const ah = cell.area.clientHeight;
  if (!w || !h || !aw || !ah) return;
  const scale = Math.min(1, aw / w, ah / h);
  if (scale < 1) box.style.transform = `scale(${scale})`;
}

// --- touch (CORE-260, CORE-276) --------------------------------------------------------

let pointer = null;
window.addEventListener("pointerdown", (ev) => {
  if (mode !== "mirror" || !ev.isPrimary || !welcome?.touch) return;
  // A touch on an element that takes gestures itself is the element's (CORE-235).
  const owner = ev.target instanceof Element ? ev.target.closest(".mm3-cell") : null;
  if (owner && cells.get(owner.dataset.instance)?.info.touch) return;
  pointer = { x: ev.clientX, y: ev.clientY, t: performance.now() };
});
window.addEventListener("pointerup", (ev) => {
  if (!pointer || !ev.isPrimary) return;
  const dx = ev.clientX - pointer.x;
  const dy = ev.clientY - pointer.y;
  const dt = performance.now() - pointer.t;
  pointer = null;
  let gesture = "tap";
  if (Math.abs(dx) >= 0.25 * window.innerWidth && Math.abs(dx) >= 2 * Math.abs(dy) && dt <= 1000) {
    gesture = dx < 0 ? "swipe_left" : "swipe_right";
  }
  host.send("touch", { gesture });
});
window.addEventListener("pointercancel", () => {
  pointer = null;
});

connect();
