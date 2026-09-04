// Basemap URLs and key handling. The MapTiler key lives in VITE_MAPTILER_KEY
// (gitignored env file) and is inlined into the JS bundle at build time. Sign
// up at maptiler.com and add your key. Use the same key as
// EXPO_PUBLIC_MAPTILER_KEY in the native app so both apps share the styles.
//
// Demo fallback: with no key set, the basemap and label tiles come from
// OpenFreeMap (tiles.openfreemap.org), which serves the same OpenMapTiles
// schema the label overlay reads (`place`, `transportation_name`,
// `housenumber`) and needs no API key.
const MAPTILER_KEY = import.meta.env.VITE_MAPTILER_KEY ?? "";
const USE_OPENFREEMAP = MAPTILER_KEY.length === 0;

// Custom styles from Maptiler account.
const STYLE_ID_LIGHT = "01961350-1791-703e-8753-2c795c604620";
const STYLE_ID_DARK = "019dc276-9981-7168-a043-8a1ae4051996";

const OPENFREEMAP_STYLE_LIGHT = "https://tiles.openfreemap.org/styles/positron";
const OPENFREEMAP_STYLE_DARK = "https://tiles.openfreemap.org/styles/fiord";

export function getMaptilerStyleUrl(isDark: boolean): string {
  if (USE_OPENFREEMAP) return isDark ? OPENFREEMAP_STYLE_DARK : OPENFREEMAP_STYLE_LIGHT;
  const styleId = isDark ? STYLE_ID_DARK : STYLE_ID_LIGHT;
  return `https://api.maptiler.com/maps/${styleId}/style.json?key=${MAPTILER_KEY}`;
}

// OpenMapTiles vector tile source. Used for label overlays (roads, places,
// house numbers) on top of the basemap.
export const MAPTILER_OPENMAPTILES_TILEJSON_URL = USE_OPENFREEMAP
  ? "https://tiles.openfreemap.org/planet"
  : `https://api.maptiler.com/tiles/v3-openmaptiles/tiles.json?key=${MAPTILER_KEY}`;

// True whenever a basemap source is available: a MapTiler key, or the
// keyless OpenFreeMap fallback.
export function isMaptilerKeyConfigured(): boolean {
  return true;
}
