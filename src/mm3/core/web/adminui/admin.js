// Shared helpers of the admin pages: texts, the language picker, the REST API and
// the admin channel.
// Implements CORE-309 (Core spec 0.26, §9.1 Remote-control API): the header offers
// the shipped languages; a choice calls PUT /api/v1/session/locale and reloads.
// Implements CORE-298 (Core spec 0.25, §9.1 Remote-control API): API errors are
// shown with the catalog text of their code.
// Implements CORE-304 (Core spec 0.22, §10.5 Diagnostics and debugging): one
// /admin/ws connection with subscribe and unsubscribe per topic, reconnecting.
// Implements CORE-381 (Core spec 0.41, §9.1 Remote-control API): the MQTT connection
// indicator in the header of every admin page, fed by one shared `status` subscription.
// Implements CORE-35, CORE-206 (Core spec 0.38, §6.4 Web configuration editor):
// validation records are shown with the localized text of their CORE_CFG_* code.
// Implements CORE-361 (Core spec 0.45, §10.8 Backup and restore): `upload` sends a backup
// archive as the raw body of POST /api/v1/backups/upload.

let messages = {};
let locale = "en";

let loading = null;

export function loadTexts() {
  if (!loading) {
    loading = (async () => {
      const r = await fetch("/admin/i18n.json", { credentials: "same-origin" });
      if (!r.ok) return null;
      const data = await r.json();
      messages = data.messages || {};
      locale = data.locale || "en";
      document.documentElement.lang = locale;
      languagePicker(data);
      connectionIndicator();
      return data;
    })();
  }
  return loading;
}

function languagePicker(data) {
  const bar = document.querySelector("header.bar");
  if (!bar || bar.querySelector("select.lang") || !data.languages) return;
  const select = document.createElement("select");
  select.className = "lang";
  select.setAttribute("aria-label", t("CORE_ADMIN_LANGUAGE"));
  const follow = document.createElement("option");
  follow.value = "";
  follow.textContent = t("CORE_ADMIN_LANGUAGE_ENGINE");
  select.append(follow);
  for (const lang of data.languages) {
    const option = document.createElement("option");
    option.value = lang.code;
    option.textContent = lang.name;
    option.selected = lang.code === data.picked;
    select.append(option);
  }
  select.addEventListener("change", async () => {
    try {
      await api("PUT", "/session/locale", { locale: select.value || null });
      window.location.reload();
    } catch (err) {
      console.error(err);
    }
  });
  const logout = bar.querySelector("form");
  bar.insertBefore(select, logout);
}

/** Catalog key of each state of the MQTT indicator (CORE-381). */
export const MQTT_KEYS = {
  off: "CORE_ADMIN_MQTT_OFF",
  connected: "CORE_ADMIN_MQTT_CONNECTED",
  connecting: "CORE_ADMIN_MQTT_CONNECTING",
  error: "CORE_ADMIN_MQTT_ERROR",
  unavailable: "CORE_ADMIN_MQTT_UNAVAILABLE",
};

/**
 * The state of the MQTT bridge from the `mqtt` object of the status (CORE-381):
 * `off` without a bridge, `connected`, `connecting` (not connected, no error yet or
 * between retries) and `error`.
 */
export function mqttState(mqtt) {
  if (!mqtt) return "off";
  if (mqtt.connected) return "connected";
  return mqtt.error ? "error" : "connecting";
}

const statusListeners = new Set();
let statusChannel = null;
let statusNow = { live: false, status: null };

function publishStatus(next) {
  statusNow = next;
  for (const listener of statusListeners) listener(statusNow);
}

/**
 * Call `listener({live, status})` now and on every change of the shared admin
 * channel's `status` topic (CORE-304, CORE-317); returns the function that stops it.
 * One channel per page serves the header indicator and the page's own status view.
 */
export function onStatus(listener) {
  if (!statusChannel) {
    statusChannel = new AdminChannel(
      (frame) => {
        if (frame.kind === "status") publishStatus({ ...statusNow, status: frame.data.status });
      },
      (on) => publishStatus({ ...statusNow, live: on }),
    );
    statusChannel.subscribe("status");
  }
  statusListeners.add(listener);
  listener(statusNow);
  return () => statusListeners.delete(listener);
}

function connectionIndicator() {
  const bar = document.querySelector("header.bar");
  if (!bar || bar.querySelector(".conn")) return;
  const box = document.createElement("span");
  box.className = "conn";
  box.setAttribute("role", "status");
  const dot = document.createElement("span");
  dot.className = "dot";
  dot.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.className = "label";
  box.append(dot, label);
  bar.insertBefore(box, bar.querySelector("select.lang") || bar.querySelector("form"));
  onStatus(({ live, status }) => {
    const known = live && status;
    const mqtt = known ? status.mqtt : null;
    const state = known ? mqttState(mqtt) : "unavailable";
    box.dataset.state = state;
    label.textContent = t(MQTT_KEYS[state]);
    const tips = [];
    if (mqtt && mqtt.since) tips.push(t("CORE_ADMIN_MQTT_SINCE", { time: fmtTime(mqtt.since) }));
    if (state === "error") tips.push(mqtt.error);
    box.title = tips.join(" — ");
  });
}

/** The language of this admin session (CORE-309); `en` until the texts are loaded. */
export function sessionLocale() {
  return locale;
}

export function t(key, params = {}) {
  const text = messages[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (m, name) => (name in params ? String(params[name]) : m));
}

/**
 * A validation record's text in the admin language: the catalog text of its
 * CORE_CFG_* code with its params, else the server's English message (ISS-010).
 */
export function recordText(r) {
  if (!r) return "";
  if (r.code && r.code in messages) {
    const params = {};
    for (const [k, v] of Object.entries(r.params || {})) params[k] = Array.isArray(v) ? v.join(", ") : v;
    return t(r.code, params);
  }
  return r.message || r.code || "";
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

/**
 * Send `file` as the raw body of `POST /api/v1{path}` (a backup upload, CORE-361) and read the
 * JSON answer. `onProgress(sent, total)` follows the upload. The promise has `abort()`.
 * Errors are `ApiError`s like those of `api`.
 */
export function upload(path, file, onProgress = () => {}) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    xhr.open("POST", `/api/v1${path}`);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (ev) => ev.lengthComputable && onProgress(ev.loaded, ev.total);
    xhr.onload = () => {
      let body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(body);
      if (xhr.status === 401) window.location.assign("/admin/login");
      reject(new ApiError(body || { detail: xhr.statusText }));
    };
    xhr.onerror = () => reject(new ApiError({ code: "CORE_API_UNAVAILABLE", detail: "network error" }));
    xhr.onabort = () => reject(new ApiError({ detail: "aborted" }));
    xhr.send(file);
  });
  promise.abort = () => xhr.abort();
  return promise;
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
      if (this.closed) return;
      if (ev.code === 4403) {
        window.location.assign("/admin/login");
        return;
      }
      setTimeout(() => this.open(), this.delay);
      this.delay = Math.min(this.delay * 2, 10000);
    });
  }

  /** Close for good: no reconnect. */
  close() {
    this.closed = true;
    this.ws?.close();
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
