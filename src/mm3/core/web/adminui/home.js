// The admin home /admin: the status view of the engine, live from the admin channel.
// Implements CORE-378 (Core spec 0.40, §10.1 Security): /admin with a session is the
// admin home, which shows the status.
// Implements CORE-381 (Core spec 0.41, §9.1 Remote-control API): the status view,
// updated from the `status` topic.
import { html, nothing, render } from "/admin/static/lit-html.js";
import { loadTexts, onStatus, t } from "/admin/static/admin.js";
import { statusAlerts, statusCards } from "/admin/static/status.js";

class AdminHome extends HTMLElement {
  async connectedCallback() {
    await loadTexts();
    this.stop = onStatus((state) => {
      this.state = state;
      this.update();
    });
  }

  disconnectedCallback() {
    if (this.stop) this.stop();
  }

  update() {
    const { live, status } = this.state;
    render(
      html`
        <h1>${t("CORE_ADMIN_STATUS")}</h1>
        ${live ? nothing : html`<p class="alert warn" role="status">${t("CORE_ADMIN_RECONNECTING")}</p>`}
        ${status
          ? html`${statusAlerts(status).map((a) => html`<p class="alert" role="alert">${a}</p>`)}
              ${statusCards(status)}`
          : html`<p class="muted">…</p>`}
      `,
      this,
    );
  }
}

customElements.define("mm3-admin-home", AdminHome);
