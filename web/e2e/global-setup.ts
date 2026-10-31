import { execFileSync } from "node:child_process";
import { join } from "node:path";

// Every e2e run starts from the same seeded story (and a reset demo clock).
export default function globalSetup() {
  if (process.env.E2E_SKIP_SEED) return;
  execFileSync("uv", ["run", "python", "-m", "app.seed"], { cwd: join(__dirname, "../../api"), stdio: "inherit" });
}
