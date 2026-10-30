/*
 * Sun and moon for mm3-clock, in the browser, without a library (CLK-06).
 *
 * Sun: the NOAA solar position equations (declination, equation of time).
 * Moon: the main terms of the Meeus series (Astronomical Algorithms, ch. 47),
 * the phase and illumination from ch. 48. Events are found by scanning the
 * altitude through a local day and bisecting each crossing of the horizon.
 *
 * All times are milliseconds since the epoch (UTC); longitudes are east positive.
 */

const RAD = Math.PI / 180;
const DAY = 86400000;
const AU_KM = 149597870.7;

const jd = (ms) => ms / DAY + 2440587.5;
const norm = (x, m = 360) => ((x % m) + m) % m;
const sin = (deg) => Math.sin(deg * RAD);
const cos = (deg) => Math.cos(deg * RAD);

/** Apparent position of the sun: ecliptic longitude, declination (degrees), equation of time (minutes), distance (AU). */
export function sunPosition(ms) {
  const T = (jd(ms) - 2451545) / 36525;
  const L0 = norm(280.46646 + T * (36000.76983 + T * 0.0003032));
  const M = norm(357.52911 + T * (35999.05029 - 0.0001537 * T));
  const e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T);
  const C =
    sin(M) * (1.914602 - T * (0.004817 + 0.000014 * T)) +
    sin(2 * M) * (0.019993 - 0.000101 * T) +
    sin(3 * M) * 0.000289;
  const trueLong = L0 + C;
  const R = (1.000001018 * (1 - e * e)) / (1 + e * cos(M + C));
  const omega = 125.04 - 1934.136 * T;
  const lambda = trueLong - 0.00569 - 0.00478 * sin(omega);
  const eps0 = 23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60;
  const eps = eps0 + 0.00256 * cos(omega);
  const decl = Math.asin(sin(eps) * sin(lambda)) / RAD;
  const y = Math.tan((eps * RAD) / 2) ** 2;
  const eqTime =
    (4 / RAD) *
    (y * sin(2 * L0) -
      2 * e * sin(M) +
      4 * e * y * sin(M) * cos(2 * L0) -
      0.5 * y * y * sin(4 * L0) -
      1.25 * e * e * sin(2 * M));
  return { lambda: norm(lambda), decl, eqTime, R };
}

/** Altitude of the centre of the sun above the horizon, degrees. */
export function sunAltitude(ms, lat, lon) {
  const { decl, eqTime } = sunPosition(ms);
  const trueSolarTime = norm(norm(ms, DAY) / 60000 + eqTime + 4 * lon, 1440);
  const hourAngle = trueSolarTime / 4 - 180;
  return Math.asin(sin(lat) * sin(decl) + cos(lat) * cos(decl) * cos(hourAngle)) / RAD;
}

// Meeus table 47.A: [D, M, M', F, longitude (1e-6 deg), distance (1e-3 km)]
const LON_DIST = [
  [0, 0, 1, 0, 6288774, -20905355],
  [2, 0, -1, 0, 1274027, -3699111],
  [2, 0, 0, 0, 658314, -2955968],
  [0, 0, 2, 0, 213618, -569925],
  [0, 1, 0, 0, -185116, 48888],
  [0, 0, 0, 2, -114332, -3149],
  [2, 0, -2, 0, 58793, 246158],
  [2, -1, -1, 0, 57066, -152138],
  [2, 0, 1, 0, 53322, -170733],
  [2, -1, 0, 0, 45758, -204586],
  [0, 1, -1, 0, -40923, -129620],
  [1, 0, 0, 0, -34720, 108743],
  [0, 1, 1, 0, -30383, 104755],
  [2, 0, 0, -2, 15327, 10321],
  [0, 0, 1, 2, -12528, 0],
  [0, 0, 1, -2, 10980, 79661],
  [4, 0, -1, 0, 10675, -34782],
  [0, 0, 3, 0, 10034, -23210],
  [4, 0, -2, 0, 8548, -21636],
  [2, 1, -1, 0, -7888, 24208],
  [2, 1, 0, 0, -6766, 30824],
  [1, 0, -1, 0, -5163, -8379],
  [1, 1, 0, 0, 4987, -16675],
  [2, -1, 1, 0, 4036, -12831],
  [2, 0, 2, 0, 3994, -10445],
  [4, 0, 0, 0, 3861, -11650],
  [2, 0, -3, 0, 3665, 14403],
  [0, 1, -2, 0, -2689, -7003],
  [2, 0, -1, 2, -2602, 0],
  [2, -1, -2, 0, 2390, 10056],
  [1, 0, 1, 0, -2348, 6322],
  [2, -2, 0, 0, 2236, -9884],
  [0, 1, 2, 0, -2120, 5751],
  [0, 2, 0, 0, -2069, 0],
  [2, -2, -1, 0, 2048, -4950],
  [2, 0, 1, -2, -1773, 4130],
  [2, 0, 0, 2, -1595, 0],
  [4, -1, -1, 0, 1215, 0],
  [0, 0, 2, 2, -1110, 0],
  [3, 0, -1, 0, -892, 0],
  [2, 1, 1, 0, -810, 0],
  [4, -1, -2, 0, 759, 0],
  [0, 2, -1, 0, -713, 0],
  [2, 2, -1, 0, -700, 0],
  [2, 1, -2, 0, 691, 0],
  [2, -1, 0, -2, 596, 0],
  [4, 0, 1, 0, 549, 0],
  [0, 0, 4, 0, 537, 0],
  [4, -1, 0, 0, 520, 0],
  [1, 0, -2, 0, -487, 0],
  [2, 1, 0, -2, -399, 0],
  [0, 0, 2, -2, -381, 0],
  [1, 1, 1, 0, 351, 0],
  [3, 0, -2, 0, -340, 0],
  [4, 0, -3, 0, 330, 0],
  [2, -1, 2, 0, 327, 0],
  [0, 2, 1, 0, -323, 0],
  [1, 1, -1, 0, 299, 0],
  [2, 0, 3, 0, 294, 0],
];

// Meeus table 47.B: [D, M, M', F, latitude (1e-6 deg)]
const LAT = [
  [0, 0, 0, 1, 5128122],
  [0, 0, 1, 1, 280602],
  [0, 0, 1, -1, 277693],
  [2, 0, 0, -1, 173237],
  [2, 0, -1, 1, 55413],
  [2, 0, -1, -1, 46271],
  [2, 0, 0, 1, 32573],
  [0, 0, 2, 1, 17198],
  [2, 0, 1, -1, 9266],
  [0, 0, 2, -1, 8822],
  [2, -1, 0, -1, 8216],
  [2, 0, -2, -1, 4324],
  [2, 0, 1, 1, 4200],
  [2, 1, 0, -1, -3359],
  [2, -1, -1, 1, 2463],
  [2, -1, 0, 1, 2211],
  [2, -1, -1, -1, 2065],
  [0, 1, -1, -1, -1870],
  [4, 0, -1, -1, 1828],
  [0, 1, 0, 1, -1794],
  [0, 0, 0, 3, -1749],
  [0, 1, -1, 1, -1565],
  [1, 0, 0, 1, -1491],
  [0, 1, 1, 1, -1475],
  [0, 1, 1, -1, -1410],
  [0, 1, 0, -1, -1344],
  [1, 0, 0, -1, -1335],
  [0, 0, 3, 1, 1107],
  [4, 0, 0, -1, 1021],
  [4, 0, -1, 1, 833],
];

/** Geocentric moon: apparent ecliptic longitude and latitude (degrees), distance (km), right ascension and declination (degrees). */
export function moonPosition(ms) {
  const T = (jd(ms) - 2451545) / 36525;
  const Lp = norm(218.3164477 + 481267.88123421 * T - 0.0015786 * T * T + T ** 3 / 538841);
  const D = norm(297.8501921 + 445267.1114034 * T - 0.0018819 * T * T + T ** 3 / 545868);
  const M = norm(357.5291092 + 35999.0502909 * T - 0.0001536 * T * T);
  const Mp = norm(134.9633964 + 477198.8675055 * T + 0.0087414 * T * T + T ** 3 / 69699);
  const F = norm(93.272095 + 483202.0175233 * T - 0.0036539 * T * T - T ** 3 / 3526000);
  const E = 1 - 0.002516 * T - 0.0000074 * T * T;
  const A1 = norm(119.75 + 131.849 * T);
  const A2 = norm(53.09 + 479264.29 * T);
  const A3 = norm(313.45 + 481266.484 * T);

  let sl = 0;
  let sr = 0;
  for (const [d, m, mp, f, l, r] of LON_DIST) {
    const arg = d * D + m * M + mp * Mp + f * F;
    const factor = E ** Math.abs(m);
    sl += l * factor * sin(arg);
    sr += r * factor * cos(arg);
  }
  let sb = 0;
  for (const [d, m, mp, f, b] of LAT) {
    sb += b * E ** Math.abs(m) * sin(d * D + m * M + mp * Mp + f * F);
  }
  sl += 3958 * sin(A1) + 1962 * sin(Lp - F) + 318 * sin(A2);
  sb += -2235 * sin(Lp) + 382 * sin(A3) + 175 * sin(A1 - F) + 175 * sin(A1 + F) + 127 * sin(Lp - Mp) - 115 * sin(Lp + Mp);

  const omega = 125.04452 - 1934.136261 * T;
  const lambda = Lp + sl / 1e6 - 0.00478 * sin(omega);
  const beta = sb / 1e6;
  const dist = 385000.56 + sr / 1000;
  const eps = 23.4392911 - 0.0130042 * T + 0.00256 * cos(omega);
  const ra = norm(
    Math.atan2(sin(lambda) * cos(eps) - Math.tan(beta * RAD) * sin(eps), cos(lambda)) / RAD,
  );
  const dec = Math.asin(sin(beta) * cos(eps) + cos(beta) * sin(eps) * sin(lambda)) / RAD;
  return { lambda: norm(lambda), beta, dist, ra, dec };
}

const gmst = (ms) => {
  const T = (jd(ms) - 2451545) / 36525;
  return norm(280.46061837 + 360.98564736629 * (jd(ms) - 2451545) + 0.000387933 * T * T - T ** 3 / 38710000);
};

/** Altitude of the centre of the moon (geocentric), degrees, and the horizon altitude at which it rises and sets. */
export function moonAltitude(ms, lat, lon) {
  const { ra, dec, dist } = moonPosition(ms);
  const hourAngle = gmst(ms) + lon - ra;
  const alt = Math.asin(sin(lat) * sin(dec) + cos(lat) * cos(dec) * cos(hourAngle)) / RAD;
  const parallax = Math.asin(6378.14 / dist) / RAD;
  return { alt, horizon: 0.7275 * parallax - 0.5667 };
}

/**
 * Phase of the moon: illuminated percentage (0-100) and the phase index
 * 0 new, 1 waxing crescent, 2 first quarter, 3 waxing gibbous, 4 full,
 * 5 waning gibbous, 6 last quarter, 7 waning crescent.
 */
export function moonPhase(ms) {
  const moon = moonPosition(ms);
  const sun = sunPosition(ms);
  const elongation = norm(moon.lambda - sun.lambda);
  const psi = Math.acos(cos(moon.beta) * cos(elongation)) / RAD;
  const sunKm = sun.R * AU_KM;
  const incidence = Math.atan2(sunKm * sin(psi), moon.dist - sunKm * cos(psi)) / RAD;
  return {
    illumination: ((1 + cos(incidence)) / 2) * 100,
    index: Math.floor(norm(elongation + 22.5) / 45),
  };
}

/** The crossings of `f(ms) = 0` between `start` and `end`, found every `step` ms and refined to a second. */
function crossings(f, start, end, step = 600000) {
  const found = [];
  let t0 = start;
  let v0 = f(t0);
  for (let t1 = start + step; t0 < end; t1 += step) {
    const stop = Math.min(t1, end);
    const v1 = f(stop);
    if (v0 < 0 !== v1 < 0) {
      let lo = t0;
      let hi = stop;
      const rising = v1 >= 0;
      for (let i = 0; i < 24 && hi - lo > 500; i++) {
        const mid = (lo + hi) / 2;
        if (f(mid) < 0 === rising) lo = mid;
        else hi = mid;
      }
      found.push({ ms: Math.round((lo + hi) / 2), rising });
    }
    t0 = stop;
    v0 = v1;
  }
  return found;
}

function events(f, start, end) {
  const found = crossings(f, start, end);
  const rise = found.find((c) => c.rising);
  const set = found.find((c) => !c.rising);
  return {
    rise: rise ? new Date(rise.ms) : null,
    set: set ? new Date(set.ms) : null,
    // no crossing at all: the body stays above or below the horizon all day
    always: found.length === 0 ? (f((start + end) / 2) >= 0 ? "up" : "down") : null,
  };
}

/** Sunrise and sunset in [start, end); `always` is "up" (polar day) or "down" (polar night) without events. */
export function sunTimes(start, end, lat, lon) {
  const times = events((ms) => sunAltitude(ms, lat, lon) + 0.833, start, end);
  let lengthMinutes = null;
  if (times.rise && times.set) {
    const span = (times.set - times.rise) / 60000;
    lengthMinutes = span >= 0 ? span : span + 1440;
  } else if (times.always) {
    lengthMinutes = times.always === "up" ? 1440 : 0;
  }
  return { ...times, lengthMinutes };
}

/** Moonrise and moonset in [start, end); either can be null (the moon rises about 50 minutes later each day). */
export function moonTimes(start, end, lat, lon) {
  return events((ms) => {
    const { alt, horizon } = moonAltitude(ms, lat, lon);
    return alt - horizon;
  }, start, end);
}

const parts = (ms, timeZone) => {
  const out = {};
  for (const p of new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(ms))) {
    if (p.type !== "literal") out[p.type] = Number(p.value);
  }
  return out;
};

const offset = (ms, timeZone) => {
  const p = parts(ms, timeZone);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(ms / 1000) * 1000;
};

/** Calendar date and clock time of an instant in a time zone. */
export const zonedParts = parts;

/** The local day that contains `ms` in a time zone, as `[start, end)` in epoch milliseconds. */
export function dayWindow(ms, timeZone) {
  const p = parts(ms, timeZone);
  const at = (day) => {
    const wall = Date.UTC(p.year, p.month - 1, day);
    return wall - offset(wall - offset(ms, timeZone), timeZone);
  };
  return [at(p.day), at(p.day + 1)];
}
