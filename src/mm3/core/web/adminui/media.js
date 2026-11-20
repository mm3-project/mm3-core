// The media service in the admin debug overview (core spec 9.7).
// Implements CORE-412 (Core spec 0.51, §9.7 Media service): the overview shows each source with
// its state, viewers and reason, and the running children, from GET /api/v1/media.
// Implements CORE-401 (Core spec 0.48, §9.7 Media service): a source that is not playable shows
// the reason `codec`.
// Implements CORE-402 (Core spec 0.52, §9.7 Media service): a camera source shows the reasons
// `feed` (waiting for the owner's feed) and `owner` (owned by nobody).
// Implements CORE-410 (Core spec 0.54, §9.7 Media service): the running takeover, its source
// and its end, above the sources.
import { html, nothing } from "/admin/static/lit-html.js";
import { fmtTime, t } from "/admin/static/admin.js";

const STATES = {
  idle: "CORE_ADMIN_MEDIA_STATE_IDLE",
  running: "CORE_ADMIN_MEDIA_STATE_RUNNING",
  error: "CORE_ADMIN_MEDIA_STATE_ERROR",
  unavailable: "CORE_ADMIN_MEDIA_STATE_UNAVAILABLE",
};

const REASONS = {
  codec: "CORE_ADMIN_MEDIA_REASON_CODEC",
  failed: "CORE_ADMIN_MEDIA_REASON_FAILED",
  ffmpeg: "CORE_ADMIN_MEDIA_REASON_FFMPEG",
  feed: "CORE_ADMIN_MEDIA_REASON_FEED",
  owner: "CORE_ADMIN_MEDIA_REASON_OWNER",
  restarting: "CORE_ADMIN_MEDIA_REASON_RESTARTING",
};

function reason(value) {
  if (!value) return nothing;
  return REASONS[value] ? t(REASONS[value]) : value;
}

/** The media card of the debug overview: sources, then the running children. */
export function mediaCard(media) {
  const sources = (media && media.sources) || [];
  const children = (media && media.children) || [];
  const takeover = media && media.takeover;
  return html`<section class="card media"><h2>${t("CORE_ADMIN_MEDIA")}</h2>
    ${takeover
      ? html`<p class="takeover" data-takeover=${takeover.id}>${t("CORE_ADMIN_MEDIA_TAKEOVER", {
          source: takeover.file ? `${takeover.source} / ${takeover.file}` : takeover.source,
          until: fmtTime(takeover.until),
        })}</p>`
      : nothing}
    ${sources.length
      ? html`<table>
          <thead><tr>
            <th>${t("CORE_ADMIN_MEDIA_SOURCE")}</th><th>${t("CORE_ADMIN_MEDIA_STATE")}</th>
            <th>${t("CORE_ADMIN_MEDIA_VIEWERS")}</th><th>${t("CORE_ADMIN_MEDIA_REASON")}</th>
          </tr></thead>
          <tbody>${sources.map(
            (s) => html`<tr data-source=${s.name} class=${s.state === "error" ? "bad" : ""}>
              <td>${s.name}</td><td>${STATES[s.state] ? t(STATES[s.state]) : s.state}</td>
              <td>${s.viewers}</td><td>${reason(s.reason)}</td>
            </tr>`,
          )}</tbody>
        </table>`
      : html`<p class="muted">${t("CORE_ADMIN_NONE")}</p>`}
    ${children.length
      ? html`<h3>${t("CORE_ADMIN_MEDIA_CHILDREN")}</h3><table>
          <thead><tr>
            <th>${t("CORE_ADMIN_MEDIA_SOURCE")}</th><th>${t("CORE_ADMIN_MEDIA_PID")}</th>
            <th>${t("CORE_ADMIN_MEDIA_TRANSCODE")}</th><th>${t("CORE_ADMIN_MEDIA_STARTED")}</th>
          </tr></thead>
          <tbody>${children.map(
            (c) => html`<tr data-child=${c.source}>
              <td>${c.source}</td><td class="mono">${c.pid}</td>
              <td>${t(c.transcode ? "CORE_ADMIN_DEVICE_YES" : "CORE_ADMIN_DEVICE_NO")}</td>
              <td class="nowrap">${fmtTime(c.started)}</td>
            </tr>`,
          )}</tbody>
        </table>`
      : nothing}
  </section>`;
}
