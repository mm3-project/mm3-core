// The Media page /admin/media: the sources with their preview and the full-screen takeover, the
// files of the media folder and the shares.
// Implements CORE-414 (Core spec 0.58, §9.7 Media service): the three parts Sources, Files and
// Shares; reads GET /api/v1/media, /devices, /media/shares and /installer when it opens, after
// each of its own actions, on Refresh and when the `status` topic changes (CORE-304); follows a
// sync only through the `installer` topic (CORE-356); never polls.
// Implements CORE-409 (Core spec 0.53, §9.7 Media service): the preview plays in an `mm3-player`
// from /admin/preview/media/, one at a time and only when asked.
// Implements CORE-411 (Core spec 0.54, §9.7 Media service): Show full screen and Stop full
// screen send POST and DELETE /api/v1/media/takeover; a folder source asks for a file first.
// Implements CORE-392 (Core spec 0.47, §6.11 Camera devices): the preview of a camera is
// disabled with the reason while its device has `images: false`.
// Implements CORE-405 (Core spec 0.57, §9.7 Media service): the file browser, the upload with
// its progress, new folder and delete (a folder that is not empty asks again, `recursive=true`);
// shares are read-only.
// Implements CORE-406 (Core spec 0.57, §9.7 Media service): the shares with their state and Sync
// now.
// Implements CORE-384, CORE-385, CORE-386, CORE-387 (Core spec 0.44, §9.1 Remote-control
// API): the dialogs, action rows and banners of the shared patterns in ui.js.
import { html, nothing, render } from "/admin/static/lit-html.js";
import { AdminChannel, ApiError, api, fmtTime, loadTexts, onStatus, t, upload } from "/admin/static/admin.js";
import { Messages, actionRow, banner, closeOnBackdrop, confirmDialog } from "/admin/static/ui.js";
import { reasonText, stateText } from "/admin/static/media.js";

/** The states in which an installer operation is over (CORE-355). */
export const FINAL_STATES = ["done", "rolled_back", "failed"];

/** The files a media source can play (CORE-404): anything else in a `files` path is a folder. */
const MEDIA_FILE = /\.(mp4|m4v|webm|mp3|m4a|aac|ogg|opus|flac|wav|jpe?g|png|webp|gif|avif)$/i;

/** `true` when the operation has reached a final state. */
export const isFinal = (op) => !!op && FINAL_STATES.includes(op.state);

/** A size in bytes as B, KiB, MiB or GiB. */
export function sizeText(bytes) {
  const n = Number(bytes);
  if (bytes === null || bytes === undefined || !Number.isFinite(n) || n < 0) return "";
  if (n < 1024) return `${n} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = n;
  let i = -1;
  do {
    value /= 1024;
    i += 1;
  } while (value >= 1024 && i < units.length - 1);
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[i]}`;
}

/** The path of a URL piece by piece, each piece escaped. */
export const encodePath = (path) => String(path).split("/").map(encodeURIComponent).join("/");

/** `true` when a `files` source points at a folder: its path is not a media file (CORE-411). */
export const isFolderSource = (src) => !!src && src.kind === "files" && !MEDIA_FILE.test(src.path || "");

/**
 * The URL a source plays from in the admin preview (CORE-400): the MJPEG stream, the HLS playlist
 * or the file, all under /admin/preview/media/; null for a folder, which has nothing to play.
 */
export function previewUrl(src) {
  const base = "/admin/preview/media";
  if (isFolderSource(src)) return null;
  const how = src.delivery || (src.kind === "files" ? "file" : "mjpeg");
  if (how === "file") return `${base}/files/${encodePath(src.path)}`;
  if (how === "hls") return `${base}/${encodeURIComponent(src.name)}/hls/index.m3u8`;
  return `${base}/${encodeURIComponent(src.name)}/stream.mjpg`;
}

/** Why the preview of `src` cannot start, as `{key, params}`, or null (CORE-414, CORE-392). */
export function previewReason(src, devices) {
  if (!src) return { key: "CORE_ADMIN_MEDIA_REASON_NO_SOURCE", params: {} };
  if (isFolderSource(src)) return { key: "CORE_ADMIN_MEDIA_REASON_FOLDER", params: {} };
  if (src.kind === "camera") {
    const device = (devices || []).find((d) => d.name === src.device);
    if (device && !device.images) return { key: "CORE_ADMIN_MEDIA_REASON_IMAGES", params: { device: device.name } };
  }
  return null;
}

/** Why the folder at `path` cannot be changed, or null: shares are read-only (CORE-405). */
export function writeReason(path) {
  const root = String(path).split("/")[0];
  return root === "files" ? null : { key: "CORE_ADMIN_MEDIA_REASON_SHARES_READONLY", params: {} };
}

/** Why Sync now is disabled for `share`, as `{key, params}`, or null (CORE-406). */
export function syncReason(share, installer, syncing) {
  if (share.method === "mount") return { key: "CORE_ADMIN_MEDIA_REASON_MOUNT", params: {} };
  if (share.state === "syncing" || (syncing && syncing.share === share.name)) {
    return { key: "CORE_ADMIN_MEDIA_REASON_SYNCING", params: {} };
  }
  if (!installer || !installer.available) return { key: "CORE_ADMIN_MEDIA_REASON_INSTALLER_AWAY", params: {} };
  if (installer.busy) return { key: "CORE_ADMIN_MEDIA_REASON_INSTALLER_BUSY", params: {} };
  return null;
}

/** The text of a failed request: the catalog text of the problem code (CORE-298). */
export function problemText(err) {
  if (!(err instanceof ApiError)) return String((err && err.message) || err);
  const p = err.problem || {};
  if (p.code === "CORE_API_INVALID_REQUEST" && p.detail) return `${err.message}: ${p.detail}`;
  return err.message;
}

const text = (reason) => (reason ? t(reason.key, reason.params) : null);
const upper = (s) => String(s).toUpperCase();
const known = (prefix, value) => {
  const key = `${prefix}${upper(value)}`;
  return t(key) === key ? String(value) : t(key);
};

let playerLoaded = null;
/** Load the engine's elements once (`mm3-player`), served on the admin listener. */
function loadPlayer() {
  if (!playerLoaded) {
    playerLoaded = import("/core/mm3.js").catch((err) => {
      playerLoaded = null;
      throw err;
    });
  }
  return playerLoaded;
}

/** A button of a table row: disabled with `aria-disabled`, a tooltip and the reason beside it. */
function rowButton({ label, onClick, reason = null, id = null, cls = "" }) {
  return html`<button type="button" class=${cls || nothing} data-action=${id || nothing}
      aria-disabled=${reason ? "true" : "false"} title=${reason || nothing}
      @click=${(ev) => (reason ? ev.preventDefault() : onClick(ev))}>${label}</button>
    ${reason ? html`<div class="muted reason">${reason}</div>` : nothing}`;
}

/** The element of the page. */
class AdminMedia extends (globalThis.HTMLElement || class {}) {
  async connectedCallback() {
    await loadTexts();
    this.messages = new Messages(() => this.update());
    this.media = null; // GET /api/v1/media
    this.devices = [];
    this.shares = null; // null while unknown or unreadable
    this.sharesError = null;
    this.installer = null; // {available, busy, locked_until}
    this.selected = null; // the name of the selected source
    this.preview = null; // {source, url}: the one preview that plays
    this.path = "files";
    this.entries = [];
    this.filesError = null;
    this.chosen = null; // the file picked for an upload
    this.progress = null; // {name, sent, total} while an upload runs
    this.sync = null; // {id, share}: a sync started here that has not ended
    this.dialog = null; // {kind: "folder" | "takeover", ...}
    this.live = true;
    this.statusKey = null;
    this.error = null;
    this.channel = new AdminChannel(
      (frame) => frame.kind === "installer" && this.onInstaller(frame.data),
      (on) => this.onChannel(on),
    );
    this.channel.subscribe("installer");
    this.stopStatus = onStatus(({ live, status }) => this.onStatus(live, status));
    await this.load();
  }

  disconnectedCallback() {
    if (this.channel) this.channel.close();
    if (this.stopStatus) this.stopStatus();
    if (this.sending && this.sending.abort) this.sending.abort();
    this.preview = null;
  }

  // --- reading ---------------------------------------------------------------------

  /** Read the state, the devices, the shares and the installer, and the open folder. */
  async load() {
    const [media, devices, shares, installer] = await Promise.allSettled([
      api("GET", "/media"),
      api("GET", "/devices"),
      api("GET", "/media/shares"),
      api("GET", "/installer"),
    ]);
    if (media.status === "fulfilled") {
      this.media = media.value;
      this.error = null;
    } else {
      this.error = problemText(media.reason);
    }
    this.devices = devices.status === "fulfilled" ? devices.value : [];
    if (shares.status === "fulfilled") {
      this.shares = shares.value;
      this.sharesError = null;
    } else {
      this.shares = null;
      this.sharesError = problemText(shares.reason);
    }
    this.installer =
      installer.status === "fulfilled" ? installer.value : { available: false, busy: false, locked_until: null };
    const sources = this.sources();
    if (!sources.some((s) => s.name === this.selected)) this.selected = sources.length ? sources[0].name : null;
    if (this.preview && !sources.some((s) => s.name === this.preview.source)) this.preview = null;
    await this.openFolder(this.path);
  }

  /** The `status` topic reports a change: read again, except for the first frame (CORE-414). */
  onStatus(live, status) {
    this.live = live;
    const key = status ? JSON.stringify({ ...status, uptime_s: 0 }) : null;
    const changed = this.statusKey !== null && key !== null && key !== this.statusKey;
    if (key !== null) this.statusKey = key;
    if (changed) this.load();
    else if (this.messages) this.update();
  }

  sources() {
    return (this.media && this.media.sources) || [];
  }

  source(name = this.selected) {
    return this.sources().find((s) => s.name === name) || null;
  }

  // --- the installer topic (CORE-356) -------------------------------------------------

  onInstaller({ status, operation }) {
    if (status) this.installer = status;
    if (operation && operation.action === "media_sync") this.follow(operation);
    this.update();
  }

  follow(op) {
    const mine = this.sync && this.sync.id === op.id;
    if (!isFinal(op)) return;
    if (!mine) {
      this.loadShares(); // a scheduled sync ended: the state changed
      return;
    }
    const share = this.sync.share;
    this.sync = null;
    if (op.state === "done") {
      this.messages.set("ok", t("CORE_ADMIN_MEDIA_SYNC_DONE", { name: share }));
    } else {
      const dismiss = { label: t("CORE_ADMIN_DISMISS"), onClick: () => this.messages.clear() };
      this.messages.set("error", t("CORE_ADMIN_MEDIA_SYNC_FAILED", { name: share, error: op.error || "" }), dismiss);
    }
    this.loadShares();
  }

  /** After the channel was lost, read the operation by id. */
  async onChannel(on) {
    if (!on || !this.sync) return;
    try {
      this.follow(await api("GET", `/installer/operations/${encodeURIComponent(this.sync.id)}`));
      this.update();
    } catch {
      // The engine is not back yet; the next connection tries again.
    }
  }

  async loadShares() {
    try {
      this.shares = await api("GET", "/media/shares");
      this.sharesError = null;
    } catch (err) {
      this.shares = null;
      this.sharesError = problemText(err);
    }
    this.update();
  }

  // --- sources ----------------------------------------------------------------------

  select(name) {
    this.selected = name;
    this.update();
  }

  async startPreview() {
    const src = this.source();
    const reason = previewReason(src, this.devices);
    if (reason) return;
    try {
      await loadPlayer();
    } catch {
      this.messages.set("error", t("CORE_ADMIN_MEDIA_PREVIEW_LOAD"));
      return;
    }
    this.messages.clear();
    this.preview = { source: src.name, url: previewUrl(src) };
    this.update();
  }

  stopPreview() {
    this.preview = null;
    this.update();
  }

  /** The player reports an error (CORE-409): `forbidden` says that camera images are off. */
  playerError(ev) {
    const reason = (ev.detail && ev.detail.reason) || "unavailable";
    const src = this.preview ? this.preview.source : "";
    const message =
      reason === "forbidden"
        ? t("CORE_ADMIN_MEDIA_PREVIEW_FORBIDDEN", { name: src })
        : t("CORE_ADMIN_MEDIA_PREVIEW_ERROR", { name: src, reason: known("CORE_ADMIN_MEDIA_PLAYER_", reason) });
    this.messages.set("error", message);
  }

  async show() {
    const src = this.source();
    if (!src) return;
    if (isFolderSource(src)) return this.openTakeoverDialog(src);
    await this.startTakeover(src.name, null);
  }

  async startTakeover(source, file) {
    const body = file ? { source, file } : { source };
    try {
      await api("POST", "/media/takeover", body);
      this.messages.set("ok", t("CORE_ADMIN_MEDIA_SHOW_DONE", { source: file ? `${source} / ${file}` : source }));
      this.closeDialog(false);
    } catch (err) {
      if (this.dialog) this.dialog.error = problemText(err);
      else this.messages.set("error", problemText(err));
    }
    await this.reloadMedia();
  }

  async stop() {
    try {
      await api("DELETE", "/media/takeover");
      this.messages.set("ok", t("CORE_ADMIN_MEDIA_STOP_DONE"));
    } catch (err) {
      this.messages.set("error", problemText(err));
    }
    await this.reloadMedia();
  }

  async reloadMedia() {
    try {
      this.media = await api("GET", "/media");
    } catch (err) {
      this.error = problemText(err);
    }
    this.update();
  }

  // --- files ------------------------------------------------------------------------

  /** List `path` and show it (CORE-405); a share that does not answer is an error banner. */
  async openFolder(path) {
    try {
      this.entries = await api("GET", `/media/files?path=${encodeURIComponent(path)}`);
      this.path = path;
      this.filesError = null;
    } catch (err) {
      this.filesError = problemText(err);
      if (path !== this.path) this.path = path;
      this.entries = [];
    }
    this.update();
  }

  up() {
    const parts = this.path.split("/");
    if (parts.length > 1) this.openFolder(parts.slice(0, -1).join("/"));
  }

  chooseFile(ev) {
    this.chosen = ev.target.files && ev.target.files[0] ? ev.target.files[0] : null;
    this.update();
  }

  async sendFile() {
    const file = this.chosen;
    if (!file) return;
    this.progress = { name: file.name, sent: 0, total: file.size };
    this.update();
    const sending = upload(
      `/media/files?path=${encodeURIComponent(this.path)}&name=${encodeURIComponent(file.name)}`,
      file,
      (sent, total) => {
        this.progress = { name: file.name, sent, total };
        this.update();
      },
    );
    this.sending = sending;
    try {
      await sending;
      this.messages.set("ok", t("CORE_ADMIN_MEDIA_UPLOAD_DONE", { name: file.name }));
      this.chosen = null;
      const input = this.querySelector("input[type=file]");
      if (input) input.value = "";
    } catch (err) {
      this.messages.set("error", problemText(err));
    }
    this.sending = null;
    this.progress = null;
    await this.openFolder(this.path);
  }

  async removeEntry(entry) {
    const full = `${this.path}/${entry.name}`;
    const folder = entry.kind === "folder";
    const first = await confirmDialog({
      title: t("CORE_ADMIN_MEDIA_DLG_DELETE_TITLE", { name: entry.name }),
      text: t(folder ? "CORE_ADMIN_MEDIA_DLG_DELETE_FOLDER_TEXT" : "CORE_ADMIN_MEDIA_DLG_DELETE_TEXT"),
      action: t("CORE_ADMIN_DELETE"),
      danger: true,
    });
    if (!first) return;
    const url = `/media/files?path=${encodeURIComponent(full)}`;
    try {
      try {
        await api("DELETE", url);
      } catch (err) {
        const conflict = err instanceof ApiError && err.problem && err.problem.code === "CORE_API_CONFLICT";
        if (!(folder && conflict)) throw err;
        const again = await confirmDialog({
          title: t("CORE_ADMIN_MEDIA_DLG_DELETE_ALL_TITLE", { name: entry.name }),
          text: t("CORE_ADMIN_MEDIA_DLG_DELETE_ALL_TEXT"),
          action: t("CORE_ADMIN_MEDIA_DLG_DELETE_ALL_ACTION"),
          danger: true,
        });
        if (!again) return;
        await api("DELETE", `${url}&recursive=true`);
      }
      this.messages.set("ok", t("CORE_ADMIN_MEDIA_DELETE_DONE", { name: entry.name }));
    } catch (err) {
      this.messages.set("error", problemText(err));
    }
    await this.openFolder(this.path);
  }

  // --- dialogs (CORE-385) --------------------------------------------------------------

  openFolderDialog() {
    this.dialog = { kind: "folder", name: "", error: null };
    this.update();
  }

  async submitFolder() {
    const name = this.dialog.name.trim();
    if (!name) {
      this.dialog.error = t("CORE_ADMIN_MEDIA_DLG_FOLDER_NEED_NAME");
      return this.update();
    }
    try {
      await api("POST", "/media/folders", { path: `${this.path}/${name}` });
      this.messages.set("ok", t("CORE_ADMIN_MEDIA_FOLDER_DONE", { name }));
      this.closeDialog(false);
    } catch (err) {
      this.dialog.error = problemText(err);
      return this.update();
    }
    await this.openFolder(this.path);
  }

  /** The dialog that asks which file of a folder source to show (CORE-414). */
  async openTakeoverDialog(src) {
    this.dialog = { kind: "takeover", source: src.name, root: src.path, dir: src.path, entries: [], file: null, error: null };
    this.update();
    await this.listDialog(src.path);
  }

  async listDialog(dir) {
    const d = this.dialog;
    try {
      d.entries = await api("GET", `/media/files?path=${encodeURIComponent(dir)}`);
      d.dir = dir;
      d.error = null;
      d.file = null;
    } catch (err) {
      d.error = problemText(err);
    }
    this.update();
  }

  closeDialog(update = true) {
    this.dialog = null;
    if (update) this.update();
  }

  // --- shares -----------------------------------------------------------------------

  async syncShare(name) {
    try {
      const { id } = await api("POST", `/media/shares/${encodeURIComponent(name)}/sync`);
      this.sync = { id, share: name };
      this.messages.set("info", t("CORE_ADMIN_MEDIA_SYNC_STARTED", { name }));
      this.onChannel(true);
    } catch (err) {
      this.messages.set("error", problemText(err));
    }
    try {
      this.installer = await api("GET", "/installer");
    } catch {
      // the installer topic brings the status
    }
    this.update();
  }

  // --- views --------------------------------------------------------------------------

  sourcesCard() {
    const sources = this.sources();
    const takeover = this.media && this.media.takeover;
    const src = this.source();
    const previewing = this.preview;
    const reason = text(previewReason(src, this.devices));
    return html`<section class="card media-sources"><h2>${t("CORE_ADMIN_MEDIA_H_SOURCES")}</h2>
      ${takeover
        ? html`<p class="takeover" data-takeover=${takeover.id}>${takeover.until
            ? t("CORE_ADMIN_MEDIA_TAKEOVER", {
                source: takeover.file ? `${takeover.source} / ${takeover.file}` : takeover.source,
                until: fmtTime(takeover.until),
              })
            : t("CORE_ADMIN_MEDIA_TAKEOVER_OPEN", {
                source: takeover.file ? `${takeover.source} / ${takeover.file}` : takeover.source,
              })}</p>`
        : html`<p class="muted takeover-none">${t("CORE_ADMIN_MEDIA_TAKEOVER_NONE")}</p>`}
      ${sources.length
        ? html`<table class="sources">
            <thead><tr><th></th>
              <th>${t("CORE_ADMIN_MEDIA_SOURCE")}</th><th>${t("CORE_ADMIN_MEDIA_KIND")}</th>
              <th>${t("CORE_ADMIN_MEDIA_STATE")}</th><th>${t("CORE_ADMIN_MEDIA_VIEWERS")}</th>
              <th>${t("CORE_ADMIN_MEDIA_REASON")}</th></tr></thead>
            <tbody>${sources.map(
              (s) => html`<tr data-source=${s.name} class="clickable ${s.name === this.selected ? "open" : ""} ${s.state === "error" ? "bad" : ""}"
                @click=${() => this.select(s.name)}>
                <td><input type="radio" name="source" value=${s.name} ?checked=${s.name === this.selected}
                  aria-label=${t("CORE_ADMIN_MEDIA_SELECT", { name: s.name })} @change=${() => this.select(s.name)}></td>
                <td>${s.name}</td><td>${known("CORE_ADMIN_MEDIA_KIND_", s.kind || "")}</td>
                <td>${stateText(s.state)}</td><td>${s.viewers}</td><td>${reasonText(s.reason)}</td>
              </tr>`,
            )}</tbody>
          </table>
          <div class="actions">
            ${actionRow({
              name: t("CORE_ADMIN_MEDIA_PREVIEW_NAME"),
              effect: t("CORE_ADMIN_MEDIA_PREVIEW_EFFECT"),
              label: previewing && src && previewing.source === src.name ? t("CORE_ADMIN_MEDIA_PREVIEW_STOP") : t("CORE_ADMIN_MEDIA_PREVIEW_LABEL"),
              id: "preview",
              reason,
              onClick: () => (previewing && src && previewing.source === src.name ? this.stopPreview() : this.startPreview()),
            })}
            ${actionRow({
              name: t("CORE_ADMIN_MEDIA_SHOW_NAME"),
              effect: t("CORE_ADMIN_MEDIA_SHOW_EFFECT"),
              label: t("CORE_ADMIN_MEDIA_SHOW_LABEL"),
              id: "show",
              reason: src ? null : t("CORE_ADMIN_MEDIA_REASON_NO_SOURCE"),
              onClick: () => this.show(),
            })}
            ${actionRow({
              name: t("CORE_ADMIN_MEDIA_STOP_NAME"),
              effect: t("CORE_ADMIN_MEDIA_STOP_EFFECT"),
              label: t("CORE_ADMIN_MEDIA_STOP_LABEL"),
              id: "stop",
              reason: takeover ? null : t("CORE_ADMIN_MEDIA_REASON_NO_TAKEOVER"),
              onClick: () => this.stop(),
            })}
          </div>
          ${previewing
            ? html`<div class="media-preview" data-preview=${previewing.source}>
                <h3>${previewing.source}</h3>
                <mm3-player src=${previewing.url} fit="contain" muted @mm3-media-error=${(ev) => this.playerError(ev)}></mm3-player>
              </div>`
            : nothing}`
        : html`<p class="muted">${t("CORE_ADMIN_MEDIA_NO_SOURCES")}</p>`}
    </section>`;
  }

  crumbs() {
    const parts = this.path.split("/");
    return html`<nav class="crumbs" aria-label=${t("CORE_ADMIN_MEDIA_PATH")}>${parts.map((part, i) => {
      const to = parts.slice(0, i + 1).join("/");
      return html`${i ? html`<span class="muted">/</span>` : nothing}<button type="button" class="link" data-path=${to}
        @click=${() => this.openFolder(to)}>${part}</button>`;
    })}</nav>`;
  }

  filesCard() {
    const readOnly = text(writeReason(this.path));
    const inShares = this.path.split("/")[0] === "shares";
    const progress = this.progress;
    const noFile = readOnly || (this.chosen ? null : t("CORE_ADMIN_MEDIA_REASON_NO_FILE"));
    return html`<section class="card media-files"><h2>${t("CORE_ADMIN_MEDIA_H_FILES")}</h2>
      <div class="toolbar">
        <button type="button" data-root="files" aria-pressed=${this.path.split("/")[0] === "files" ? "true" : "false"}
          @click=${() => this.openFolder("files")}>files</button>
        <button type="button" data-root="shares" aria-pressed=${inShares ? "true" : "false"}
          @click=${() => this.openFolder("shares")}>shares</button>
        ${this.crumbs()}
      </div>
      ${this.filesError
        ? banner("error", this.filesError, { label: t("CORE_ADMIN_RELOAD"), onClick: () => this.openFolder(this.path) })
        : nothing}
      ${this.entries.length
        ? html`<table class="entries">
            <thead><tr><th>${t("CORE_ADMIN_MEDIA_NAME")}</th><th>${t("CORE_ADMIN_MEDIA_KIND")}</th>
              <th>${t("CORE_ADMIN_MEDIA_SIZE")}</th><th>${t("CORE_ADMIN_MEDIA_MODIFIED")}</th><th></th></tr></thead>
            <tbody>${this.entries.map(
              (e) => html`<tr data-entry=${e.name}>
                <td>${e.kind === "folder"
                  ? html`<button type="button" class="link" data-open=${e.name}
                      @click=${() => this.openFolder(`${this.path}/${e.name}`)}>${e.name}</button>`
                  : e.name}</td>
                <td>${known("CORE_ADMIN_MEDIA_FILE_", e.kind)}</td>
                <td class="nowrap">${e.kind === "folder" ? "" : sizeText(e.size)}</td>
                <td class="nowrap">${fmtTime(e.modified)}</td>
                <td>${rowButton({ label: t("CORE_ADMIN_DELETE"), id: "delete", cls: "danger", reason: readOnly, onClick: () => this.removeEntry(e) })}</td>
              </tr>`,
            )}</tbody>
          </table>`
        : this.filesError
          ? nothing
          : html`<p class="muted empty">${t("CORE_ADMIN_MEDIA_FILES_EMPTY")}</p>`}
      <div class="actions">
        ${actionRow({
          name: t("CORE_ADMIN_MEDIA_UPLOAD_NAME"),
          effect: t("CORE_ADMIN_MEDIA_UPLOAD_EFFECT"),
          label: t("CORE_ADMIN_MEDIA_UPLOAD_LABEL"),
          id: "upload",
          reason: progress ? t("CORE_ADMIN_MEDIA_REASON_UPLOADING") : noFile,
          control: html`<input type="file" aria-label=${t("CORE_ADMIN_MEDIA_UPLOAD_NAME")} ?disabled=${!!readOnly || !!progress}
            @change=${(ev) => this.chooseFile(ev)}>`,
          onClick: () => this.sendFile(),
        })}
        ${progress
          ? html`<progress max=${progress.total} value=${progress.sent} aria-label=${t("CORE_ADMIN_MEDIA_UPLOAD_NAME")}></progress>
              <span class="muted">${t("CORE_ADMIN_MEDIA_UPLOAD_PROGRESS", {
                name: progress.name,
                percent: progress.total ? Math.floor((progress.sent * 100) / progress.total) : 100,
              })}</span>`
          : nothing}
        ${actionRow({
          name: t("CORE_ADMIN_MEDIA_FOLDER_NAME"),
          effect: t("CORE_ADMIN_MEDIA_FOLDER_EFFECT"),
          label: t("CORE_ADMIN_MEDIA_FOLDER_LABEL"),
          id: "folder",
          reason: readOnly,
          onClick: () => this.openFolderDialog(),
        })}
      </div>
    </section>`;
  }

  sharesCard() {
    const shares = this.shares || [];
    return html`<section class="card media-shares"><h2>${t("CORE_ADMIN_MEDIA_H_SHARES")}</h2>
      ${this.sharesError ? banner("warn", t("CORE_ADMIN_MEDIA_SHARES_UNAVAILABLE", { error: this.sharesError })) : nothing}
      ${shares.length
        ? html`<table class="shares">
            <thead><tr><th>${t("CORE_ADMIN_MEDIA_NAME")}</th><th>${t("CORE_ADMIN_MEDIA_METHOD")}</th>
              <th>${t("CORE_ADMIN_MEDIA_STATE")}</th><th>${t("CORE_ADMIN_MEDIA_LAST_SYNC")}</th>
              <th>${t("CORE_ADMIN_MEDIA_SIZE")}</th><th></th></tr></thead>
            <tbody>${shares.map(
              (s) => html`<tr data-share=${s.name} class=${s.state === "error" ? "bad" : ""}>
                <td>${s.name}</td><td>${known("CORE_ADMIN_MEDIA_METHOD_", s.method)}</td>
                <td>${known("CORE_ADMIN_MEDIA_SHARE_STATE_", s.state)}${s.error
                  ? html`<div class="muted share-error">${known("CORE_ADMIN_MEDIA_SHARE_ERROR_", s.error)}</div>`
                  : nothing}</td>
                <td class="nowrap">${s.last_sync ? fmtTime(s.last_sync) : ""}</td>
                <td class="nowrap">${s.method === "mount" ? "" : sizeText(s.size)}</td>
                <td>${rowButton({
                  label: t("CORE_ADMIN_MEDIA_SYNC_LABEL"),
                  id: "sync",
                  reason: text(syncReason(s, this.installer, this.sync)),
                  onClick: () => this.syncShare(s.name),
                })}</td>
              </tr>`,
            )}</tbody>
          </table>`
        : this.sharesError
          ? nothing
          : html`<p class="muted">${t("CORE_ADMIN_MEDIA_SHARES_NONE")}</p>`}
    </section>`;
  }

  dialogView() {
    const d = this.dialog;
    if (!d) return nothing;
    return html`<dialog class="media-dialog" aria-labelledby="media-dialog-title" @close=${() => this.dialog && this.closeDialog()}>
      ${d.kind === "folder" ? this.folderDialog(d) : this.takeoverDialog(d)}</dialog>`;
  }

  folderDialog(d) {
    return html`<form method="dialog" @submit=${(ev) => { ev.preventDefault(); this.submitFolder(); }}>
      <h2 id="media-dialog-title">${t("CORE_ADMIN_MEDIA_DLG_FOLDER_TITLE", { path: this.path })}</h2>
      ${d.error ? banner("error", d.error) : nothing}
      <label class="field">${t("CORE_ADMIN_MEDIA_DLG_FOLDER_NAME")}
        <input type="text" name="name" autocomplete="off" spellcheck="false" .value=${d.name}
          @input=${(ev) => { d.name = ev.target.value; }}></label>
      <div class="dialog-actions">
        <button type="button" class="cancel" @click=${() => this.closeDialog()}>${t("CORE_ADMIN_CANCEL")}</button>
        <button type="submit" class="primary confirm">${t("CORE_ADMIN_MEDIA_FOLDER_LABEL")}</button>
      </div>
    </form>`;
  }

  takeoverDialog(d) {
    const prefix = `${d.root}/`;
    return html`<form method="dialog" @submit=${(ev) => { ev.preventDefault(); this.startTakeover(d.source, d.file); }}>
      <h2 id="media-dialog-title">${t("CORE_ADMIN_MEDIA_DLG_FILE_TITLE", { source: d.source })}</h2>
      <p>${t("CORE_ADMIN_MEDIA_DLG_FILE_TEXT")}</p>
      ${d.error ? banner("error", d.error) : nothing}
      <div class="toolbar"><span class="mono">${d.dir}</span>
        ${d.dir !== d.root
          ? html`<button type="button" data-up @click=${() => this.listDialog(d.dir.split("/").slice(0, -1).join("/"))}>${t("CORE_ADMIN_MEDIA_DLG_FILE_UP")}</button>`
          : nothing}</div>
      ${d.entries.length
        ? html`<ul class="pick">${d.entries.map((e) =>
            e.kind === "folder"
              ? html`<li><button type="button" class="link" data-open=${e.name}
                  @click=${() => this.listDialog(`${d.dir}/${e.name}`)}>${e.name}/</button></li>`
              : html`<li><label class="inline"><input type="radio" name="file" value=${e.name}
                  ?checked=${d.file === `${d.dir}/${e.name}`.slice(prefix.length)}
                  @change=${() => { d.file = `${d.dir}/${e.name}`.slice(prefix.length); this.update(); }}> ${e.name}</label></li>`,
          )}</ul>`
        : html`<p class="muted">${t("CORE_ADMIN_MEDIA_DLG_FILE_EMPTY")}</p>`}
      <div class="dialog-actions">
        <button type="button" class="cancel" @click=${() => this.closeDialog()}>${t("CORE_ADMIN_CANCEL")}</button>
        <button type="submit" class="primary confirm" aria-disabled=${d.file ? "false" : "true"}
          title=${d.file ? nothing : t("CORE_ADMIN_MEDIA_REASON_NO_FILE")}
          @click=${(ev) => !d.file && ev.preventDefault()}>${t("CORE_ADMIN_MEDIA_DLG_FILE_SHOW")}</button>
      </div>
    </form>`;
  }

  update() {
    if (!this.messages) return;
    render(
      html`
        ${this.messages.view()}
        <h1>${t("CORE_ADMIN_MEDIA")}
          <button type="button" class="refresh" data-action="refresh" @click=${() => this.load()}>${t("CORE_ADMIN_MEDIA_REFRESH")}</button></h1>
        ${this.live ? nothing : banner("warn", t("CORE_ADMIN_RECONNECTING"))}
        ${this.error ? banner("error", this.error, { label: t("CORE_ADMIN_RELOAD"), onClick: () => this.load() }) : nothing}
        ${this.sourcesCard()}${this.filesCard()}${this.sharesCard()}
        ${this.dialogView()}
      `,
      this,
    );
    this.syncDialog();
  }

  /** Open or close the dialog element to match the state, after the page has rendered. */
  syncDialog() {
    const dialog = this.querySelector("dialog.media-dialog");
    if (!dialog) return;
    if (!dialog.dataset.backdrop) {
      dialog.dataset.backdrop = "1";
      closeOnBackdrop(dialog, () => this.closeDialog());
    }
    if (this.dialog && !dialog.open) dialog.showModal();
    if (!this.dialog && dialog.open) dialog.close();
  }
}

if (typeof customElements !== "undefined") customElements.define("mm3-admin-media", AdminMedia);
