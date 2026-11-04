// Shared helpers of the admin pages: texts, the REST API and the admin channel.
// Implements CORE-298 (Core spec 0.23, §9.1 Remote-control API): API errors are
// shown with the catalog text of their code.
// Implements CORE-304 (Core spec 0.22, §10.5 Diagnostics and debugging): one
// /admin/ws connection with subscribe and unsubscribe per topic, reconnecting.

let messages = {};
let locale = "en";

export async function loadTexts() {
  const r = await fetch("/admin/i18n.json", { credentials: "same-origin" });
  if (r.ok) {
    const data = await r.json();
    messages = data.messages || {};
    locale = data.locale || "en";
    document.documentElement.lang = locale;
  }
}

export function t(key, params = {}) {
  const text = messages[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (m, name) => (name in params ? String(params[name]) : m));
}

export function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(locale, { hour12: false });
}

export function fmtDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h ${m}m` : h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
}

export class ApiError extends Error {
  constructor(problem) {
    super(problem && problem.code ? t(problem.code) : String(problem && problem.detail));
    this.problem = problem;
  }
}

export async function api(method, path, body) {
  const init = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const r = await fetch(`/api/v1${path}`, init);
  const type = r.headers.get("content-type") || "";
  if (!r.ok) {
    if (r.status === 401) window.location.assign("/admin/login");
    const problem = type.includes("json") ? await r.json() : { detail: r.statusText };
    throw new ApiError(problem);
  }
  return type.includes("json") ? r.json() : r.text();
}

export function download(name, text, type = "application/x-ndjson") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** The admin channel: frames {v: 1, kind, data}; topics survive reconnects. */
export class AdminChannel {
  constructor(onFrame, onState) {
    this.onFrame = onFrame;
    this.onState = onState;
    this.topics = new Map();
    this.ws = null;
    this.delay = 500;
    this.open();
  }

  open() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/admin/ws`);
    this.ws = ws;
    ws.addEventListener("open", () => {
      this.delay = 500;
      this.onState(true);
      for (const [topic, filter] of this.topics) this._send("subscribe", { topic, filter });
    });
    ws.addEventListener("message", (ev) => {
      try {
        this.onFrame(JSON.parse(ev.data));
      } catch (err) {
        console.error(err);
      }
    });
    ws.addEventListener("close", (ev) => {
      this.onState(false);
      if (ev.code === 4403) {
        window.location.assign("/admin/login");
        return;
      }
      setTimeout(() => this.open(), this.delay);
      this.delay = Math.min(this.delay * 2, 10000);
    });
  }

  _send(kind, data) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ v: 1, kind, data }));
    }
  }

  subscribe(topic, filter = {}) {
    this.topics.set(topic, filter);
    this._send("subscribe", { topic, filter });
  }

  unsubscribe(topic) {
    this.topics.delete(topic);
    this._send("unsubscribe", { topic });
  }
}
