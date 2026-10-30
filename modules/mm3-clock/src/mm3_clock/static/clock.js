// Implements CLK-01, CLK-02, CLK-03, CLK-04, CLK-05, CLK-06, CLK-08 (Core spec 0.19, §9.4 Reference modules)
import { MirrorElement, html, svg, nothing } from "/core/mm3.js";
import { dayWindow, moonPhase, moonTimes, sunTimes, zonedParts } from "./astro.js";

const DAY = 86400000;

/** City part of an IANA name: "America/New_York" -> "New York". */
const city = (zone) => zone.split("/").pop().replace(/_/g, " ");

/** A LocalizedText from the public config: a string or a mapping by language. */
function localized(text, locale, fallback) {
  if (text === null || text === undefined) return fallback;
  if (typeof text !== "object") return String(text);
  const lower = new Map(Object.entries(text).map(([k, v]) => [k.toLowerCase(), v]));
  const tag = String(locale).toLowerCase();
  return lower.get(tag) ?? lower.get(tag.split("-")[0]) ?? lower.get("en") ?? Object.values(text)[0] ?? fallback;
}

/** ISO 8601 week number of a calendar date. */
function isoWeek(year, month, day) {
  const date = Date.UTC(year, month - 1, day);
  const weekday = (new Date(date).getUTCDay() + 6) % 7;
  const thursday = new Date(date + (3 - weekday) * DAY);
  const start = Date.UTC(thursday.getUTCFullYear(), 0, 1);
  return Math.floor((thursday.getTime() - start) / DAY / 7) + 1;
}

class ClockElement extends MirrorElement {
  constructor() {
    super();
    this._now = null;
    this._armed = false;
    this._astro = null;
    this._wide = true;
    this._size = null;
  }

  connectedCallback() {
    this._armed = false;
    this._tick();
  }

  // CLK-05: wake at the next second (or minute) boundary, not on a fixed interval, so drift and
  // a suspended tab correct themselves at the next tick.
  _unit() {
    return this.config.show_seconds === false ? 60000 : 1000;
  }

  _tick() {
    this._now = Date.now();
    this.requestRender();
    if (this._armed) return;
    this._armed = true;
    this._arm();
  }

  _arm() {
    const unit = this._unit();
    this.setTimeout(() => {
      this._now = Date.now();
      this.requestRender();
      this._arm();
    }, unit - (Date.now() % unit));
  }

  onResume() {
    this._now = Date.now();
    this.requestRender();
  }

  onResize(width, height) {
    this._size = [width, height];
    this._wide = width > height;
    this.requestRender();
  }

  // The cell's size, in CSS pixels, drives the face and the text so nothing overflows.
  _fit(digital, analog, sky) {
    const [w, h] = this._size ?? [240, 160];
    const room = sky ? h * 0.7 : h;
    let face = Math.min(w, room, 280);
    if (analog && digital) face = this._wide ? Math.min(w * 0.45, room) : Math.min(w, room * 0.55);
    return { w, face: Math.max(40, Math.floor(face)) };
  }

  get _zone() {
    return this.config.timezone || this.timezone;
  }

  _time(ms, zone, seconds) {
    const options = { hour: "numeric", minute: "2-digit", timeZone: zone };
    if (seconds) options.second = "2-digit";
    if (this.config.time_format === "12") options.hour12 = true;
    else if (this.config.time_format === "24") {
      options.hourCycle = "h23";
      options.hour = "2-digit"; // 08:30 in every language
    }
    return this.fmtDate(ms, options);
  }

  _place() {
    const place = this.config.location || this.location;
    return place && typeof place.lat === "number" && typeof place.lon === "number" ? place : null;
  }

  render() {
    const ms = this._now ?? Date.now();
    const zone = this._zone;
    const display = this.config.display || "digital";
    const analog = display === "analog" || display === "both";
    const digital = display === "digital" || display === "both";
    const body = [];
    if (analog) body.push(this._face(ms, zone));
    if (digital) body.push(this._digital(ms, zone));
    const sky = this._sky(ms, zone);
    const layout = display === "both" && this._wide ? "row" : "column";
    const { w, face } = this._fit(digital, analog, sky !== nothing);
    // CSSOM, not a style attribute: the CSP forbids inline styles
    this.style.setProperty("--w", `${w}px`);
    this.style.setProperty("--face", `${face}px`);
    return html`<div class="clock ${layout}">
      <div class="main ${layout}">${body}</div>
      ${sky}
    </div>`;
  }

  _digital(ms, zone) {
    const seconds = this.config.show_seconds !== false;
    const main = zonedParts(ms, zone);
    const zones = (this.config.zones || []).map((z) => {
      const p = zonedParts(ms, z.timezone);
      const days = Math.round(
        (Date.UTC(p.year, p.month - 1, p.day) - Date.UTC(main.year, main.month - 1, main.day)) / DAY,
      );
      const marker =
        days === 0
          ? nothing
          : html`<span class="offset">${this.t("DAY_OFFSET", {
              days: `${days > 0 ? "+" : "−"}${this.fmtNumber(Math.abs(days))}`,
            })}</span>`;
      return html`<li>
        <span class="label">${localized(z.label, this.locale, city(z.timezone))}</span>
        <span class="time">${this._time(ms, z.timezone, seconds)}</span> ${marker}
      </li>`;
    });
    return html`<div class="digital">
      <div class="time">${this._time(ms, zone, seconds)}</div>
      ${this.config.show_date === false
        ? nothing
        : html`<div class="date">${this.fmtDate(ms, { dateStyle: this.config.date_style || "long", timeZone: zone })}</div>`}
      ${this.config.show_week
        ? html`<div class="week">${this.t("WEEK", { week: this.fmtNumber(isoWeek(main.year, main.month, main.day)) })}</div>`
        : nothing}
      ${zones.length ? html`<ul class="zones">${zones}</ul>` : nothing}
    </div>`;
  }

  // CLK-04: an SVG face scaled to the element and kept square; colours from theme variables.
  _face(ms, zone) {
    const p = zonedParts(ms, zone);
    const showSeconds = this.config.show_seconds !== false;
    const minutes = p.minute + p.second / 60;
    const hours = (p.hour % 12) + minutes / 60;
    const face = this.config.face || "ticks";
    const marks = [];
    if (face === "ticks") {
      for (let i = 0; i < 60; i++) {
        const major = i % 5 === 0;
        marks.push(
          svg`<line class=${major ? "tick major" : "tick"} x1="0" y1=${major ? -41 : -44} x2="0" y2="-47" transform="rotate(${i * 6})" />`,
        );
      }
    } else if (face === "numbers") {
      for (let h = 1; h <= 12; h++) {
        const a = (h * Math.PI) / 6;
        marks.push(
          svg`<text class="number" x=${(Math.sin(a) * 37).toFixed(2)} y=${(-Math.cos(a) * 37).toFixed(2)}>${this.fmtNumber(h)}</text>`,
        );
      }
    }
    return html`<div class="face">
      <svg viewBox="-50 -50 100 100" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
        <circle class="rim" r="48" />
        ${marks}
        <line class="hand hour" x1="0" y1="4" x2="0" y2="-24" transform="rotate(${(hours * 30).toFixed(2)})" />
        <line class="hand minute" x1="0" y1="6" x2="0" y2="-36" transform="rotate(${(minutes * 6).toFixed(2)})" />
        ${showSeconds
          ? svg`<line class="hand second" x1="0" y1="8" x2="0" y2="-40" transform="rotate(${p.second * 6})" />`
          : nothing}
        <circle class="pin" r="1.8" />
      </svg>
    </div>`;
  }

  // CLK-06: sun and moon, computed here from the instance's or the mirror's location.
  _sky(ms, zone) {
    const wantSun = this.config.sun === true;
    const wantMoon = this.config.moon === true;
    if (!wantSun && !wantMoon) return nothing;
    const place = this._place();
    if (!place) return html`<p class="hint">${this.t("NO_LOCATION")}</p>`;
    const [start, end] = dayWindow(ms, zone);
    const key = `${start}|${place.lat}|${place.lon}|${wantSun}|${wantMoon}`;
    if (!this._astro || this._astro.key !== key) {
      this._astro = {
        key,
        sun: wantSun ? sunTimes(start, end, place.lat, place.lon) : null,
        moon: wantMoon ? moonTimes(start, end, place.lat, place.lon) : null,
      };
    }
    const at = (date) => (date ? this._time(date, zone, false) : "—");
    const rows = [];
    const { sun, moon } = this._astro;
    if (sun) {
      if (sun.always) {
        rows.push(html`<li class="polar">${this.t(sun.always === "up" ? "POLAR_DAY" : "POLAR_NIGHT")}</li>`);
      } else {
        const m = Math.round(sun.lengthMinutes);
        rows.push(
          html`<li><span class="label">${this.t("SUNRISE")}</span><span>${at(sun.rise)}</span></li>`,
          html`<li><span class="label">${this.t("SUNSET")}</span><span>${at(sun.set)}</span></li>`,
          html`<li><span class="label">${this.t("DAY_LENGTH")}</span><span>${this.t("DURATION", {
            hours: this.fmtNumber(Math.floor(m / 60)),
            minutes: this.fmtNumber(m % 60),
          })}</span></li>`,
        );
      }
    }
    if (moon) {
      const phase = moonPhase(ms);
      rows.push(
        html`<li><span class="label">${this.t(`MOON_${phase.index}`)}</span><span>${this.fmtNumber(Math.round(phase.illumination))}%</span></li>`,
        html`<li><span class="label">${this.t("MOONRISE")}</span><span>${at(moon.rise)}</span></li>`,
        html`<li><span class="label">${this.t("MOONSET")}</span><span>${at(moon.set)}</span></li>`,
      );
    }
    return html`<ul class="sky">${rows}</ul>`;
  }
}
customElements.define("mm3-clock", ClockElement);
