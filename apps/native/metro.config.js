const path = require("node:path");
const { getDefaultConfig } = require("expo/metro-config");
const { withNativeWind } = require("nativewind/metro");

const config = getDefaultConfig(__dirname);

// Jotai's ESM build reads `import.meta.env`, which a classic-script web bundle
// rejects at parse time. On web, point every `jotai` specifier at the
// package's CommonJS file of the same name so the bundle never contains
// `import.meta`.
const jotaiRoot = path.dirname(require.resolve("jotai/package.json"));
const upstreamResolveRequest = config.resolver.resolveRequest;
config.resolver.resolveRequest = (context, moduleName, platform) => {
  if (platform === "web" && (moduleName === "jotai" || moduleName.startsWith("jotai/"))) {
    const subpath = moduleName === "jotai" ? "index" : moduleName.slice("jotai/".length);
    return { type: "sourceFile", filePath: path.join(jotaiRoot, `${subpath}.js`) };
  }
  const resolve = upstreamResolveRequest ?? context.resolveRequest;
  return resolve(context, moduleName, platform);
};

module.exports = withNativeWind(config, { input: "./src/global.css" });
