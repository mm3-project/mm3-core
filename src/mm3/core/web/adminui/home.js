// The admin home /admin: the status view of the engine, live from the admin channel.
// Implements CORE-378 (Core spec 0.40, §10.1 Security): /admin with a session is the
// admin home, which shows the status.
// Implements CORE-381 (Core spec 0.41, §9.1 Remote-control API): the status view,
// updated from the `status` topic.
// Implements CORE-383 (Core spec 0.42, §9.1 Remote-control API): the System card.
import { html, nothing, render } from "/admin/static/lit-html.js";
import { loadTexts, onStatus, t } from "/admin/static/admin.js";
import { statusAlerts, statusCards } from "/admin/static/status.js";
import { SystemControls, syncDialog, systemBanner, systemCard, systemDialog } from "/admin/static/system.js";

class AdminHome extends HTMLElement {
  async connectedCallback() {
    await loadTexts();
    this.state = { live: false, status: null };
    this.system = new SystemControls(() => this.update());
    this.system.start();
    this.stop = onStatus((state) => {
      this.state = state;
      this.system.observe(state.live, state.status);
      this.update();
    });
  }

  disconnectedCallback() {
    if (this.stop) this.stop();
    if (this.system) this.system.stop();
  }

  update() {
    const { live, status } = this.state;
    render(
      html`
        ${systemBanner(this.system)}
        <h1>${t("CORE_ADMIN_STATUS")}</h1>
        ${live ? nothing : html`<p class="alert warn" role="status">${t("CORE_ADMIN_RECONNECTING")}</p>`}
        ${status
          ? html`${statusAlerts(status).map((a) => html`<p class="alert" role="alert">${a}</p>`)}
              ${statusCards(status)}`
          : html`<p class="muted">…</p>`}
        ${systemCard(this.system)}
        ${systemDialog(this.system)}
      `,
      this,
    );
    syncDialog(this, this.system);
  }
}

customElements.define("mm3-admin-home", AdminHome);
