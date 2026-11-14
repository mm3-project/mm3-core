// Shared pieces of the admin pages: banners, action rows and confirmation dialogs.
// Implements CORE-384 (Core spec 0.43, §9.1 Remote-control API): the confirmation dialog.
// Implements CORE-385 (Core spec 0.43, §9.1 Remote-control API): the footer of form and
// preview dialogs.
// Implements CORE-386 (Core spec 0.43, §9.1 Remote-control API): action rows.
// Implements CORE-387 (Core spec 0.43, §9.1 Remote-control API): banners of four kinds.
import { html, nothing, render } from "/admin/static/lit-html.js";
import { t } from "/admin/static/admin.js";

/** The four kinds of banner and their ARIA role (CORE-387). */
export const BANNER_ROLES = { error: "alert", warn: "status", info: "status", ok: "status" };

/** Seconds after which a done banner goes away (CORE-387). */
export const OK_SHOWN_MS = 8000;

/**
 * One banner: a line of text and at most one button aligned right (CORE-387).
 * `action` is `{label, onClick}`.
 */
export function banner(kind, content, action = null, cls = "") {
  const role = BANNER_ROLES[kind] || "alert";
  return html`<div class="alert ${kind === "error" ? "" : kind} ${cls}" role=${role}><span class="text">${content}</span>
    ${action ? html`<button type="button" class="link" @click=${action.onClick}>${action.label}</button>` : nothing}</div>`;
}

/**
 * The message line of a page: one banner at a time. A done banner clears itself after
 * 8 s; the others stay until `clear()` or a new message (CORE-387). `onChange` is called
 * after every change so the page can render again.
 */
export class Messages {
  constructor(onChange) {
    this.onChange = onChange;
    this.current = null; // {kind, text, action}
    this.timer = null;
  }

  set(kind, text, action = null) {
    clearTimeout(this.timer);
    this.current = text ? { kind, text, action } : null;
    if (this.current && kind === "ok") {
      this.timer = setTimeout(() => this.clear(), OK_SHOWN_MS);
    }
    this.onChange();
  }

  clear() {
    clearTimeout(this.timer);
    if (!this.current) return;
    this.current = null;
    this.onChange();
  }

  view() {
    const m = this.current;
    return m ? banner(m.kind, m.text, m.action) : nothing;
  }
}

/**
 * One action row: name, one sentence on its effect and a button aligned right. A disabled
 * button has `aria-disabled`, shows the reason as tooltip and in the row, and does nothing
 * (CORE-386).
 */
export function actionRow({ name, effect, label, onClick, reason = null, cls = "", id = null, control = nothing }) {
  return html`<div class="action-row">
    <div class="text">
      <div class="name">${name}</div>
      <div class="muted">${effect}</div>
      ${reason ? html`<div class="muted reason">${reason}</div>` : nothing}
    </div>
    <div class="controls">${control}
      <button type="button" class=${cls || nothing} data-action=${id || nothing}
        aria-disabled=${reason ? "true" : "false"} title=${reason || nothing}
        @click=${(ev) => (reason ? ev.preventDefault() : onClick(ev))}>${label}</button>
    </div>
  </div>`;
}

/** Close a dialog on a click outside its box, as Cancel (CORE-384). */
export function closeOnBackdrop(dialog, close) {
  dialog.addEventListener("click", (ev) => {
    if (ev.target !== dialog) return;
    const r = dialog.getBoundingClientRect();
    const inside = ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom;
    if (!inside) close();
  });
}

/**
 * Ask for a confirmation in a dialog of the shape of CORE-384: a heading that asks the
 * question, one sentence, an optional preview (a lit-html template), Cancel with the focus
 * and then the action, red only when `danger`. Resolves true on the action, false on
 * Cancel, Escape or a click outside. Never uses the native confirm.
 */
export function confirmDialog({ title, text, preview = nothing, action, danger = false }) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "confirm";
    dialog.setAttribute("aria-labelledby", "confirm-title");
    let answer = false;
    const finish = (value) => {
      answer = value;
      dialog.close();
    };
    render(
      html`<h2 id="confirm-title">${title}</h2>
        <p>${text}</p>
        ${preview}
        <div class="dialog-actions">
          <button type="button" class="cancel" autofocus @click=${() => finish(false)}>${t("CORE_ADMIN_CANCEL")}</button>
          <button type="button" class=${danger ? "danger confirm" : "primary confirm"} @click=${() => finish(true)}>${action}</button>
        </div>`,
      dialog,
    );
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(answer);
    });
    closeOnBackdrop(dialog, () => finish(false));
    document.body.append(dialog);
    dialog.showModal();
  });
}
