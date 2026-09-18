import { defineConfig } from "rolldown";

const folder = "com.remyducros.streamstaterouter.sdPlugin";
export default defineConfig({
  input: "src/plugin.ts",
  output: { file: `${folder}/bin/plugin.js`, minify: true },
  transform: { decorator: { legacy: true } },
  platform: "node",
  resolve: { conditionNames: ["node"] },
  plugins: [{
    name: "emit-module-package-file",
    generateBundle() {
      this.emitFile({ fileName: "package.json", source: `{ "type": "module" }`, type: "asset" });
    }
  }]
});
