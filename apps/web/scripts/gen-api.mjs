// Regenerate src/api/openapi.json and src/api/schema.d.ts from the FastAPI app.
// Requires uv (https://docs.astral.sh/uv/). Run: npm run gen:api
import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const web = resolve(here, "..");
const api = resolve(web, "../../services/api");
const specPath = resolve(web, "src/api/openapi.json");
const typesPath = resolve(web, "src/api/schema.d.ts");

execFileSync("uv", ["run", "python", "-m", "resonance.openapi_export", specPath], {
  cwd: api,
  stdio: "inherit",
});
execFileSync(
  process.execPath,
  [resolve(web, "node_modules/openapi-typescript/bin/cli.js"), specPath, "-o", typesPath],
  { cwd: web, stdio: "inherit" },
);
console.log("Generated", specPath, "and", typesPath);
