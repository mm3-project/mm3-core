// The module-facing API of the front-end contract (Module Contract section 5):
// `html`, `svg`, `nothing` and `MirrorElement`. lit-html is an engine detail
// behind this file (MOD-37).
// Implements CORE-64 (Core spec 0.14, §10.1 Security)
// Implements CORE-95, CORE-255, CORE-259 (Core spec 0.14, §6.5 Screen layout)
// Implements CORE-276 (Core spec 0.14, §5.6 WebSocket protocol)
// Implements CORE-265, CORE-266 (Core spec 0.15, §8.5 Catalogs at run time)
// Implements CORE-114 (Core spec 0.22, §10.5 Diagnostics and debugging): render,
// onMessage and onNotification are wrapped; errors go out with stack and user agent
// Implements CORE-98 (Core spec 0.1, §6.6 Live layout preview): `preview` is true in
// the admin's preview shell, so modules can render a placeholder instead
// Implements MOD-38 (Module Contract 1.0 rev 13, §5.3 Rules): `render` is exported for
// admin elements
// Implements CORE-409, CORE-413, CORE-417 (Core spec 0.53, §9.7 Media service): the
// elements `mm3-player` and `mm3-youtube`; sound only where `welcome.audio` is true
import { html, svg, nothing, render as litRender } from "/core/vendor/lit-html/lit-html.js";

// `render` is for admin elements (MOD-38), which extend HTMLElement and render themselves.
export { html, svg, nothing, litRender as render };

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
    // CORE-417: read by mm3-player and mm3-youtube inside this element, not by modules.
    this._mediaAudio = info.audio === true;
    this._youtube = info.youtube === true;
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
    this._host?.send(
      "log",
      {
        level: "error",
        message: `${where}: ${err && err.message ? err.message : err}`,
        stack: err && err.stack ? String(err.stack) : undefined,
        user_agent: navigator.userAgent,
        fields: {},
      },
      this.instanceId,
    );
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

// --- media elements (core spec 9.7) ----------------------------------------------------

const RETRY_MS = 30000; // CORE-409: a failed player tries again every 30 s
const HLS_TYPE = "application/vnd.apple.mpegurl";
const IMAGE_EXT = /\.(jpe?g|png|webp|gif|avif)$/i;
const AUDIO_EXT = /\.(mp3|m4a|aac|ogg|opus|flac|wav)$/i;
const YOUTUBE_ORIGIN = "https://www.youtube-nocookie.com";
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;

/** The module element an engine media element sits in, through nested shadow roots. */
function mirrorHost(node) {
  let n = node;
  for (;;) {
    const host = n.getRootNode().host;
    if (!host) return null;
    if (host instanceof MirrorElement) return host;
    n = host;
  }
}

/** CORE-409: in the preview shell a `/media/` URL is served under `/admin/preview/media/`. */
function mediaUrl(src) {
  const preview = document.documentElement.dataset.mode === "preview";
  return preview && src.startsWith("/media/") ? `/admin/preview${src}` : src;
}

function mediaKind(url) {
  let path = url;
  try {
    path = new URL(url, location.href).pathname;
  } catch {
    // keep the raw string
  }
  if (/\.m3u8$/i.test(path)) return "hls";
  if (/\.mjpg$/i.test(path) || IMAGE_EXT.test(path)) return "image";
  if (AUDIO_EXT.test(path)) return "audio";
  return "video";
}

/** Whether sound may play here (CORE-409): `welcome.audio`, and `muted` always wins. */
function soundAllowed(el) {
  const host = mirrorHost(el);
  // CORE-418: the shell's takeover player is in no element; the shell gives it `_mediaAudio`.
  const audio = host ? host._mediaAudio : el._mediaAudio;
  return audio === true && !el.hasAttribute("muted");
}

function mediaEvent(el, type, detail) {
  el.dispatchEvent(new CustomEvent(type, { bubbles: true, composed: true, detail }));
}

let playerSheet = null;
function mediaSheet() {
  if (!playerSheet) {
    playerSheet = new CSSStyleSheet();
    playerSheet.replaceSync(
      ":host { display: block; position: relative; overflow: hidden; width: 100%; height: 100%; }" +
        ".media { display: block; width: 100%; height: 100%; border: 0; object-fit: contain; }" +
        ":host([fit='cover']) .media { object-fit: cover; }" +
        "audio.media { display: none; }" +
        ".placeholder { width: 100%; height: 100%; min-height: 2em; display: flex; align-items: center;" +
        " justify-content: center; background: rgba(255, 255, 255, 0.06); }" +
        ".placeholder svg { width: 3em; height: 3em; fill: currentColor; opacity: 0.4; }",
    );
  }
  return playerSheet;
}

/**
 * `<mm3-player src fit loop muted>` (CORE-409): plays an MJPEG URL in an image, an HLS URL
 * natively (no HLS library: `mm3-media-error` `unsupported` without native HLS), or a file
 * URL of the media folder. Sound plays only where `welcome.audio` is true. Events
 * `mm3-media-error` `{reason, status?}` and `mm3-media-playing` bubble out of the shadow
 * root; after an error it tries again every 30 s. `stop()` and `start()` let an element
 * stop it while suspended (MOD-27); removing it or its `src` stops it too.
 */
class MirrorPlayer extends HTMLElement {
  static get observedAttributes() {
    return ["src", "loop", "muted"];
  }

  constructor() {
    super();
    this._root = this.attachShadow({ mode: "open" });
    this._root.adoptedStyleSheets = [mediaSheet()];
    this._media = null;
    this._timer = null;
    this._probe = null;
    this._attempt = 0;
    this._stopped = false;
  }

  connectedCallback() {
    if (!this._stopped) this._load();
  }

  disconnectedCallback() {
    this._clear();
  }

  attributeChangedCallback(name, oldValue, newValue) {
    if (!this.isConnected || oldValue === newValue) return;
    if (name === "src") {
      if (!this._stopped) this._load();
      return;
    }
    const m = this._media;
    if (!m || m.tagName === "IMG") return;
    if (name === "loop") m.loop = this.hasAttribute("loop");
    if (name === "muted") m.muted = !soundAllowed(this);
  }

  /** Stop playing and close the stream until `start()`. */
  stop() {
    this._stopped = true;
    this._clear();
  }

  /** Start again after `stop()`. */
  start() {
    this._stopped = false;
    if (this.isConnected) this._load();
  }

  _clear() {
    clearTimeout(this._timer);
    this._timer = null;
    if (this._probe) this._probe.abort();
    this._probe = null;
    const m = this._media;
    this._media = null;
    this._attempt += 1;
    if (m) {
      if (m.tagName === "IMG") {
        m.removeAttribute("src"); // closes the MJPEG response
      } else {
        m.pause();
        m.removeAttribute("src");
        m.load();
      }
    }
    this._root.replaceChildren();
  }

  _load() {
    this._clear();
    const src = this.getAttribute("src");
    if (!src) return;
    const url = mediaUrl(src);
    const kind = mediaKind(url);
    const attempt = this._attempt;
    const failed = (reason) => {
      if (attempt === this._attempt) this._failed(url, attempt, reason);
    };
    let played = false;
    const playing = () => {
      if (attempt !== this._attempt || played) return;
      played = true;
      mediaEvent(this, "mm3-media-playing", {});
    };
    if (kind === "hls" && !document.createElement("video").canPlayType(HLS_TYPE)) {
      this._failed(url, attempt, "unsupported");
      return;
    }
    let m;
    if (kind === "image") {
      m = document.createElement("img");
      m.alt = "";
      m.addEventListener("load", playing);
      m.addEventListener("error", () => failed(null));
    } else {
      m = document.createElement(kind === "audio" ? "audio" : "video");
      m.autoplay = true;
      m.playsInline = true;
      m.loop = this.hasAttribute("loop");
      m.muted = !soundAllowed(this);
      m.addEventListener("playing", playing);
      m.addEventListener("error", () => {
        const code = m.error ? m.error.code : 0;
        failed(code === MediaError.MEDIA_ERR_DECODE ? "decode" : null);
      });
      // A live stream that ends is gone; a file that ends is simply over.
      if (kind === "hls") m.addEventListener("ended", () => failed("unavailable"));
    }
    m.className = "media";
    this._media = m;
    this._root.append(m);
    m.src = url;
    if (m.tagName === "IMG") {
      m.decode().then(playing, () => {});
      return;
    }
    m.play().catch((err) => {
      // A browser that refuses sound without a gesture still plays muted.
      if (attempt === this._attempt && err && err.name === "NotAllowedError" && !m.muted) {
        m.muted = true;
        m.play().catch(() => {});
      }
    });
  }

  async _failed(url, attempt, reason) {
    let detail = { reason: reason || "unavailable" };
    if (!reason) detail = await this._why(url, attempt);
    if (attempt !== this._attempt) return;
    const m = this._media;
    this._media = null;
    if (m) {
      m.removeAttribute("src");
      if (m.tagName !== "IMG") m.load();
    }
    this._root.replaceChildren();
    mediaEvent(this, "mm3-media-error", detail);
    if (detail.reason === "unsupported") return;
    this._timer = setTimeout(() => {
      if (this.isConnected && !this._stopped) this._load();
    }, RETRY_MS);
  }

  /** Ask the URL once to tell an HTTP refusal from a decoding failure. */
  async _why(url, attempt) {
    const probe = new AbortController();
    this._probe = probe;
    try {
      const res = await fetch(url, { cache: "no-store", signal: probe.signal });
      probe.abort(); // only the status is needed; an MJPEG body never ends
      if (res.status === 403) return { reason: "forbidden", status: 403 };
      if (!res.ok) return { reason: "unavailable", status: res.status };
      return { reason: "decode", status: res.status };
    } catch {
      return attempt === this._attempt ? { reason: "network" } : { reason: "unavailable" };
    } finally {
      if (this._probe === probe) this._probe = null;
    }
  }
}

function placeholder() {
  const box = document.createElement("div");
  box.className = "placeholder";
  const ns = "http://www.w3.org/2000/svg";
  const icon = document.createElementNS(ns, "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", "M8 5v14l11-7z");
  icon.append(path);
  box.append(icon);
  return box;
}

/**
 * `<mm3-youtube video-id loop muted>` (CORE-413): the youtube-nocookie.com iframe, only
 * while the instance has the capability `media.youtube` and the grant `youtube: true`
 * (`welcome`, CORE-417); otherwise a placeholder and `mm3-media-error` `forbidden`
 * (`unavailable` for an invalid id). Sound follows CORE-409.
 */
class MirrorYouTube extends HTMLElement {
  static get observedAttributes() {
    return ["video-id", "loop", "muted"];
  }

  constructor() {
    super();
    this._root = this.attachShadow({ mode: "open" });
    this._root.adoptedStyleSheets = [mediaSheet()];
  }

  connectedCallback() {
    this._build();
  }

  disconnectedCallback() {
    this._root.replaceChildren();
  }

  attributeChangedCallback(name, oldValue, newValue) {
    if (this.isConnected && oldValue !== newValue) this._build();
  }

  /** Remove the iframe until `start()` (MOD-27). */
  stop() {
    this._stopped = true;
    this._root.replaceChildren();
  }

  /** Build the iframe again after `stop()`. */
  start() {
    this._stopped = false;
    if (this.isConnected) this._build();
  }

  _build() {
    this._root.replaceChildren();
    if (this._stopped) return;
    const host = mirrorHost(this);
    const id = this.getAttribute("video-id") || "";
    if (!host || !host._youtube || !VIDEO_ID.test(id)) {
      this._root.append(placeholder());
      mediaEvent(this, "mm3-media-error", { reason: host && host._youtube ? "unavailable" : "forbidden" });
      return;
    }
    const params = new URLSearchParams({
      autoplay: "1",
      mute: soundAllowed(this) ? "0" : "1",
      controls: "0",
      playsinline: "1",
      rel: "0",
    });
    if (this.hasAttribute("loop")) {
      params.set("loop", "1");
      params.set("playlist", id);
    }
    const frame = document.createElement("iframe");
    frame.className = "media";
    frame.title = "YouTube";
    frame.referrerPolicy = "strict-origin-when-cross-origin";
    frame.allow = "autoplay; encrypted-media";
    frame.src = `${YOUTUBE_ORIGIN}/embed/${id}?${params}`;
    this._root.append(frame);
  }
}

if (!customElements.get("mm3-player")) customElements.define("mm3-player", MirrorPlayer);
if (!customElements.get("mm3-youtube")) customElements.define("mm3-youtube", MirrorYouTube);
