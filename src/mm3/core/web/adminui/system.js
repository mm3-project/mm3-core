// The System card of the admin home: restart the engine or the kiosk, reboot the system.
// Implements CORE-383 (Core spec 0.42, §9.1 Remote-control API): three buttons with a
// confirmation dialog, disabled while the installer is unreachable or busy, a banner after
// Confirm that ends when the status shows the effect or after 120 s.
// Implements CORE-382 (Core spec 0.42, §9.1 Remote-control API): the routes
// POST /api/v1/system/<action> it calls.
// Implements CORE-386 (Core spec 0.43, §9.1 Remote-control API): the rows of the card.
// Implements CORE-387 (Core spec 0.43, §9.1 Remote-control API): its banners.
import { html, nothing } from "/admin/static/lit-html.js";
import { ApiError, api, t } from "/admin/static/admin.js";
import { actionRow, banner, closeOnBackdrop } from "/admin/static/ui.js";

/** The three actions in card order: route name, catalog key stem. */
export const ACTIONS = [
  ["restart-engine", "RESTART_ENGINE"],
  ["restart-kiosk", "RESTART_KIOSK"],
  ["reboot", "REBOOT"],
];

/** Seconds after which the banner gives up waiting for the effect (CORE-383). */
export const EFFECT_WAIT_MS = 120_000;
const POLL_MS = 10_000;
const SEEN_SHOWN_MS = 8_000;

const stem = (action) => ACTIONS.find(([name]) => name === action)[1];
const label = (action) => t(`CORE_ADMIN_SYSTEM_${stem(action)}`);

/**
 * State and behaviour of the System card. `onChange` is called after every change so
 * the page can render again; `observe(live, status)` is fed with the shared status.
 */
export class SystemControls {
  constructor(onChange) {
    this.onChange = onChange;
    this.installer = null; // {available, busy} of GET /api/v1/installer
    this.confirming = null; // the action whose dialog is open
    this.pending = null; // {action, before, sawDown, outcome} while a banner shows
    this.error = null; // the text of a 409 or 503 answer: no banner (CORE-383)
    this.timers = new Set();
    this.status = null;
    this.live = false;
  }

  start() {
    this.refresh();
    this.poll = setInterval(() => this.refresh(), POLL_MS);
  }

  stop() {
    clearInterval(this.poll);
    for (const timer of this.timers) clearTimeout(timer);
    this.timers.clear();
  }

  later(fn, ms) {
    const timer = setTimeout(() => {
      this.timers.delete(timer);
      fn();
    }, ms);
    this.timers.add(timer);
  }

  async refresh() {
    try {
      this.installer = await api("GET", "/installer");
    } catch {
      // The engine is restarting, or the request failed: not available now.
      this.installer = { available: false, busy: false };
    }
    this.onChange();
  }

  /** Why the buttons are disabled, or null when they are not (CORE-383). */
  get reason() {
    const info = this.installer;
    if (!info) return t("CORE_ADMIN_SYSTEM_CHECKING");
    if (!info.available) return t("CORE_ADMIN_SYSTEM_INSTALLER_UNAVAILABLE");
    if (info.busy || (this.pending && !this.pending.outcome)) return t("CORE_ADMIN_SYSTEM_INSTALLER_BUSY");
    return null;
  }

  ask(action) {
    if (this.reason) return;
    this.confirming = action;
    this.error = null;
    this.onChange();
  }

  cancel() {
    this.confirming = null;
    this.onChange();
  }

  /** Confirm: send the request; a 202 starts the banner, an error shows its text (CORE-383). */
  async confirm() {
    const action = this.confirming;
    if (!action) return;
    this.confirming = null;
    this.error = null;
    const status = this.status;
    const before = {
      startedAt: status && status.started_at ? Date.parse(status.started_at) : null,
      kioskConnected: status && status.kiosk ? status.kiosk.connected : null,
    };
    try {
      await api("POST", `/system/${action}`);
    } catch (err) {
      this.error = err instanceof ApiError ? err.message : String(err.message || err);
      this.refresh();
      return;
    }
    const pending = { action, before, sawDown: before.kioskConnected === false, outcome: null };
    this.pending = pending;
    this.later(() => this.expire(pending), EFFECT_WAIT_MS);
    this.refresh();
  }

  /** The 120 s are over without the effect: say so (CORE-383). */
  expire(pending) {
    if (this.pending !== pending || pending.outcome) return;
    pending.outcome = "unseen";
    this.onChange();
  }

  dismiss() {
    this.pending = null;
    this.error = null;
    this.onChange();
  }

  /**
   * Look at the status for the effect of the pending action (CORE-383): for the engine and
   * the reboot a `started_at` later than before the request, for the kiosk `kiosk.connected`
   * true after it was false.
   */
  observe(live, status) {
    this.live = live;
    if (status) this.status = status;
    const p = this.pending;
    if (!p || p.outcome) return;
    if (!live || !status) {
      p.sawDown = p.sawDown || p.action !== "restart-kiosk";
      return;
    }
    let seen = false;
    if (p.action === "restart-kiosk") {
      const connected = status.kiosk ? status.kiosk.connected : null;
      if (connected === false) p.sawDown = true;
      seen = connected === true && p.sawDown;
    } else if (status.started_at) {
      const started = Date.parse(status.started_at);
      seen = p.before.startedAt === null ? p.sawDown : started > p.before.startedAt;
    }
    if (!seen) return;
    p.outcome = "seen";
    this.refresh();
    this.later(() => {
      if (this.pending === p) this.dismiss();
    }, SEEN_SHOWN_MS);
  }
}

/** The banner of the pending action, or of an error answer (CORE-383). */
export function systemBanner(sc) {
  const dismiss = { label: t("CORE_ADMIN_DISMISS"), onClick: () => sc.dismiss() };
  if (sc.error) return banner("error", sc.error, dismiss);
  const p = sc.pending;
  if (!p) return nothing;
  const name = stem(p.action);
  if (p.outcome === "seen") return banner("ok", t(`CORE_ADMIN_SYSTEM_SEEN_${name}`));
  if (p.outcome === "unseen") return banner("warn", t("CORE_ADMIN_SYSTEM_UNSEEN", { action: label(p.action) }), dismiss);
  return banner("info", t(`CORE_ADMIN_SYSTEM_PENDING_${name}`));
}

/** The System card: one row per action with its effect and its button (CORE-383, CORE-386). */
export function systemCard(sc) {
  const reason = sc.reason;
  return html`
    <section class="card system" aria-labelledby="system-title">
      <h2 id="system-title">${t("CORE_ADMIN_SYSTEM")}</h2>
      ${ACTIONS.map(([action, key]) =>
        actionRow({
          name: t(`CORE_ADMIN_SYSTEM_${key}`),
          effect: t(`CORE_ADMIN_SYSTEM_${key}_EFFECT`),
          label: t(`CORE_ADMIN_SYSTEM_${key}`),
          reason,
          id: action,
          onClick: () => sc.ask(action),
        }),
      )}
    </section>`;
}

/** The confirmation dialog of the action being asked; Cancel has the focus (CORE-383). */
export function systemDialog(sc) {
  const action = sc.confirming;
  if (!action) return html`<dialog class="system-confirm"></dialog>`;
  const key = stem(action);
  return html`<dialog class="system-confirm" aria-labelledby="system-confirm-title" @close=${() => sc.cancel()}>
    <h2 id="system-confirm-title">${t(`CORE_ADMIN_SYSTEM_${key}_TITLE`)}</h2>
    <p>${t(`CORE_ADMIN_SYSTEM_${key}_CONFIRM_TEXT`)}</p>
    <div class="dialog-actions">
      <button type="button" class="cancel" autofocus @click=${() => sc.cancel()}>${t("CORE_ADMIN_CANCEL")}</button>
      <button type="button" class=${action === "reboot" ? "danger confirm" : "primary confirm"} @click=${() => sc.confirm()}>
        ${t(`CORE_ADMIN_SYSTEM_${key}`)}
      </button>
    </div>
  </dialog>`;
}

/** Open or close the dialog element to match the state, after the page has rendered. */
export function syncDialog(root, sc) {
  const dialog = root.querySelector("dialog.system-confirm");
  if (!dialog) return;
  if (!dialog.dataset.backdrop) {
    dialog.dataset.backdrop = "1";
    closeOnBackdrop(dialog, () => sc.cancel());
  }
  if (sc.confirming && !dialog.open) dialog.showModal();
  if (!sc.confirming && dialog.open) dialog.close();
}
