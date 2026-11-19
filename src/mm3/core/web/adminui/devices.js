// Camera devices in the admin (core spec 6.11): the camera images warning and the list of
// devices with their owners.
// Implements CORE-392 (Core spec 0.47, §6.11 Camera devices): while any device has
// `images: true`, a warning banner (CORE-387) says that every client admitted by
// server.allow can see the images.
// Implements CORE-393 (Core spec 0.47, §6.11 Camera devices): the debug overview lists each
// device with its owner, from GET /api/v1/devices.
import { html, nothing } from "/admin/static/lit-html.js";
import { t } from "/admin/static/admin.js";
import { banner } from "/admin/static/ui.js";

/** True when any device has `images: true`: a list from the API or the `devices` map of a draft. */
export function imagesOn(devices) {
  if (!devices || typeof devices !== "object") return false;
  const entries = Array.isArray(devices) ? devices : Object.values(devices);
  return entries.some((d) => d && typeof d === "object" && d.images === true);
}

/** The warning banner of CORE-392, or nothing. */
export function imagesWarning(on) {
  return on ? banner("warn", t("CORE_ADMIN_DEVICES_IMAGES_WARNING"), null, "devices-images") : nothing;
}

/** The devices card of the debug overview: name, kind, device, owner and why, present, images. */
export function devicesCard(devices) {
  const rows = devices || [];
  return html`<section class="card devices"><h2>${t("CORE_ADMIN_DEVICES")}</h2>
    ${rows.length
      ? html`<table>
          <thead><tr>
            <th>${t("CORE_ADMIN_DEVICE")}</th><th>${t("CORE_ADMIN_DEVICE_KIND")}</th>
            <th>${t("CORE_ADMIN_DEVICE_ID")}</th><th>${t("CORE_ADMIN_DEVICE_OWNER")}</th>
            <th>${t("CORE_ADMIN_DEVICE_PRESENT")}</th><th>${t("CORE_ADMIN_DEVICE_IMAGES")}</th>
          </tr></thead>
          <tbody>${rows.map(
            (d) => html`<tr data-device=${d.name}>
              <td>${d.name}</td><td>${d.kind}</td>
              <td class="mono">${d.path || d.serial || t("CORE_ADMIN_DEVICE_FIRST_FOUND")}</td>
              <td>${d.owner} <span class="muted">(${t(d.reason === "auto" ? "CORE_ADMIN_DEVICE_OWNER_AUTO" : "CORE_ADMIN_DEVICE_OWNER_EXPLICIT")})</span></td>
              <td>${d.present === null ? "–" : t(d.present ? "CORE_ADMIN_DEVICE_YES" : "CORE_ADMIN_DEVICE_NO")}</td>
              <td>${t(d.images ? "CORE_ADMIN_DEVICE_YES" : "CORE_ADMIN_DEVICE_NO")}</td>
            </tr>`,
          )}</tbody>
        </table>`
      : html`<p class="muted">${t("CORE_ADMIN_NONE")}</p>`}
  </section>`;
}
