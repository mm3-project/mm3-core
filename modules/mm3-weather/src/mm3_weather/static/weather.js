// Implements WTH-06, WTH-08, WTH-09, WTH-10 (Core spec 0.19, §9.4 Reference modules)
import { MirrorElement, html, svg, nothing } from "/core/mm3.js";
import { ICONS } from "./icons.js";

const UNITS = {
  metric: { temp: "celsius", wind: "kilometer-per-hour", length: "millimeter" },
  imperial: { temp: "fahrenheit", wind: "mile-per-hour", length: "inch" },
};
const MINUTE = 60000;
const MAX_U = 20; // px, the theme's small text size: nothing is drawn larger than this unit
const MIN_U = 13;

/** Milliseconds of an ISO 8601 duration such as "PT10M" (the public config form of a timedelta). */
function duration(text, fallback) {
  const m = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$/.exec(String(text ?? ""));
  if (!m) return fallback;
  const [, d = 0, h = 0, min = 0, s = 0] = m;
  return (((Number(d) * 24 + Number(h)) * 60 + Number(min)) * 60 + Number(s)) * 1000;
}

class WeatherElement extends MirrorElement {
  constructor() {
    super();
    this._size = null;
  }

  connectedCallback() {
    this._tick();
  }

  // The age of the data changes without any new state: look again once a minute (WTH-08).
  _tick() {
    this.requestRender();
    this.setTimeout(() => this._tick(), MINUTE);
  }

  onResume() {
    this.requestRender();
  }

  onResize(width, height) {
    this._size = [width, height];
    this.requestRender();
  }

  // The cell's size, in CSS pixels, sets the unit `u` all sizes are multiples of, so nothing
  // overflows; when even the smallest unit is too big, fewer entries are shown.
  _fit(rowUnits, perRowUnits, widthUnits, shrink = false) {
    const [w, h] = this._size ?? [240, 160];
    let u = Math.min(MAX_U, w / widthUnits, h / (rowUnits * perRowUnits));
    let rows = rowUnits;
    if (u < MIN_U && !shrink) {
      u = Math.min(MIN_U, w / widthUnits);
      rows = Math.max(1, Math.floor(h / (u * perRowUnits)));
    }
    return { u, rows, w, h };
  }

  _setUnit(u) {
    // CSSOM, not a style attribute: the CSP forbids inline styles
    this.style.setProperty("--u", `${u.toFixed(2)}px`);
  }

  _units() {
    return UNITS[this.state.units] ?? UNITS.metric;
  }

  _temp(value) {
    return this.fmtNumber(Math.round(value), this._units().temp);
  }

  /** WTH-06: the glyph of a condition, day or night, drawn with `svg` in theme colours. */
  _icon(condition, isDay = true) {
    const night = isDay === false && (condition === "clear" || condition === "partly_cloudy");
    const path = ICONS[night ? `${condition}-night` : condition] ?? ICONS.cloudy;
    return svg`<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d=${path}></path></svg>`;
  }

  /** WTH-10: `WEATHER_<CONDITION>`, with a `_NIGHT` variant for clear and partly cloudy. */
  _condition(condition, isDay = true) {
    const night = isDay === false && (condition === "clear" || condition === "partly_cloudy");
    return this.t(`WEATHER_${condition.toUpperCase()}${night ? "_NIGHT" : ""}`);
  }

  _percent(value) {
    return this.fmtNumber(Math.round(value), "percent");
  }

  // WTH-08: no success for more than twice the refresh period.
  _age() {
    const fetched = Date.parse(this.state.fetched_at ?? "");
    if (Number.isNaN(fetched)) return null;
    const age = Date.now() - fetched;
    const limit = 2 * duration(this.config.refresh, 10 * MINUTE);
    return age > limit ? age : null;
  }

  _ageText(ms) {
    const minutes = Math.max(1, Math.round(ms / MINUTE));
    const text =
      minutes < 90
        ? this.fmtNumber(minutes, "minute")
        : this.fmtNumber(Math.round(minutes / 60), "hour");
    return this.t("STALE_AGE", { age: text });
  }

  render() {
    const s = this.state;
    if (s.error === "grant") {
      // WTH-09: say which grant to add
      return this._notice(this.t("GRANT_MISSING", { grant: s.grant ?? "" }), "alert");
    }
    if (!s.data || s.data.length === 0) {
      return this._notice(this.t(s.error ? "WEATHER_UNAVAILABLE" : "LOADING"));
    }
    const stale = this._age();
    const extra = stale === null ? 0 : 1;
    const body =
      s.type === "daily"
        ? this._daily(s.data, extra)
        : s.type === "hourly"
          ? this._hourly(s.data, extra)
          : this._current(s.data, extra);
    return html`<div class="weather ${s.type} ${stale === null ? "" : "stale"}">
      ${body}
      ${stale === null ? nothing : html`<p class="age">${this._ageText(stale)}</p>`}
    </div>`;
  }

  _notice(text, role) {
    const [w, h] = this._size ?? [240, 160];
    this._setUnit(Math.max(9, Math.min(16, Math.sqrt((w * h) / (text.length * 0.9)))));
    return html`<p class="notice" role=${role ?? nothing}>${text}</p>`;
  }

  _current(c, extra) {
    const u = this._units();
    const rows = [];
    if (this.config.show_feels_like !== false && c.feels_like != null)
      rows.push(this._row("FEELS_LIKE", this._temp(c.feels_like)));
    if (this.config.show_humidity && c.humidity != null)
      rows.push(this._row("HUMIDITY", this._percent(c.humidity)));
    if (this.config.show_wind && c.wind_speed != null)
      rows.push(this._row("WIND", this.fmtNumber(Math.round(c.wind_speed), u.wind)));
    if (this.config.show_precipitation && c.precipitation != null)
      rows.push(this._row("PRECIPITATION", this.fmtNumber(c.precipitation, u.length)));
    const place = this.state.location?.name;
    // temperature 2.4u, condition 1.15u and place 1.05u beside the icon; one row each below.
    // Detail rows that would shrink the text below the readable unit are left out.
    const lines = 3.6 + (place ? 1.05 : 0);
    let shown = rows.length;
    const need = (k) => lines + k * 1.05 + extra * 0.95 + 1.0;
    while (shown > 0 && this._fit(need(shown), 1, 10, true).u < MIN_U) shown -= 1;
    const fit = this._fit(need(shown), 1, 10, true);
    rows.length = shown;
    this._setUnit(fit.u);
    return html`<div class="now">
      ${this._icon(c.condition, c.is_day)}
      <div class="main">
        <div class="temp">${this._temp(c.temperature)}</div>
        <div class="text">${this._condition(c.condition, c.is_day)}</div>
        ${place ? html`<div class="place">${place}</div>` : nothing}
      </div>
    </div>
    ${rows.length ? html`<ul class="details">${rows}</ul>` : nothing}`;
  }

  _row(key, value) {
    return html`<li><span class="label">${this.t(key)}</span> <span>${value}</span></li>`;
  }

  _daily(days, extra) {
    const sun = this.config.show_sun === true;
    const wide = sun ? 22 : 16;
    const fit = this._fit(days.length + extra * 0.6, 1.7, wide);
    this._setUnit(fit.u);
    return html`<ul class="list">
      ${days.slice(0, Math.max(1, fit.rows - Math.ceil(extra * 0.6))).map((d) => {
        const extra = [];
        if (d.precipitation_probability != null)
          extra.push(html`<span class="prob">${this._percent(d.precipitation_probability)}</span>`);
        if (sun && d.sunrise && d.sunset)
          extra.push(html`<span class="sun">${this._clock(d.sunrise)}–${this._clock(d.sunset)}</span>`);
        return html`<li>
          <span class="when">${this.fmtDate(`${d.date}T12:00:00Z`, { weekday: "short", timeZone: "UTC" })}</span>
          ${this._icon(d.condition)}
          <span class="range"><span class="max">${this._temp(d.temp_max)}</span>
            <span class="min">${this._temp(d.temp_min)}</span></span>
          <span class="extra">${extra}</span>
        </li>`;
      })}
    </ul>`;
  }

  _clock(iso) {
    return this.fmtDate(iso, { hour: "numeric", minute: "2-digit" });
  }

  _hourly(hours, extra) {
    // one strip of cells; as many as fit the width at a readable unit
    const [w, h] = this._size ?? [240, 160];
    let u = Math.min(MAX_U, h / (5.2 + extra * 0.9));
    let cols = Math.floor(w / (3.6 * u));
    const want = Math.min(hours.length, 4);
    if (cols < want) {
      u = Math.max(9, Math.min(u, w / (3.6 * want)));
      cols = Math.max(1, Math.floor(w / (3.6 * u)));
    }
    this._setUnit(u);
    return html`<ul class="list hours">
      ${hours.slice(0, Math.max(1, cols)).map(
        (h) => html`<li>
          <span class="when">${this.fmtDate(h.time, { hour: "numeric" })}</span>
          ${this._icon(h.condition, h.is_day)}
          <span class="max">${this._temp(h.temperature)}</span>
          ${h.precipitation_probability != null
            ? html`<span class="prob">${this._percent(h.precipitation_probability)}</span>`
            : nothing}
        </li>`,
      )}
    </ul>`;
  }
}

customElements.define("mm3-weather", WeatherElement);
