// The admin home /admin: the status view of the engine, live from the admin channel.
// Implements CORE-378 (Core spec 0.40, §10.1 Security): /admin with a session is the
// admin home, which shows the status.
// Implements CORE-381 (Core spec 0.41, §9.1 Remote-control API): the status view,
// updated from the `status` topic.
// Implements CORE-383 (Core spec 0.42, §9.1 Remote-control API): the System card.
// Implements CORE-387 (Core spec 0.43, §9.1 Remote-control API): its banners.
// Implements CORE-392 (Core spec 0.47, §6.11 Camera devices): the camera images warning,
// from GET /api/v1/devices, read again when the running config version changes.
import { html, nothing, render } from "/admin/static/lit-html.js";
import { api, loadTexts, onStatus, t } from "/admin/static/admin.js";
import { imagesOn, imagesWarning } from "/admin/static/devices.js";
import { statusAlerts, statusCards } from "/admin/static/status.js";
import { banner } from "/admin/static/ui.js";
import { SystemControls, syncDialog, systemBanner, systemCard, systemDialog } from "/admin/static/system.js";

class AdminHome extends HTMLElement {
  async connectedCallback() {
    await loadTexts();
    this.state = { live: false, status: null };
    this.images = false;
    this.devicesFor = undefined;
    this.system = new SystemControls(() => this.update());
    this.system.start();
    this.stop = onStatus((state) => {
      this.state = state;
      this.system.observe(state.live, state.status);
      this.devices(state.status);
      this.update();
    });
  }

  /** Read the devices again when the running config version changes (CORE-392). */
  async devices(status) {
    const version = status && status.config ? status.config.version : null;
    if (!status || version === this.devicesFor) return;
    this.devicesFor = version;
    try {
      this.images = imagesOn(await api("GET", "/devices"));
    } catch {
      this.devicesFor = undefined;
      return;
    }
    this.update();
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
        ${live ? nothing : banner("warn", t("CORE_ADMIN_RECONNECTING"))}
        ${imagesWarning(this.images)}
        ${status
          ? html`${statusAlerts(status).map((a) => banner("error", a))}
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
