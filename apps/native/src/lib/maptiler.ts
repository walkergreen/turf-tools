// Basemap URLs and key handling. The MapTiler key lives in
// EXPO_PUBLIC_MAPTILER_KEY (gitignored env file) and is inlined into the JS
// bundle at build time. Sign up at maptiler.com and add your key.
//
// Demo fallback: with no key set, the basemap and label tiles come from
// OpenFreeMap (tiles.openfreemap.org), which serves the same OpenMapTiles
// schema the label layers read and needs no API key.

const MAPTILER_KEY = process.env.EXPO_PUBLIC_MAPTILER_KEY ?? "";
const USE_OPENFREEMAP = MAPTILER_KEY.length === 0;

// Custom styles from Maptiler account.
const STYLE_ID_LIGHT = "01968205-0dc7-71df-87a7-8b67f7828379";
const STYLE_ID_DARK = "019d7ad6-3be3-7b78-86e3-853060587c76";

const OPENFREEMAP_STYLE_LIGHT = "https://tiles.openfreemap.org/styles/positron";
const OPENFREEMAP_STYLE_DARK = "https://tiles.openfreemap.org/styles/fiord";

export function getMaptilerStyleUrl(isDark: boolean): string {
  if (USE_OPENFREEMAP) return isDark ? OPENFREEMAP_STYLE_DARK : OPENFREEMAP_STYLE_LIGHT;
  const styleId = isDark ? STYLE_ID_DARK : STYLE_ID_LIGHT;
  return `https://api.maptiler.com/maps/${styleId}/style.json?key=${MAPTILER_KEY}`;
}

// Keep the default export for backwards compat until all callers switch.
export const MAPTILER_STYLE_URL = getMaptilerStyleUrl(false);

// OpenMapTiles vector tile source. Used for label layers (roads, places,
// house numbers) overlaid on top of the basemap.
export const MAPTILER_OPENMAPTILES_TILEJSON_URL = USE_OPENFREEMAP
  ? "https://tiles.openfreemap.org/planet"
  : `https://api.maptiler.com/tiles/v3-openmaptiles/tiles.json?key=${MAPTILER_KEY}`;

// True whenever a basemap source is available: a MapTiler key, or the
// keyless OpenFreeMap fallback.
export function isMaptilerKeyConfigured(): boolean {
  return true;
}
