// Implements CORE-289 (Core spec 0.19, §9.4 Reference modules)
// Implements CORE-409, CORE-413 (Core spec 0.53, §9.7 Media service): media_demo plays one
// URL or YouTube video under the greeting, a test of the media elements before ctx.media
import { MirrorElement, html, nothing } from "/core/mm3.js";

const KEYS = { morning: "GOOD_MORNING", afternoon: "GOOD_AFTERNOON", evening: "GOOD_EVENING" };
const MEDIA_ERRORS = {
  unavailable: "MEDIA_UNAVAILABLE",
  forbidden: "MEDIA_FORBIDDEN",
  network: "MEDIA_NETWORK",
  decode: "MEDIA_DECODE",
  unsupported: "MEDIA_UNSUPPORTED",
};

class GreetingElement extends MirrorElement {
  constructor() {
    super();
    this.mediaError = null;
    this.shadow.addEventListener("mm3-media-error", (ev) => {
      this.mediaError = MEDIA_ERRORS[ev.detail && ev.detail.reason] || "MEDIA_UNAVAILABLE";
      this.requestRender();
    });
    this.shadow.addEventListener("mm3-media-playing", () => {
      this.mediaError = null;
      this.requestRender();
    });
  }

  render(state) {
    const name = this.config.name;
    const greeting = state.part
      ? html`<p class="greeting">${this.t(name ? `${KEYS[state.part]}_NAME` : KEYS[state.part], { name })}</p>`
      : nothing;
    return html`${greeting}${this.renderMedia(this.config.media_demo)}`;
  }

  renderMedia(demo) {
    if (!demo || (!demo.src && !demo.youtube)) return nothing;
    const player = demo.youtube
      ? html`<mm3-youtube video-id=${demo.youtube} ?loop=${demo.loop} ?muted=${demo.muted}></mm3-youtube>`
      : html`<mm3-player src=${demo.src} fit=${demo.fit} ?loop=${demo.loop} ?muted=${demo.muted}></mm3-player>`;
    const error = this.mediaError ? html`<p class="media-error">${this.t(this.mediaError)}</p>` : nothing;
    return html`<div class="media">${player}${error}</div>`;
  }

  // MED-06 / MOD-27: no stream while hidden, so the engine can stop its child.
  onSuspend() {
    for (const el of this.shadow.querySelectorAll("mm3-player, mm3-youtube")) el.stop();
  }

  onResume() {
    for (const el of this.shadow.querySelectorAll("mm3-player, mm3-youtube")) el.start();
  }
}
customElements.define("mm3-greeting", GreetingElement);
