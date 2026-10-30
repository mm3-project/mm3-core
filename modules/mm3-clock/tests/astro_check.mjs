// Runs the sun and moon functions over the almanac fixture and prints the results as JSON.
// Usage: node astro_check.mjs <astro.mjs> <almanac.json>
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [astroPath, fixturePath] = process.argv.slice(2);
const { sunTimes, moonTimes, moonPhase, dayWindow } = await import(pathToFileURL(astroPath).href);
const { rows } = JSON.parse(readFileSync(fixturePath, "utf8"));
const iso = (d) => (d ? d.toISOString() : null);
const out = rows.map((r) => {
  const [start, end] = dayWindow(Date.parse(r.noon), r.tz);
  const sun = sunTimes(start, end, r.lat, r.lon);
  const moon = moonTimes(start, end, r.lat, r.lon);
  const phase = moonPhase(Date.parse(r.noon));
  return {
    place: r.place,
    date: r.date,
    sunrise: iso(sun.rise),
    sunset: iso(sun.set),
    sun_always: sun.always,
    moonrise: iso(moon.rise),
    moonset: iso(moon.set),
    illumination: phase.illumination,
    index: phase.index,
  };
});
console.log(JSON.stringify(out));
