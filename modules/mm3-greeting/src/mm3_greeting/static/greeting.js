// Implements CORE-289 (Core spec 0.19, §9.4 Reference modules)
import { MirrorElement, html } from "/core/mm3.js";

const KEYS = { morning: "GOOD_MORNING", afternoon: "GOOD_AFTERNOON", evening: "GOOD_EVENING" };

class GreetingElement extends MirrorElement {
  render(state) {
    if (!state.part) return html``;
    const name = this.config.name;
    const key = name ? `${KEYS[state.part]}_NAME` : KEYS[state.part];
    return html`<p class="greeting">${this.t(key, { name })}</p>`;
  }
}
customElements.define("mm3-greeting", GreetingElement);
