// The status view shared by the admin home and the debug overview: the object of
// GET /api/v1/status as alerts and cards.
// Implements CORE-305 (Core spec 0.22, §9.1 Remote-control API): the admin home and the
// debug overview show the status, mismatches and failures highlighted.
// Implements CORE-381 (Core spec 0.41, §9.1 Remote-control API): the MQTT bridge with
// `since` and `error` beside the engine, the clients and the kiosk.
import { html, nothing } from "/admin/static/lit-html.js";
import { MQTT_KEYS, fmtDuration, fmtTime, mqttState, t } from "/admin/static/admin.js";

/** Texts of what is wrong in a status object: highlighted by the page (CORE-305). */
export function statusAlerts(s) {
  const out = [];
  if (s.config && s.config.disk_valid === false) out.push(t("CORE_ADMIN_DISK_INVALID"));
  const k = s.kiosk;
  if (k) {
    if (!k.connected) out.push(t("CORE_ADMIN_KIOSK_DISCONNECTED"));
    if (k.display && k.display.mismatch) out.push(t("CORE_ADMIN_DISPLAY_MISMATCH"));
    if (k.chromium && k.chromium.failed) out.push(t("CORE_ADMIN_CHROMIUM_FAILED"));
    if (k.audio && k.audio.mismatch) out.push(t("CORE_ADMIN_AUDIO_MISMATCH"));
    if (k.audio && k.audio.failure) out.push(`${t("CORE_ADMIN_AUDIO_FAILURE")}: ${k.audio.failure}`);
  }
  if (s.mqtt && !s.mqtt.connected && s.mqtt.error) {
    out.push(`${t("CORE_ADMIN_MQTT_ERROR")}: ${s.mqtt.error}`);
  }
  return out;
}

/** The engine, clients, kiosk and MQTT cards of a status object. */
export function statusCards(s) {
  const k = s.kiosk;
  const stat = (key, value) => html`<dt>${t(key)}</dt><dd>${value}</dd>`;
  const state = mqttState(s.mqtt);
  return html`
    <div class="grid">
      <section class="card"><h2>${t("CORE_ADMIN_ENGINE")}</h2>
        <dl class="stat">
          ${stat("CORE_ADMIN_VERSION", `${s.version} (${s.engine_build})`)}
          ${stat("CORE_ADMIN_UPTIME", fmtDuration(s.uptime_s))}
          ${stat("CORE_ADMIN_CONFIG_VERSION", s.config ? s.config.version : "–")}
          ${stat("CORE_ADMIN_PAGE", `${s.page.id} (${s.page.index + 1}/${s.page.count})`)}
          ${s.suspended ? stat("CORE_ADMIN_SUSPENDED", "✓") : nothing}
        </dl>
        <a class="button" href="/api/v1/debug/bundle" download>${t("CORE_ADMIN_DOWNLOAD_BUNDLE")}</a>
      </section>
      <section class="card"><h2>${t("CORE_ADMIN_CLIENTS")}</h2>
        ${s.clients.length
          ? html`<table><tbody>${s.clients.map(
              (c) => html`<tr><td>${c.role}</td><td class="mono">${c.address || ""}</td><td>${fmtTime(c.since)}</td></tr>`,
            )}</tbody></table>`
          : html`<p class="muted">${t("CORE_ADMIN_NONE")}</p>`}
      </section>
      <section class="card"><h2>${t("CORE_ADMIN_KIOSK")}</h2>
        ${k
          ? html`<dl class="stat">
              ${stat("CORE_ADMIN_STATE", k.connected ? t("CORE_ADMIN_LIVE") : t("CORE_ADMIN_KIOSK_DISCONNECTED"))}
              ${k.display && k.display.viewport ? stat("CORE_ADMIN_VIEWPORT", k.display.viewport.join(" × ")) : nothing}
              ${k.chromium ? stat("CORE_ADMIN_CHROMIUM", `${k.chromium.state || "–"} · ${k.chromium.restarts}`) : nothing}
            </dl>`
          : html`<p class="muted">${t("CORE_ADMIN_KIOSK_NONE")}</p>`}
      </section>
      <section class="card mqtt" data-state=${state}><h2>${t("CORE_ADMIN_MQTT")}</h2>
        <dl class="stat">
          ${stat("CORE_ADMIN_STATE", t(MQTT_KEYS[state]))}
          ${s.mqtt && s.mqtt.since ? stat("CORE_ADMIN_MQTT_SINCE_LABEL", fmtTime(s.mqtt.since)) : nothing}
          ${s.mqtt && s.mqtt.error ? stat("CORE_ADMIN_MQTT_ERROR_LABEL", s.mqtt.error) : nothing}
        </dl>
      </section>
    </div>`;
}
