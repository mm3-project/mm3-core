// The Backups page /admin/backups: make a backup, list, download, verify and restore the
// archives, upload one, make an age key and manage the rclone remotes, all through the
// installer service.
// Implements CORE-361 (Core spec 0.45, §10.8 Backup and restore): reads and writes the routes
// /api/v1/backups..., follows the `installer` topic of /admin/ws for the operations of action
// `backup` and `restore`; creating, listing, uploading, verifying, previewing, downloading and
// making a key need no re-authentication, a restore and changing or deleting a remote always
// ask for the passphrase and the TOTP code, which are sent once and never kept.
// Implements CORE-162, CORE-164, CORE-165 (Core spec 0.45, §10.8 Backup and restore): the
// restore dialog shows what the restore would change, the warnings of the archive, the parts
// to take and one instance.
// Implements CORE-161, CORE-364 (Core spec 0.45, §10.8 Backup and restore): secrets only in an
// encrypted archive; the age key is shown once.
// Implements CORE-365 (Core spec 0.45, §10.8 Backup and restore): the remotes and their test;
// secret parameters are shown masked and kept when sent back.
// Implements CORE-384, CORE-385, CORE-386, CORE-387 (Core spec 0.44, §9.1 Remote-control
// API): the dialogs, action rows and banners of the shared patterns in ui.js.
import { html, nothing, render } from "/admin/static/lit-html.js";
import { AdminChannel, ApiError, api, fmtTime, loadTexts, t, upload } from "/admin/static/admin.js";
import { Messages, actionRow, banner, closeOnBackdrop } from "/admin/static/ui.js";

/** The states in which an operation is over (CORE-355). */
export const FINAL_STATES = ["done", "rolled_back", "failed"];

/** The parts of a backup, in the order of the spec (CORE-161). */
export const PARTS = ["config", "data", "modules", "secrets"];

/** The largest archive the admin sends (CORE-361). */
export const MAX_UPLOAD = 2 * 1024 ** 3;

/** The remote types the installer accepts, with the parameters it knows (CORE-365). */
export const REMOTE_TYPES = {
  sftp: ["host", "port", "user", "pass", "key_pem"],
  smb: ["host", "port", "user", "pass", "domain"],
  webdav: ["url", "vendor", "user", "pass"],
  s3: ["provider", "access_key_id", "secret_access_key", "region", "endpoint"],
};

const MASKED = "!masked";
const upper = (s) => String(s).toUpperCase();

/** `true` when the operation has reached a final state. */
export const isFinal = (op) => !!op && FINAL_STATES.includes(op.state);

/** `true` for the operations this page shows: a backup or a restore. */
export const isBackupOp = (op) => !!op && (op.action === "backup" || op.action === "restore");

/** A size in bytes as KiB, MiB or GiB. */
export function sizeText(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 1024) return `${n} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = n;
  let i = -1;
  do {
    value /= 1024;
    i += 1;
  } while (value >= 1024 && i < units.length - 1);
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

/** The equivalent `mm3` command of an action (CORE-361): the alternative that needs no browser. */
export function cliCommand(action, name = null) {
  if (action === "backup") return "mm3 backup create";
  if (action === "restore") return `mm3 backup restore ${name || "<file>"}`;
  if (action === "verify") return `mm3 backup verify ${name || "<file>"}`;
  if (action === "keygen") return "mm3 backup keygen";
  return `mm3 backup ${action}`;
}

/**
 * Why a backup or restore cannot start now, as `{key, params}`, or null when it can
 * (CORE-386). `installer` is the answer of `GET /api/v1/installer`; `mine` is true while an
 * operation started here has not ended.
 */
export function reasonFor(installer, mine = false, now = Date.now()) {
  if (!installer) return { key: "CORE_ADMIN_BACKUPS_REASON_CHECKING", params: {} };
  if (!installer.available) return { key: "CORE_ADMIN_BACKUPS_REASON_AWAY", params: {} };
  const until = installer.locked_until ? Date.parse(installer.locked_until) : 0;
  if (until > now) {
    return { key: "CORE_ADMIN_BACKUPS_REASON_LOCKED", params: { time: fmtTime(installer.locked_until) } };
  }
  if (installer.busy || mine) return { key: "CORE_ADMIN_BACKUPS_REASON_BUSY", params: {} };
  return null;
}

/** The banner of an operation as `{kind, text}` (CORE-387). */
export function operationBanner(op) {
  const params = { action: t(`CORE_ADMIN_BACKUPS_ACTION_${upper(op.action)}`), name: op.to || op.name || "" };
  const text = (key, extra = {}) => t(key, { ...params, ...extra }).replace(/\s+([.:])/g, "$1").trim();
  if (op.state === "done") return { kind: "ok", text: text("CORE_ADMIN_BACKUPS_OP_DONE") };
  if (op.state === "rolled_back") return { kind: "warn", text: text("CORE_ADMIN_BACKUPS_OP_ROLLED_BACK", { error: op.error || "" }) };
  if (op.state === "failed") return { kind: "error", text: text("CORE_ADMIN_BACKUPS_OP_FAILED", { error: op.error || "" }) };
  return { kind: "info", text: text("CORE_ADMIN_BACKUPS_OP_RUNNING", { state: t(`CORE_ADMIN_BACKUPS_STATE_${upper(op.state)}`) }) };
}

/**
 * The text of a failed request: the catalog text of the problem code (CORE-298), with the
 * installer's own words for a refused request and the end of a lock (CORE-356).
 */
export function problemText(err) {
  if (!(err instanceof ApiError)) return String((err && err.message) || err);
  const p = err.problem || {};
  if (p.code === "CORE_API_INSTALLER_LOCKED" && p.locked_until) {
    return `${err.message} ${t("CORE_ADMIN_BACKUPS_LOCKED_UNTIL", { time: fmtTime(p.locked_until) })}`;
  }
  if (p.code === "CORE_API_INVALID_REQUEST" && p.detail) return `${err.message}: ${p.detail}`;
  return err.message;
}

/** Why a file cannot be uploaded, as `{key, params}`, or null (CORE-361). */
export function checkFile(file) {
  if (!file) return { key: "CORE_ADMIN_BACKUPS_UPLOAD_NONE", params: {} };
  if (!file.name.endsWith(".mm3backup")) return { key: "CORE_ADMIN_BACKUPS_UPLOAD_NAME", params: {} };
  if (!file.size) return { key: "CORE_ADMIN_BACKUPS_UPLOAD_EMPTY", params: {} };
  if (file.size > MAX_UPLOAD) return { key: "CORE_ADMIN_BACKUPS_UPLOAD_LARGE", params: { size: sizeText(MAX_UPLOAD) } };
  return null;
}

/**
 * The body of `POST /api/v1/backups`. `mode` is how the archive is encrypted: `none`,
 * `configured` (the key of the settings), `key` (an age public key) or `passphrase`.
 * Secrets are only taken when the archive is encrypted (CORE-161).
 */
export function createBody({ parts, mode, recipient = "", passphrase = "" }) {
  const body = {};
  const chosen = PARTS.filter((p) => parts.includes(p) && (p !== "secrets" || mode !== "none"));
  if (chosen.length) body.parts = chosen;
  if (mode === "configured") body.use_configured_key = true;
  if (mode === "key") body.recipient = recipient.trim();
  if (mode === "passphrase") body.archive_passphrase = passphrase;
  return body;
}

/** The `source` of a verify, preview or restore: an archive by name, or a staged upload. */
export const sourceOf = (what) => (what.upload ? { upload: what.upload } : what.remote ? { name: what.name, remote: true } : { name: what.name });

/** The body of `POST /api/v1/backups/restore` without the re-authentication. */
export function restoreBody({ source, parts, instance, key }) {
  const body = { source };
  if (parts && parts.length) body.parts = parts;
  if (instance && instance.trim()) body.instance = instance.trim();
  if (key && key.trim()) body.key = key.trim();
  return body;
}

/** The `key=value` lines of a remote's parameters; a masked secret is shown as `!masked`. */
export function formatParams(params) {
  return Object.entries(params || {})
    .map(([k, v]) => `${k}=${v && typeof v === "object" && MASKED in v ? MASKED : v}`)
    .join("\n");
}

/**
 * Read the `key=value` lines of a remote: `{params}` or `{error}` naming the first bad line.
 * `!masked` is sent back as the mask object, which keeps the stored secret (CORE-365).
 */
export function parseParams(text) {
  const params = {};
  let n = 0;
  for (const raw of String(text).split("\n")) {
    n += 1;
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const at = line.indexOf("=");
    const key = at < 1 ? "" : line.slice(0, at).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) || key in params) return { error: n };
    const value = line.slice(at + 1).trim();
    params[key] = value === MASKED ? { [MASKED]: null } : value;
  }
  return { params };
}

const none = () => html`<span class="muted">${t("CORE_ADMIN_BACKUPS_NONE")}</span>`;
const partsList = (parts) => (parts && parts.length ? parts.map((p) => html`<span class="tag">${t(`CORE_ADMIN_BACKUPS_PART_${upper(p)}`)}</span> `) : none());

/** The result of the last scheduled run or remote copy, in a line. */
function resultLine(label, result) {
  if (!result) return html`<dt>${label}</dt><dd>${none()}</dd>`;
  return html`<dt>${label}</dt><dd>${result.ok
    ? t("CORE_ADMIN_BACKUPS_RESULT_OK", { time: fmtTime(result.at), name: result.name || "" })
    : t("CORE_ADMIN_BACKUPS_RESULT_FAILED", { time: fmtTime(result.at), error: result.error || "" })}</dd>`;
}

/** The element of the page. */
class AdminBackups extends (globalThis.HTMLElement || class {}) {
  async connectedCallback() {
    await loadTexts();
    this.messages = new Messages(() => this.update());
    this.backups = [];
    this.status = {};
    this.remote = null; // the archives on the remote, once asked for
    this.remotes = [];
    this.installer = null; // {available, busy, locked_until}
    this.create = { parts: ["config", "data", "modules"], mode: "none", recipient: "", passphrase: "" };
    this.staged = null; // {upload, size, name}: an uploaded archive waiting for a restore
    this.progress = null; // {sent, total} while an upload runs
    this.dialog = null;
    this.op = null; // the operation shown
    this.mine = null; // the id of an operation started here that has not ended
    this.error = null;
    this.channel = new AdminChannel(
      (frame) => frame.kind === "installer" && this.onInstaller(frame.data),
      (on) => this.onChannel(on),
    );
    this.channel.subscribe("installer");
    await this.load();
  }

  disconnectedCallback() {
    if (this.channel) this.channel.close();
    if (this.sending && this.sending.abort) this.sending.abort();
  }

  /** Read the list, the installer and the remotes. */
  async load() {
    try {
      const data = await api("GET", "/backups");
      this.backups = data.backups;
      this.status = data.status || {};
      this.error = null;
    } catch (err) {
      this.error = problemText(err);
      this.backups = [];
    }
    try {
      const inst = await api("GET", "/installer");
      this.installer = inst;
    } catch {
      if (!this.installer) this.installer = { available: false, busy: false, locked_until: null };
    }
    try {
      this.remotes = (await api("GET", "/backups/remotes")).remotes;
    } catch {
      this.remotes = [];
    }
    this.update();
  }

  /** The archives on the remote (CORE-365); 422 when no remote is set. */
  async loadRemote() {
    try {
      this.remote = (await api("GET", "/backups/remote")).backups;
    } catch (err) {
      this.remote = null;
      this.messages.set("error", problemText(err));
    }
    this.update();
  }

  // --- the installer topic (CORE-356) --------------------------------------------------

  onInstaller({ status, operation }) {
    if (status) this.installer = status;
    if (operation && isBackupOp(operation)) this.follow(operation);
    this.update();
  }

  follow(op) {
    const tracked = this.op && this.op.id === op.id;
    if (!tracked && (isFinal(op) || (this.op && !isFinal(this.op)))) return;
    const was = this.op && this.op.state;
    this.op = op;
    if (isFinal(op)) {
      if (was !== op.state) this.ended(op);
    } else if (was !== op.state) {
      this.messages.set("info", operationBanner(op).text);
    }
  }

  ended(op) {
    if (this.mine === op.id) this.mine = null;
    const b = operationBanner(op);
    const dismiss = { label: t("CORE_ADMIN_DISMISS"), onClick: () => this.messages.clear() };
    this.messages.set(b.kind, b.text, b.kind === "ok" ? null : dismiss);
    this.load();
  }

  /** After the channel was lost (a restore restarts the engine), read the operation by id. */
  async onChannel(on) {
    if (!on || !this.op || isFinal(this.op)) return;
    try {
      this.follow(await api("GET", `/installer/operations/${encodeURIComponent(this.op.id)}`));
      this.update();
    } catch {
      // The engine is not back yet; the next connection tries again.
    }
  }

  /** An operation was accepted: show it as the one of this page. */
  started(id, action) {
    this.mine = id;
    this.op = { id, action, name: null, to: null, state: "queued", steps: [], error: null };
    this.messages.set("info", operationBanner(this.op).text);
    this.onChannel(true);
  }

  // --- making a backup ---------------------------------------------------------------

  async backup() {
    const c = this.create;
    if (c.mode === "key" && !c.recipient.trim()) return this.messages.set("error", t("CORE_ADMIN_BACKUPS_NEED_KEY"));
    if (c.mode === "passphrase" && !c.passphrase) return this.messages.set("error", t("CORE_ADMIN_BACKUPS_NEED_PASSPHRASE"));
    const body = createBody(c);
    c.passphrase = "";
    try {
      const { id } = await api("POST", "/backups", body);
      this.started(id, "backup");
    } catch (err) {
      this.messages.set("error", problemText(err));
    }
    this.update();
  }

  // --- uploading an archive ----------------------------------------------------------

  async chooseFile(file) {
    const bad = checkFile(file);
    if (bad) return this.messages.set("error", t(bad.key, bad.params));
    this.progress = { sent: 0, total: file.size };
    this.staged = null;
    this.update();
    const sending = upload("/backups/upload", file, (sent, total) => {
      this.progress = { sent, total };
      this.update();
    });
    this.sending = sending;
    try {
      const done = await sending;
      this.staged = { upload: done.upload, size: done.size, name: file.name };
      this.messages.set("ok", t("CORE_ADMIN_BACKUPS_UPLOADED", { name: file.name, size: sizeText(done.size) }));
    } catch (err) {
      this.messages.set("error", problemText(err));
    }
    this.sending = null;
    this.progress = null;
    this.update();
  }

  // --- the dialogs ----------------------------------------------------------------------

  closeDialog() {
    this.dialog = null;
    this.update();
  }

  /** Verify an archive; an encrypted one first asks for its key. */
  askVerify(what) {
    this.dialog = { kind: "verify", what, key: "", result: null, error: null, sending: false };
    this.update();
    this.inspect("verify");
  }

  /** Open the restore dialog and read what the restore would change. */
  askRestore(what) {
    this.dialog = { kind: "restore", what, key: "", info: null, parts: null, instance: "", error: null, sending: false };
    this.update();
    this.inspect("preview");
  }

  /** Read the archive: the summary (verify) or what a restore would change (preview). */
  async inspect(route) {
    const d = this.dialog;
    if (!d || d.sending) return;
    d.sending = true;
    d.error = null;
    this.update();
    const body = { source: sourceOf(d.what) };
    if (d.key.trim()) body.key = d.key.trim();
    if (route === "preview" && d.parts && d.parts.length) body.parts = d.parts;
    if (route === "preview" && d.instance.trim()) body.instance = d.instance.trim();
    try {
      const info = await api("POST", `/backups/${route}`, body);
      if (route === "verify") d.result = info;
      else {
        d.info = info;
        if (!d.parts) d.parts = [...info.parts];
      }
    } catch (err) {
      d.error = problemText(err);
    }
    d.sending = false;
    this.update();
  }

  /** Send the restore: the passphrase and the code go out once and are not kept (CORE-356). */
  async submitRestore(form) {
    const d = this.dialog;
    if (!d || d.sending) return;
    const body = restoreBody({ source: sourceOf(d.what), parts: d.parts, instance: d.instance, key: d.key });
    body.passphrase = form.passphrase.value;
    const code = form.totp.value.trim();
    if (code) body.totp = code;
    form.passphrase.value = "";
    form.totp.value = "";
    d.sending = true;
    d.error = null;
    this.update();
    try {
      const { id } = await api("POST", "/backups/restore", body);
      this.dialog = null;
      this.started(id, "restore");
    } catch (err) {
      d.sending = false;
      d.error = problemText(err);
    }
    this.update();
  }

  askKeygen() {
    this.dialog = { kind: "keygen", keep: false, result: null, error: null, sending: false };
    this.update();
  }

  /** Make an age key; the identity is in this answer only and is not kept by the page. */
  async keygen() {
    const d = this.dialog;
    if (!d || d.sending) return;
    d.sending = true;
    this.update();
    try {
      d.result = await api("POST", "/backups/keygen", { keep: d.keep });
    } catch (err) {
      d.error = problemText(err);
    }
    d.sending = false;
    this.update();
  }

  askRemote(remote = null) {
    this.dialog = {
      kind: "remote",
      name: remote ? remote.name : "",
      isNew: !remote,
      type: remote ? remote.type : "sftp",
      text: remote ? formatParams(remote.params) : "",
      error: null,
      sending: false,
    };
    this.update();
  }

  askRemoteDelete(remote) {
    this.dialog = { kind: "remote-delete", name: remote.name, error: null, sending: false };
    this.update();
  }

  /** Save or delete a remote after re-authentication (CORE-365). */
  async submitRemote(form) {
    const d = this.dialog;
    if (!d || d.sending) return;
    const auth = { passphrase: form.passphrase.value };
    const code = form.totp.value.trim();
    if (code) auth.totp = code;
    form.passphrase.value = "";
    form.totp.value = "";
    d.sending = true;
    d.error = null;
    this.update();
    try {
      if (d.kind === "remote-delete") {
        await api("DELETE", `/backups/remotes/${encodeURIComponent(d.name)}`, auth);
        this.messages.set("ok", t("CORE_ADMIN_BACKUPS_REMOTE_DELETED", { name: d.name }));
      } else {
        const parsed = parseParams(d.text);
        if (parsed.error) {
          d.sending = false;
          d.error = t("CORE_ADMIN_BACKUPS_REMOTE_BAD_LINE", { line: parsed.error });
          return this.update();
        }
        await api("PUT", `/backups/remotes/${encodeURIComponent(d.name)}`, { type: d.type, params: parsed.params, ...auth });
        this.messages.set("ok", t("CORE_ADMIN_BACKUPS_REMOTE_SAVED", { name: d.name }));
      }
      this.dialog = null;
      await this.load();
    } catch (err) {
      d.sending = false;
      d.error = problemText(err);
    }
    this.update();
  }

  /** Check that a remote answers; no re-authentication (CORE-365). */
  async testRemote(remote) {
    try {
      await api("POST", `/backups/remotes/${encodeURIComponent(remote.name)}/test`, {});
      this.messages.set("ok", t("CORE_ADMIN_BACKUPS_REMOTE_TEST_OK", { name: remote.name }));
    } catch (err) {
      this.messages.set("error", t("CORE_ADMIN_BACKUPS_REMOTE_TEST_FAILED", { name: remote.name, error: problemText(err) }));
    }
  }

  // --- views ------------------------------------------------------------------------

  reason() {
    const r = reasonFor(this.installer, this.mine !== null);
    return r ? t(r.key, r.params) : null;
  }

  encryptionFields() {
    const c = this.create;
    return html`<fieldset class="encryption"><legend>${t("CORE_ADMIN_BACKUPS_ENCRYPTION")}</legend>
      ${["none", "configured", "key", "passphrase"].map(
        (m) => html`<label class="inline"><input type="radio" name="mode" value=${m} ?checked=${c.mode === m}
          @change=${() => { c.mode = m; if (m === "none") c.parts = c.parts.filter((p) => p !== "secrets"); this.update(); }}>
          ${t(`CORE_ADMIN_BACKUPS_MODE_${upper(m)}`)}</label>`,
      )}
      ${c.mode === "key"
        ? html`<label class="field">${t("CORE_ADMIN_BACKUPS_RECIPIENT")}
            <input type="text" name="recipient" autocomplete="off" spellcheck="false" .value=${c.recipient}
              @input=${(ev) => { c.recipient = ev.target.value; }}></label>`
        : nothing}
      ${c.mode === "passphrase"
        ? html`<label class="field">${t("CORE_ADMIN_BACKUPS_ARCHIVE_PASSPHRASE")}
            <input type="password" name="archive_passphrase" autocomplete="off" .value=${c.passphrase}
              @input=${(ev) => { c.passphrase = ev.target.value; }}></label>`
        : nothing}
    </fieldset>`;
  }

  createCard() {
    const c = this.create;
    const reason = this.reason();
    return html`<section class="card" aria-labelledby="create-title">
      <h2 id="create-title">${t("CORE_ADMIN_BACKUPS_CREATE")}</h2>
      <fieldset class="parts"><legend>${t("CORE_ADMIN_BACKUPS_PARTS")}</legend>
        ${PARTS.map((p) => {
          const locked = p === "secrets" && c.mode === "none";
          return html`<label class="inline" title=${locked ? t("CORE_ADMIN_BACKUPS_SECRETS_NOTE") : nothing}>
            <input type="checkbox" name="part" value=${p} ?checked=${c.parts.includes(p)} ?disabled=${locked}
              @change=${(ev) => { c.parts = ev.target.checked ? [...c.parts, p] : c.parts.filter((x) => x !== p); this.update(); }}>
            ${t(`CORE_ADMIN_BACKUPS_PART_${upper(p)}`)}</label>`;
        })}
        ${c.mode === "none" ? html`<p class="muted">${t("CORE_ADMIN_BACKUPS_SECRETS_NOTE")}</p>` : nothing}
      </fieldset>
      ${this.encryptionFields()}
      ${actionRow({
        name: t("CORE_ADMIN_BACKUPS_ACTION_BACKUP"),
        effect: t("CORE_ADMIN_BACKUPS_CREATE_EFFECT"),
        label: t("CORE_ADMIN_BACKUPS_CREATE_NOW"),
        reason: reason || (c.parts.length ? null : t("CORE_ADMIN_BACKUPS_NO_PARTS")),
        id: "backup",
        cls: "primary",
        onClick: () => this.backup(),
        control: html`<code class="cli">${cliCommand("backup")}</code>`,
      })}
    </section>`;
  }

  statusCard() {
    const s = this.status || {};
    return html`<section class="card" aria-labelledby="status-title">
      <h2 id="status-title">${t("CORE_ADMIN_BACKUPS_STATUS")}</h2>
      <dl class="facts">
        ${resultLine(t("CORE_ADMIN_BACKUPS_LAST_SCHEDULED"), s.scheduled)}
        ${resultLine(t("CORE_ADMIN_BACKUPS_LAST_REMOTE"), s.remote)}
      </dl>
    </section>`;
  }

  rowActions(entry, remote) {
    const what = { name: entry.name, remote };
    const reason = this.reason();
    return html`<td class="row-actions">
      ${remote ? nothing : html`<a class="button" data-action="download" href=${`/api/v1/backups/${encodeURIComponent(entry.name)}`} download=${entry.name}>${t("CORE_ADMIN_BACKUPS_DOWNLOAD")}</a>`}
      <button type="button" data-action="verify" @click=${() => this.askVerify(what)}>${t("CORE_ADMIN_BACKUPS_VERIFY")}</button>
      <button type="button" data-action="restore" class="danger" aria-disabled=${reason ? "true" : "false"} title=${reason || nothing}
        @click=${(ev) => (reason ? ev.preventDefault() : this.askRestore(what))}>${t("CORE_ADMIN_BACKUPS_RESTORE")}</button>
    </td>`;
  }

  table(entries, remote) {
    if (!entries.length) return html`<p class="muted">${t("CORE_ADMIN_BACKUPS_EMPTY")}</p>`;
    return html`<table class="backup-list">
      <thead><tr>
        <th>${t("CORE_ADMIN_BACKUPS_COL_CREATED")}</th><th>${t("CORE_ADMIN_BACKUPS_COL_KIND")}</th>
        <th>${t("CORE_ADMIN_BACKUPS_COL_HOST")}</th><th>${t("CORE_ADMIN_BACKUPS_COL_PARTS")}</th>
        <th>${t("CORE_ADMIN_BACKUPS_COL_SIZE")}</th><th>${t("CORE_ADMIN_BACKUPS_COL_ENCRYPTED")}</th><th></th>
      </tr></thead>
      <tbody>${entries.map(
        (e) => html`<tr data-name=${e.name}>
          <td title=${e.name}>${e.created ? fmtTime(e.created) : e.name}</td>
          <td>${e.kind ? t(`CORE_ADMIN_BACKUPS_KIND_${upper(e.kind)}`) : nothing}</td>
          <td>${e.hostname || nothing}</td><td>${partsList(e.parts)}</td>
          <td>${e.size !== undefined ? sizeText(e.size) : nothing}</td>
          <td>${e.encrypted ? t("CORE_ADMIN_BACKUPS_YES") : t("CORE_ADMIN_BACKUPS_NO")}</td>
          ${this.rowActions(e, remote)}
        </tr>`,
      )}</tbody></table>`;
  }

  listCard() {
    return html`<section class="card" aria-labelledby="list-title">
      <h2 id="list-title">${t("CORE_ADMIN_BACKUPS_LIST")}</h2>
      ${this.table(this.backups, false)}
    </section>`;
  }

  remoteArchivesCard() {
    return html`<section class="card" aria-labelledby="remote-title">
      <h2 id="remote-title">${t("CORE_ADMIN_BACKUPS_ON_REMOTE")}</h2>
      ${this.remote === null
        ? html`<p class="muted">${t("CORE_ADMIN_BACKUPS_ON_REMOTE_TEXT")}</p>
            <button type="button" data-action="show-remote" @click=${() => this.loadRemote()}>${t("CORE_ADMIN_BACKUPS_SHOW_REMOTE")}</button>`
        : html`${this.table(this.remote, true)}
            <button type="button" data-action="show-remote" @click=${() => this.loadRemote()}>${t("CORE_ADMIN_BACKUPS_RELOAD")}</button>`}
    </section>`;
  }

  uploadCard() {
    const p = this.progress;
    const s = this.staged;
    const reason = this.reason();
    return html`<section class="card" aria-labelledby="upload-title">
      <h2 id="upload-title">${t("CORE_ADMIN_BACKUPS_UPLOAD")}</h2>
      <p class="muted">${t("CORE_ADMIN_BACKUPS_UPLOAD_TEXT", { size: sizeText(MAX_UPLOAD) })}</p>
      <label class="field">${t("CORE_ADMIN_BACKUPS_UPLOAD_FILE")}
        <input type="file" name="archive" accept=".mm3backup" ?disabled=${!!p}
          @change=${(ev) => { const f = ev.target.files[0]; ev.target.value = ""; this.chooseFile(f); }}></label>
      ${p ? html`<progress max=${p.total} value=${p.sent}></progress> <span class="muted">${sizeText(p.sent)} / ${sizeText(p.total)}</span>` : nothing}
      ${s
        ? html`<div class="staged">
            <p>${t("CORE_ADMIN_BACKUPS_STAGED", { name: s.name, size: sizeText(s.size) })}</p>
            <button type="button" data-action="verify-upload" @click=${() => this.askVerify({ upload: s.upload })}>${t("CORE_ADMIN_BACKUPS_VERIFY")}</button>
            <button type="button" data-action="restore-upload" class="danger" aria-disabled=${reason ? "true" : "false"} title=${reason || nothing}
              @click=${(ev) => (reason ? ev.preventDefault() : this.askRestore({ upload: s.upload }))}>${t("CORE_ADMIN_BACKUPS_RESTORE")}</button>
          </div>`
        : nothing}
    </section>`;
  }

  keysCard() {
    return html`<section class="card" aria-labelledby="keys-title">
      <h2 id="keys-title">${t("CORE_ADMIN_BACKUPS_KEYS")}</h2>
      ${actionRow({
        name: t("CORE_ADMIN_BACKUPS_KEYGEN"),
        effect: t("CORE_ADMIN_BACKUPS_KEYGEN_EFFECT"),
        label: t("CORE_ADMIN_BACKUPS_KEYGEN"),
        id: "keygen",
        onClick: () => this.askKeygen(),
        control: html`<code class="cli">${cliCommand("keygen")}</code>`,
      })}
    </section>`;
  }

  remotesCard() {
    return html`<section class="card" aria-labelledby="remotes-title">
      <h2 id="remotes-title">${t("CORE_ADMIN_BACKUPS_REMOTES")}</h2>
      ${this.remotes.length
        ? html`<table class="remote-list">
            <thead><tr><th>${t("CORE_ADMIN_BACKUPS_REMOTE_NAME")}</th><th>${t("CORE_ADMIN_BACKUPS_REMOTE_TYPE")}</th><th></th></tr></thead>
            <tbody>${this.remotes.map(
              (r) => html`<tr data-remote=${r.name}><td class="mono">${r.name}</td><td>${r.type}</td>
                <td class="row-actions">
                  <button type="button" data-action="test-remote" @click=${() => this.testRemote(r)}>${t("CORE_ADMIN_BACKUPS_REMOTE_TEST")}</button>
                  <button type="button" data-action="edit-remote" @click=${() => this.askRemote(r)}>${t("CORE_ADMIN_BACKUPS_REMOTE_EDIT")}</button>
                  <button type="button" data-action="delete-remote" class="danger" @click=${() => this.askRemoteDelete(r)}>${t("CORE_ADMIN_BACKUPS_REMOTE_DELETE")}</button>
                </td></tr>`,
            )}</tbody></table>`
        : html`<p class="muted">${t("CORE_ADMIN_BACKUPS_REMOTES_EMPTY")}</p>`}
      <button type="button" data-action="add-remote" @click=${() => this.askRemote()}>${t("CORE_ADMIN_BACKUPS_REMOTE_ADD")}</button>
      <p class="muted">${t("CORE_ADMIN_BACKUPS_REMOTES_NOTE")}</p>
    </section>`;
  }

  operationCard() {
    const op = this.op;
    if (!op) return nothing;
    return html`<section class="card operation" aria-labelledby="operation-title">
      <h2 id="operation-title">${t("CORE_ADMIN_BACKUPS_OPERATION")}</h2>
      <p>${operationBanner(op).text}</p>
      ${(op.steps || []).length
        ? html`<ol class="steps">${op.steps.map(
            (s) => html`<li><span class="mono">${fmtTime(s.at)}</span> ${t(`CORE_ADMIN_BACKUPS_STATE_${upper(s.state)}`)}${s.detail ? html` — ${s.detail}` : nothing}</li>`,
          )}</ol>`
        : nothing}
    </section>`;
  }

  // --- dialogs --------------------------------------------------------------------------

  keyField(d, run) {
    return html`<label class="field">${t("CORE_ADMIN_BACKUPS_KEY")}
      <input type="password" name="key" autocomplete="off" spellcheck="false" .value=${d.key}
        @input=${(ev) => { d.key = ev.target.value; }}
        @keydown=${(ev) => { if (ev.key === "Enter") { ev.preventDefault(); run(); } }}></label>
      <p class="muted">${t("CORE_ADMIN_BACKUPS_KEY_NOTE")}</p>`;
  }

  reauthFields() {
    return html`<label class="field">${t("CORE_ADMIN_BACKUPS_PASSPHRASE")}
        <input type="password" name="passphrase" autocomplete="off" required autofocus></label>
      <label class="field">${t("CORE_ADMIN_BACKUPS_TOTP")}
        <input type="text" name="totp" autocomplete="off" inputmode="numeric" pattern="[0-9 ]*"></label>`;
  }

  actions(cancelFirst, confirm) {
    return html`<div class="dialog-actions">
      <button type="button" class="cancel" @click=${() => this.closeDialog()}>${cancelFirst}</button>
      ${confirm}
    </div>`;
  }

  whatName(what) {
    return what.upload ? t("CORE_ADMIN_BACKUPS_THE_UPLOAD") : what.name;
  }

  verifyDialog(d) {
    const r = d.result;
    return html`<form method="dialog" @submit=${(ev) => { ev.preventDefault(); this.inspect("verify"); }}>
      <h2 id="backups-dialog-title">${t("CORE_ADMIN_BACKUPS_DLG_VERIFY_TITLE", { name: this.whatName(d.what) })}</h2>
      ${d.error ? banner("error", d.error) : nothing}
      ${r
        ? html`${banner(r.ok ? "ok" : "error", r.ok ? t("CORE_ADMIN_BACKUPS_VERIFY_OK") : t("CORE_ADMIN_BACKUPS_VERIFY_BAD"))}
            ${this.summary(r)}`
        : html`<p class="muted">${d.sending ? "…" : t("CORE_ADMIN_BACKUPS_DLG_VERIFY_TEXT")}</p>`}
      ${r ? nothing : this.keyField(d, () => this.inspect("verify"))}
      ${this.actions(
        t("CORE_ADMIN_CLOSE"),
        r ? nothing : html`<button type="submit" class="primary confirm" aria-disabled=${d.sending ? "true" : "false"}>${t("CORE_ADMIN_BACKUPS_VERIFY")}</button>`,
      )}
    </form>`;
  }

  summary(r) {
    return html`<dl class="facts">
      <dt>${t("CORE_ADMIN_BACKUPS_COL_CREATED")}</dt><dd>${fmtTime(r.created)}</dd>
      <dt>${t("CORE_ADMIN_BACKUPS_COL_HOST")}</dt><dd>${r.hostname}</dd>
      <dt>${t("CORE_ADMIN_BACKUPS_COL_KIND")}</dt><dd>${t(`CORE_ADMIN_BACKUPS_KIND_${upper(r.kind)}`)}</dd>
      <dt>${t("CORE_ADMIN_BACKUPS_ENGINE")}</dt><dd class="mono">${r.engine_version}</dd>
      <dt>${t("CORE_ADMIN_BACKUPS_COL_PARTS")}</dt><dd>${partsList(r.parts)}</dd>
      ${r.omitted && r.omitted.length ? html`<dt>${t("CORE_ADMIN_BACKUPS_OMITTED")}</dt><dd>${partsList(r.omitted)}</dd>` : nothing}
      <dt>${t("CORE_ADMIN_BACKUPS_FILES")}</dt><dd>${r.files}</dd>
      <dt>${t("CORE_ADMIN_BACKUPS_COL_ENCRYPTED")}</dt><dd>${r.encrypted ? t("CORE_ADMIN_BACKUPS_YES") : t("CORE_ADMIN_BACKUPS_NO")}</dd>
    </dl>
    ${(r.warnings || []).map((w) => banner("warn", w))}`;
  }

  restoreDialog(d) {
    const info = d.info;
    const reason = this.reason();
    return html`<form method="dialog" @submit=${(ev) => { ev.preventDefault(); this.submitRestore(ev.target); }}>
      <h2 id="backups-dialog-title">${t("CORE_ADMIN_BACKUPS_DLG_RESTORE_TITLE", { name: this.whatName(d.what) })}</h2>
      <p>${t("CORE_ADMIN_BACKUPS_DLG_RESTORE_TEXT")}</p>
      ${d.error ? banner("error", d.error) : nothing}
      ${info
        ? html`${this.summary(info)}
            <fieldset class="parts"><legend>${t("CORE_ADMIN_BACKUPS_RESTORE_PARTS")}</legend>
              ${info.parts.map(
                (p) => html`<label class="inline"><input type="checkbox" name="part" value=${p} ?checked=${(d.parts || []).includes(p)}
                  @change=${(ev) => { d.parts = ev.target.checked ? [...d.parts, p] : d.parts.filter((x) => x !== p); this.inspect("preview"); }}>
                  ${t(`CORE_ADMIN_BACKUPS_PART_${upper(p)}`)}</label>`,
              )}</fieldset>
            <label class="field">${t("CORE_ADMIN_BACKUPS_INSTANCE")}
              <input type="text" name="instance" autocomplete="off" .value=${d.instance} @input=${(ev) => { d.instance = ev.target.value; }}
                @change=${() => this.inspect("preview")}></label>
            <h3>${t("CORE_ADMIN_BACKUPS_WOULD_CHANGE")}</h3>
            ${(info.lines || []).length
              ? html`<ul class="preview">${info.lines.map((line) => html`<li>${line}</li>`)}</ul>`
              : html`<p class="muted">${t("CORE_ADMIN_BACKUPS_NOTHING_TO_CHANGE")}</p>`}`
        : html`<p class="muted">${d.sending ? "…" : t("CORE_ADMIN_BACKUPS_DLG_RESTORE_KEY")}</p>
            ${this.keyField(d, () => this.inspect("preview"))}
            <button type="button" data-action="preview" @click=${() => this.inspect("preview")}>${t("CORE_ADMIN_BACKUPS_PREVIEW")}</button>`}
      ${info ? this.reauthFields() : nothing}
      <p class="muted">${t("CORE_ADMIN_BACKUPS_CLI_ALTERNATIVE")} <code class="cli">${cliCommand("restore", d.what.name)}</code></p>
      ${this.actions(
        t("CORE_ADMIN_CANCEL"),
        html`<button type="submit" class="danger confirm" aria-disabled=${d.sending || !info || reason ? "true" : "false"} title=${reason || nothing}>${t("CORE_ADMIN_BACKUPS_RESTORE")}</button>`,
      )}
    </form>`;
  }

  keygenDialog(d) {
    const r = d.result;
    return html`<form method="dialog" @submit=${(ev) => { ev.preventDefault(); this.keygen(); }}>
      <h2 id="backups-dialog-title">${t("CORE_ADMIN_BACKUPS_DLG_KEYGEN_TITLE")}</h2>
      ${d.error ? banner("error", d.error) : nothing}
      ${r
        ? html`${banner("warn", t("CORE_ADMIN_BACKUPS_IDENTITY_ONCE"))}
            <label class="field">${t("CORE_ADMIN_BACKUPS_PUBLIC_KEY")}
              <textarea readonly rows="2" name="public" spellcheck="false">${r.public}</textarea></label>
            <label class="field">${t("CORE_ADMIN_BACKUPS_IDENTITY")}
              <textarea readonly rows="3" name="identity" spellcheck="false">${r.identity}</textarea></label>
            ${r.kept ? html`<p class="muted">${t("CORE_ADMIN_BACKUPS_KEPT")}</p>` : nothing}`
        : html`<p>${t("CORE_ADMIN_BACKUPS_DLG_KEYGEN_TEXT")}</p>
            <label class="inline"><input type="checkbox" name="keep" ?checked=${d.keep} @change=${(ev) => { d.keep = ev.target.checked; this.update(); }}>
              ${t("CORE_ADMIN_BACKUPS_KEEP_KEY")}</label>`}
      ${this.actions(
        r ? t("CORE_ADMIN_CLOSE") : t("CORE_ADMIN_CANCEL"),
        r ? nothing : html`<button type="submit" class="primary confirm" aria-disabled=${d.sending ? "true" : "false"}>${t("CORE_ADMIN_BACKUPS_KEYGEN")}</button>`,
      )}
    </form>`;
  }

  remoteDialog(d) {
    const deleting = d.kind === "remote-delete";
    return html`<form method="dialog" @submit=${(ev) => { ev.preventDefault(); this.submitRemote(ev.target); }}>
      <h2 id="backups-dialog-title">${deleting
        ? t("CORE_ADMIN_BACKUPS_DLG_REMOTE_DELETE_TITLE", { name: d.name })
        : d.isNew ? t("CORE_ADMIN_BACKUPS_DLG_REMOTE_ADD_TITLE") : t("CORE_ADMIN_BACKUPS_DLG_REMOTE_EDIT_TITLE", { name: d.name })}</h2>
      ${deleting ? html`<p>${t("CORE_ADMIN_BACKUPS_DLG_REMOTE_DELETE_TEXT")}</p>` : nothing}
      ${d.error ? banner("error", d.error) : nothing}
      ${deleting
        ? nothing
        : html`<label class="field">${t("CORE_ADMIN_BACKUPS_REMOTE_NAME")}
              <input type="text" name="name" autocomplete="off" required pattern="[A-Za-z0-9_][A-Za-z0-9_.\\-]*" maxlength="64"
                ?readonly=${!d.isNew} .value=${d.name} @input=${(ev) => { d.name = ev.target.value; }}></label>
            <label class="field">${t("CORE_ADMIN_BACKUPS_REMOTE_TYPE")}
              <select name="type" @change=${(ev) => { d.type = ev.target.value; this.update(); }}>
                ${Object.keys(REMOTE_TYPES).map((k) => html`<option value=${k} ?selected=${k === d.type}>${k}</option>`)}
              </select></label>
            <label class="field">${t("CORE_ADMIN_BACKUPS_REMOTE_PARAMS")}
              <textarea name="params" rows="6" spellcheck="false" autocomplete="off" .value=${d.text}
                @input=${(ev) => { d.text = ev.target.value; }}></textarea></label>
            <p class="muted">${t("CORE_ADMIN_BACKUPS_REMOTE_PARAMS_NOTE", { names: (REMOTE_TYPES[d.type] || []).join(", ") })}</p>`}
      ${this.reauthFields()}
      ${this.actions(
        t("CORE_ADMIN_CANCEL"),
        html`<button type="submit" class="${deleting ? "danger" : "primary"} confirm" aria-disabled=${d.sending ? "true" : "false"}>${deleting ? t("CORE_ADMIN_BACKUPS_REMOTE_DELETE") : t("CORE_ADMIN_BACKUPS_SAVE")}</button>`,
      )}
    </form>`;
  }

  dialogView() {
    const d = this.dialog;
    if (!d) return html`<dialog class="backups-dialog"></dialog>`;
    const body = { verify: () => this.verifyDialog(d), restore: () => this.restoreDialog(d), keygen: () => this.keygenDialog(d) }[d.kind] || (() => this.remoteDialog(d));
    return html`<dialog class="backups-dialog" aria-labelledby="backups-dialog-title" @close=${() => this.dialog && this.closeDialog()}>${body()}</dialog>`;
  }

  update() {
    const inst = this.installer;
    render(
      html`
        ${this.messages.view()}
        <h1>${t("CORE_ADMIN_BACKUPS")}</h1>
        ${this.error ? banner("error", this.error, { label: t("CORE_ADMIN_BACKUPS_RELOAD"), onClick: () => this.load() }) : nothing}
        ${inst && !inst.available ? banner("warn", t("CORE_ADMIN_BACKUPS_REASON_AWAY")) : nothing}
        ${inst && inst.available && inst.locked_until && Date.parse(inst.locked_until) > Date.now()
          ? banner("warn", t("CORE_ADMIN_BACKUPS_REASON_LOCKED", { time: fmtTime(inst.locked_until) }))
          : nothing}
        <div class="split backups">
          <div>${this.listCard()}${this.remoteArchivesCard()}</div>
          <div>${this.createCard()}${this.operationCard()}${this.statusCard()}${this.uploadCard()}${this.keysCard()}${this.remotesCard()}</div>
        </div>
        ${this.dialogView()}
      `,
      this,
    );
    this.syncDialog();
  }

  /** Open or close the dialog element to match the state, after the page has rendered. */
  syncDialog() {
    const dialog = this.querySelector("dialog.backups-dialog");
    if (!dialog) return;
    if (!dialog.dataset.backdrop) {
      dialog.dataset.backdrop = "1";
      closeOnBackdrop(dialog, () => this.closeDialog());
    }
    if (this.dialog && !dialog.open) dialog.showModal();
    if (!this.dialog && dialog.open) dialog.close();
  }
}

if (typeof customElements !== "undefined") customElements.define("mm3-admin-backups", AdminBackups);
