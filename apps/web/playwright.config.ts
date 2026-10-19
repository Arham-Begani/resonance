// Browser end-to-end checks (owner: independent verification engineer).
//
// Starts its own isolated API (port 8044) and Vite dev server (port 5191) for every run; it
// never reuses a running server and never touches the shared `.data/` directory. The API uses
// the real ngspice build and, because no NEBIUS_API_KEY is configured, reports the AI as
// unavailable: these tests exercise the manual (Guide me) path and the AI-unavailable states,
// never live inference.
//
// Run:   cd apps/web && npx playwright test                         (Vite dev server, default)
//        RESONANCE_E2E_TARGET=prod npx playwright test              (production build served by
//        the API itself, as deployed: `vite build` into a scratch dir + WEB_DIST_DIR)
// Data:  RESONANCE_E2E_DATA_ROOT (default D:\tmp\verif-D\data) / run-<timestamp>
import { execSync } from "node:child_process";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

export const API_PORT = 8044;
export const WEB_PORT = 5191;
const target = process.env.RESONANCE_E2E_TARGET === "prod" ? "prod" : "dev";

// One data directory per run (computed once in the runner process; workers inherit the env).
process.env.RESONANCE_E2E_RUN_ID ??= String(Date.now());
const dataRoot = process.env.RESONANCE_E2E_DATA_ROOT ?? "D:\\tmp\\verif-D\\data";
const dataDir = join(dataRoot, `run-${process.env.RESONANCE_E2E_RUN_ID}`);
const webDir = fileURLToPath(new URL(".", import.meta.url));
const apiDir = resolve(webDir, "../../services/api");
const prodDist = join(dataRoot, `web-dist-${process.env.RESONANCE_E2E_RUN_ID}`);

// Production target: build the current sources once (runner process only) into a scratch
// directory, never into apps/web/dist.
if (target === "prod" && !process.env.RESONANCE_E2E_BUILT) {
  execSync(`npx vite build --outDir "${prodDist}" --emptyOutDir --logLevel warn`, { cwd: webDir, stdio: "inherit" });
  process.env.RESONANCE_E2E_BUILT = "1";
}

const apiServer = {
  command: `uv run uvicorn resonance.app:app --host 127.0.0.1 --port ${API_PORT}`,
  cwd: apiDir,
  url: `http://127.0.0.1:${API_PORT}/api/health`,
  reuseExistingServer: false,
  timeout: 120_000,
  stdout: "ignore" as const,
  stderr: "pipe" as const,
  env: {
    RESONANCE_DATA_DIR: dataDir,
    // Make the AI state explicit and deterministic for these checks (never a real key).
    NEBIUS_API_KEY: "",
    ...(target === "prod" ? { WEB_DIST_DIR: prodDist } : {}),
  },
};

const viteServer = {
  command: `npx vite --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
  cwd: webDir,
  url: `http://127.0.0.1:${WEB_PORT}/`,
  reuseExistingServer: false,
  timeout: 120_000,
  stdout: "ignore" as const,
  stderr: "pipe" as const,
  env: { RESONANCE_API_URL: `http://127.0.0.1:${API_PORT}` },
};

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts$/,
  testIgnore: /simulator-unavailable\/.*/,
  // One backend shared by every test; ngspice runs are CPU-heavy on a shared 4-core host.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  // Explore experiments take 3–8 s and case instruments a few seconds each on this host.
  timeout: 300_000,
  expect: { timeout: 45_000 },
  reporter: [["list"], ["json", { outputFile: "test-results/verification/e2e-results.json" }]],
  outputDir: "test-results/verification/artifacts",
  metadata: { target },
  use: {
    baseURL: target === "prod" ? `http://127.0.0.1:${API_PORT}` : `http://127.0.0.1:${WEB_PORT}`,
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: target === "prod" ? [apiServer] : [apiServer, viteServer],
});
