// Copies the Python backend next to the extension so the .vsix is self-contained.
import { cpSync, mkdirSync, rmSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const out = join(here, "..", "backend");
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
for (const f of ["server.py", "config.py", "requirements.txt", "requirements-index.txt"]) cpSync(join(root, f), join(out, f));
for (const d of ["core", "agents", "tools", "rag"]) {
  if (!existsSync(join(root, d))) throw new Error(`missing backend folder: ${d}`);
  cpSync(join(root, d), join(out, d), { recursive: true, filter: (s) => !s.includes("__pycache__") && !s.endsWith(".pyc") });
}
console.log("backend copied to", out);
