// Implements MED-02, MED-03, MED-04, MED-05, MED-06, MED-07 (Core spec 0.57, §9.4 Reference modules)
// Implements MOD-102 (Module Contract 1.0 rev 16, §4.5 Media): media is shown only through
// mm3-player and mm3-youtube with the URLs the backend gives in the state; this file sets no
// media URL of its own and stops its players when it is suspended.
import { MirrorElement, html, nothing } from "/core/mm3.js";

// The reasons of `mm3-media-error` (core CORE-409, CORE-413) and their catalog keys (MED-04).
const REASONS = {
  unavailable: "MEDIA_ERROR_UNAVAILABLE",
  forbidden: "MEDIA_ERROR_FORBIDDEN",
  network: "MEDIA_ERROR_NETWORK",
  decode: "MEDIA_ERROR_DECODE",
  unsupported: "MEDIA_ERROR_UNSUPPORTED",
};

// Two templates that differ only in a class: the slides alternate between them, so each new
// slide is a new node and the CSS animation of its class fades it in (MED-03).
const slideA = (src, fit, role) => html`<div class="slide a ${role}">
  <mm3-player class="media" src=${src} fit=${fit}></mm3-player>
</div>`;
const slideB = (src, fit, role) => html`<div class="slide b ${role}">
  <mm3-player class="media" src=${src} fit=${fit}></mm3-player>
</div>`;

class MediaElement extends MirrorElement {
  constructor() {
    super();
    this._reason = null; // the last `mm3-media-error` reason, until the player plays again
    this._stopped = false; // between onSuspend and onResume
  }

  connectedCallback() {
    // CSSOM, not a style attribute: the CSP forbids inline styles
    this.style.setProperty("--media-opacity", String(this.config.opacity ?? 1));
  }

  // --- MED-06: nothing plays while the instance is hidden or suspended ------------------

  _players() {
    return [...this.shadow.querySelectorAll("mm3-player, mm3-youtube")];
  }

  onSuspend() {
    this._stopped = true;
    for (const player of this._players()) player.stop();
  }

  onResume() {
    this._stopped = false;
    for (const player of this._players()) player.start();
  }

  // --- MED-04: errors of the player and of the backend show a message --------------------

  _onError(event) {
    const reason = event.detail && event.detail.reason;
    this._reason = Object.hasOwn(REASONS, reason) ? reason : "unavailable";
    this.requestRender();
  }

  _onPlaying() {
    if (this._reason === null) return;
    this._reason = null;
    this.requestRender();
  }

  _playerMessage(reason) {
    if (reason === "forbidden" && this.config.kind === "youtube") return this.t("MEDIA_GRANT_YOUTUBE");
    return this.t(REASONS[reason]);
  }

  _stateMessage(state) {
    if (state.error === "grant") return this.t("MEDIA_GRANT_MISSING", { source: state.source ?? "" });
    if (state.error === "empty") return this.t("MEDIA_NO_IMAGES");
    return this.t("MEDIA_ERROR_UNAVAILABLE");
  }

  _notice(text) {
    return html`<div class="notice" role="status">${text}</div>`;
  }

  // --- MED-05: a touch on the cell asks the backend for a takeover -----------------------

  _touch() {
    this.send("touch");
  }

  // --- MED-02, MED-03: what is shown ----------------------------------------------------

  _slide(src, n, role) {
    const fit = this.config.fit;
    return n % 2 ? slideA(src, fit, role) : slideB(src, fit, role);
  }

  _slides(state) {
    const layers = [];
    if (state.previous) layers.push(this._slide(state.previous, state.seq - 1, "previous"));
    layers.push(this._slide(state.src, state.seq, "current"));
    return html`<div class="layers slides ${this.config.transition}">${layers}</div>`;
  }

  _layers(state) {
    const c = this.config;
    if (c.kind === "youtube") {
      return html`<div class="layers"><mm3-youtube class="media" video-id=${c.video_id} ?loop=${c.loop} ?muted=${c.muted}></mm3-youtube></div>`;
    }
    if (c.kind === "slideshow") return this._slides(state);
    return html`<div class="layers"><mm3-player class="media" src=${state.src} fit=${c.fit} ?loop=${c.loop} ?muted=${c.muted}></mm3-player></div>`;
  }

  render() {
    const state = this.state;
    if (!state.kind) return this._notice(this.t("MEDIA_LOADING")); // MOD-36: the empty state
    if (state.error) return this._notice(this._stateMessage(state));
    // A player made while suspended starts by itself: stop it again (MED-06).
    if (this._stopped) this.setTimeout(() => this.onSuspend(), 0);
    const failed = this._reason !== null;
    const touch = this.config.on_touch === "takeover";
    return html`<div
      class="box ${failed ? "failed" : ""} ${touch ? "touch" : ""}"
      @mm3-media-error=${(e) => this._onError(e)}
      @mm3-media-playing=${() => this._onPlaying()}
      @click=${touch ? () => this._touch() : nothing}
    >
      ${this._layers(state)} ${failed ? this._notice(this._playerMessage(this._reason)) : nothing}
    </div>`;
  }
}

customElements.define("mm3-media", MediaElement);
