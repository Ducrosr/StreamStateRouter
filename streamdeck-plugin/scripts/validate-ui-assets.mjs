import { readFileSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";

const pluginRoot = resolve("com.remyducros.streamstaterouter.sdPlugin");
const manifestPath = resolve(pluginRoot, "manifest.json");
const uiRoot = resolve(pluginRoot, "ui");
const bundle = resolve(uiRoot, "sdpi-components.js");
const license = resolve(uiRoot, "sdpi-components.LICENSE.md");

const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
const inspectors = [
  ...new Set(
    (manifest.Actions ?? [])
      .map((action) => action?.PropertyInspectorPath)
      .filter((path) => typeof path === "string" && path.length > 0)
      .map((path) => resolve(pluginRoot, path)),
  ),
];

if (inspectors.length === 0) {
  throw new Error("Manifest exposes no Property Inspector to validate");
}

for (const path of [bundle, license, ...inspectors]) {
  const stat = statSync(path);
  if (!stat.isFile() || stat.size === 0) {
    throw new Error(`Required Property Inspector asset is missing or empty: ${path}`);
  }
}

const bundleText = readFileSync(bundle, "utf8");
if (!bundleText.includes("sdpi-components v4.0.1")) {
  throw new Error("Bundled sdpi-components version is not v4.0.1");
}

const licenseText = readFileSync(license, "utf8");
if (!licenseText.includes("MIT License") || !licenseText.includes("Corsair Memory Inc.")) {
  throw new Error("Bundled sdpi-components MIT license notice is missing");
}

for (const path of inspectors) {
  const html = readFileSync(path, "utf8");
  if (html.includes("sdpi-components.dev")) {
    throw new Error(`Remote sdpi-components CDN reference is forbidden: ${path}`);
  }

  const expectedRelativeBundle = resolve(dirname(path), "sdpi-components.js");
  if (expectedRelativeBundle !== bundle || !html.includes('src="sdpi-components.js"')) {
    throw new Error(`Local sdpi-components reference is missing or unexpected: ${path}`);
  }
}

console.log(`Property Inspector assets OK (${inspectors.length} inspector(s))`);
