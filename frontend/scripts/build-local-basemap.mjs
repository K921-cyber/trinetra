/**
 * Build the local basemap assets used by LocalBasemap.tsx.
 *
 * The dashboard must render without calling any external tile/map API, so the
 * basemap is shipped as two small GeoJSON modules generated from Natural Earth
 * (public domain — https://www.naturalearthdata.com/about/terms-of-use/):
 *
 *   - src/utils/basemap/indiaStates.ts     36 states/UTs (50m admin-1)
 *   - src/utils/basemap/worldCountries.ts  world country outlines + label points
 *                                          (110m admin-0)
 *
 * Shapes are simplified (Douglas–Peucker) and coordinate-quantized so the whole
 * basemap stays a few hundred KB in the bundle.
 *
 * Usage (from frontend/):
 *   node scripts/build-local-basemap.mjs                 # download + generate
 *   node scripts/build-local-basemap.mjs --cache-dir DIR # reuse downloaded files
 *
 * Output is committed, so normal builds never need network access.
 *
 * Regenerate only if you want different detail levels or newer boundaries.
 */

import { writeFile, mkdir, readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC_DIR = resolve(HERE, '..', 'src');
const OUT_DIR = join(SRC_DIR, 'utils', 'basemap');

const SOURCES = {
  india: {
    url: 'https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_1_states_provinces.geojson',
    file: 'ne_50m_admin_1_states_provinces.geojson',
    // ~1.1 km at India's latitudes: plenty for zoom 6–9, a fraction of the bytes.
    tolerance: 0.01,
    precision: 3,
    minRingArea: 0.0005,
    label: 'Natural Earth 1:50m Admin-1 States/Provinces (public domain)',
  },
  world: {
    url: 'https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson',
    file: 'ne_110m_admin_0_countries.geojson',
    tolerance: 0.05,
    precision: 2,
    minRingArea: 0.02,
    label: 'Natural Earth 1:110m Admin-0 Countries (public domain)',
  },
};

// ==================== Geometry helpers ====================

function sqSegDist(p, a, b) {
  let x = a[0];
  let y = a[1];
  let dx = b[0] - x;
  let dy = b[1] - y;
  if (dx !== 0 || dy !== 0) {
    const t = ((p[0] - x) * dx + (p[1] - y) * dy) / (dx * dx + dy * dy);
    if (t > 1) {
      x = b[0];
      y = b[1];
    } else if (t > 0) {
      x += dx * t;
      y += dy * t;
    }
  }
  dx = p[0] - x;
  dy = p[1] - y;
  return dx * dx + dy * dy;
}

/** Douglas–Peucker simplification of an open coordinate line. */
function simplifyLine(points, tolerance) {
  if (points.length < 3) return points;
  const sqTol = tolerance * tolerance;
  const keep = new Uint8Array(points.length);
  keep[0] = 1;
  keep[points.length - 1] = 1;
  const stack = [[0, points.length - 1]];
  while (stack.length) {
    const [first, last] = stack.pop();
    let maxDist = -1;
    let index = -1;
    for (let i = first + 1; i < last; i++) {
      const d = sqSegDist(points[i], points[first], points[last]);
      if (d > maxDist) {
        maxDist = d;
        index = i;
      }
    }
    if (maxDist > sqTol && index !== -1) {
      keep[index] = 1;
      stack.push([first, index], [index, last]);
    }
  }
  return points.filter((_, i) => keep[i] === 1);
}

function ringArea(ring) {
  let area = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    area += ring[j][0] * ring[i][1] - ring[i][0] * ring[j][1];
  }
  return Math.abs(area / 2);
}

function pointInRing([px, py], ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

function ringCentroid(ring) {
  let twiceArea = 0;
  let cx = 0;
  let cy = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const cross = ring[j][0] * ring[i][1] - ring[i][0] * ring[j][1];
    twiceArea += cross;
    cx += (ring[j][0] + ring[i][0]) * cross;
    cy += (ring[j][1] + ring[i][1]) * cross;
  }
  if (twiceArea === 0) {
    const [x, y] = ring[0];
    return [x, y];
  }
  return [cx / (3 * twiceArea), cy / (3 * twiceArea)];
}

function quantize(ring, precision) {
  const factor = 10 ** precision;
  const out = [];
  for (const [lng, lat] of ring) {
    const p = [Math.round(lng * factor) / factor, Math.round(lat * factor) / factor];
    const prev = out[out.length - 1];
    if (!prev || prev[0] !== p[0] || prev[1] !== p[1]) out.push(p);
  }
  // Keep the ring closed.
  if (out.length > 1) {
    const first = out[0];
    const last = out[out.length - 1];
    if (first[0] !== last[0] || first[1] !== last[1]) out.push([first[0], first[1]]);
  }
  return out;
}

/** Simplify, quantize and drop insignificant rings from one polygon (array of rings). */
function cleanPolygon(rings, opts) {
  const cleaned = [];
  for (const ring of rings) {
    if (ring.length < 4) continue;
    const simplified = simplifyLine(ring, opts.tolerance);
    const quantized = quantize(simplified, opts.precision);
    if (quantized.length < 4) continue;
    if (ringArea(quantized) < opts.minRingArea) continue;
    cleaned.push(quantized);
  }
  return cleaned;
}

/** Convert any Polygon/MultiPolygon geometry into a MultiPolygon coordinate array. */
function toMultiPolygon(geometry) {
  if (!geometry) return null;
  if (geometry.type === 'Polygon') return [geometry.coordinates];
  if (geometry.type === 'MultiPolygon') return geometry.coordinates;
  return null;
}

/** A stable point inside the largest polygon, used for the state label. */
function labelPoint(polygons) {
  let largest = null;
  let largestArea = -1;
  for (const rings of polygons) {
    const area = ringArea(rings[0]);
    if (area > largestArea) {
      largestArea = area;
      largest = rings[0];
    }
  }
  if (!largest) return null;

  const lngs = largest.map((p) => p[0]);
  const lats = largest.map((p) => p[1]);
  const lngMin = Math.min(...lngs);
  const lngMax = Math.max(...lngs);
  const latMin = Math.min(...lats);
  const latMax = Math.max(...lats);

  const candidates = [
    ringCentroid(largest),
    [(lngMin + lngMax) / 2, (latMin + latMax) / 2],
    largest[0],
    largest[Math.floor(largest.length / 2)],
  ];
  for (const candidate of candidates) {
    if (pointInRing(candidate, largest)) {
      return candidate.map((v) => Math.round(v * 1000) / 1000);
    }
  }
  return candidates[0].map((v) => Math.round(v * 1000) / 1000);
}

// ==================== Data loading ====================

async function loadSource(source, cacheDir) {
  if (cacheDir) {
    const cached = join(cacheDir, source.file);
    if (existsSync(cached)) {
      console.log(`  using cached ${cached}`);
      return JSON.parse(await readFile(cached, 'utf8'));
    }
  }
  console.log(`  downloading ${source.url}`);
  const res = await fetch(source.url);
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${source.url}`);
  const text = await res.text();
  if (cacheDir) {
    await mkdir(cacheDir, { recursive: true });
    await writeFile(join(cacheDir, source.file), text);
  }
  return JSON.parse(text);
}

const ok = (value) => typeof value === 'number' && Number.isFinite(value);
const small = (n) => Math.round(n * 100) / 100;

// ==================== Writers ====================

async function writeModule(fileName, headerNote, importTypeLine, typeName, body) {
  const content = `${headerNote}\n\n${importTypeLine}\n\nconst data = ${body} as unknown as ${typeName};\n\nexport default data;\n`;
  const path = join(OUT_DIR, fileName);
  await writeFile(path, content);
  const kb = (Buffer.byteLength(content) / 1024).toFixed(1);
  console.log(`  wrote ${fileName} (${kb} KB)`);
}

function featureLine(feature, properties) {
  return `  ${JSON.stringify({ type: 'Feature', properties, geometry: feature })}`;
}

// ==================== Main ====================

async function main() {
  const cacheArgIndex = process.argv.indexOf('--cache-dir');
  const cacheDir = cacheArgIndex !== -1 ? resolve(process.argv[cacheArgIndex + 1]) : null;

  await mkdir(OUT_DIR, { recursive: true });

  // ---------- India states (50m admin-1) ----------
  console.log('India states:');
  const indiaRaw = await loadSource(SOURCES.india, cacheDir);
  const states = [];
  for (const feature of indiaRaw.features) {
    const props = feature.properties || {};
    if (props.adm0_a3 !== 'IND' && props.geonunit !== 'India') continue;

    const sourceMulti = toMultiPolygon(feature.geometry);
    if (!sourceMulti) continue;
    const polygons = sourceMulti
      .map((rings) => cleanPolygon(rings, SOURCES.india))
      .filter((rings) => rings.length > 0);
    if (!polygons.length) continue;

    const name = props.name || props.NAME_1;
    if (!name) continue;
    const label = labelPoint(polygons);
    if (!label || !ok(label[0]) || !ok(label[1])) continue;

    const iso = String(props.iso_3166_2 || '');
    const code = iso.replace(/^IN-/, '');
    states.push({
      geometry: { type: 'MultiPolygon', coordinates: polygons.map((rings) => rings.map((r) => r.map(([lng, lat]) => [small(lng), small(lat)]))) },
      properties: { name, code, label },
    });
  }
  states.sort((a, b) => a.properties.name.localeCompare(b.properties.name));
  if (states.length < 30) throw new Error(`Only ${states.length} Indian states found — aborting`);

  await writeModule(
    'indiaStates.ts',
    [
      '// AUTO-GENERATED by scripts/build-local-basemap.mjs — do not edit by hand.',
      `// ${SOURCES.india.label}.`,
      '// Simplified to ~1 km at 3 decimal places: sufficient for zoom 6–9 overlays.',
    ].join('\n'),
    "import type { FeatureCollection, MultiPolygon, Polygon } from 'geojson';\n\n// Declared as a type (not an interface) so it stays assignable to GeoJSON's\n// open-ended properties record.\nexport type BasemapStateProperties = {\n  name: string;\n  code: string;\n  /** Point inside the state, used to place the map label. */\n  label: [number, number];\n};",
    'FeatureCollection<Polygon | MultiPolygon, BasemapStateProperties>',
    `{\n  type: 'FeatureCollection',\n  features: [\n${states.map((s) => featureLine(s.geometry, s.properties)).join(',\n')}\n  ]\n}`,
  );

  // ---------- World countries (110m admin-0) ----------
  console.log('World countries:');
  const worldRaw = await loadSource(SOURCES.world, cacheDir);
  const countries = [];
  for (const feature of worldRaw.features) {
    const props = feature.properties || {};
    const name = props.NAME || props.ADMIN || props.name;
    if (!name || name === 'Antarctica') continue;

    const sourceMulti = toMultiPolygon(feature.geometry);
    if (!sourceMulti) continue;
    const polygons = sourceMulti
      .map((rings) => cleanPolygon(rings, SOURCES.world))
      .filter((rings) => rings.length > 0);
    if (!polygons.length) continue;

    // Natural Earth ships a curated label anchor per country (hand-placed so
    // it does not land in the sea or in a neighbour); fall back to the largest
    // polygon's interior point when it is missing.
    let label =
      ok(props.LABEL_X) && ok(props.LABEL_Y)
        ? [small(props.LABEL_X), small(props.LABEL_Y)]
        : labelPoint(polygons);
    if (!label) label = [0, 0];

    countries.push({
      geometry: { type: 'MultiPolygon', coordinates: polygons },
      properties: {
        name,
        code: String(props.ADM0_A3 || ''),
        label,
        // 2 = draw at world zoom, 7 = only when zoomed well in.
        rank: ok(props.LABELRANK) ? props.LABELRANK : 6,
      },
    });
  }
  countries.sort((a, b) => a.properties.name.localeCompare(b.properties.name));
  if (countries.length < 150) throw new Error(`Only ${countries.length} countries found — aborting`);

  await writeModule(
    'worldCountries.ts',
    [
      '// AUTO-GENERATED by scripts/build-local-basemap.mjs — do not edit by hand.',
      `// ${SOURCES.world.label}.`,
      '// Context shapes plus label anchors, used under the India state layer.',
    ].join('\n'),
    "import type { FeatureCollection, MultiPolygon, Polygon } from 'geojson';\n\n// Declared as a type (not an interface) so it stays assignable to GeoJSON's\n// open-ended properties record.\nexport type BasemapCountryProperties = {\n  name: string;\n  code: string;\n  /** Hand-placed point inside the country, used to place the map label. */\n  label: [number, number];\n  /** Natural Earth label rank: 2 = label at world zoom, 7 = only when close. */\n  rank: number;\n};",
    'FeatureCollection<Polygon | MultiPolygon, BasemapCountryProperties>',
    `{\n  type: 'FeatureCollection',\n  features: [\n${countries.map((c) => featureLine(c.geometry, c.properties)).join(',\n')}\n  ]\n}`,
  );

  console.log(`Done: ${states.length} states/UTs, ${countries.length} countries.`);
}

main().catch((err) => {
  console.error('Failed to build local basemap:', err.message);
  process.exit(1);
});
