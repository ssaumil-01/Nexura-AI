// Launches a real VS Code with the extension and runs test/suite. The backend runs in fake-LLM mode (NEXURA_FAKE_LLM=1):
// no network and no API keys. Needs the repo's Python venv (NEXURA_TEST_PYTHON overrides the default).
import { execFileSync } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { runTests } from "@vscode/test-electron";

function git(cwd: string, ...args: string[]) {
  execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@example.com", ...args], { cwd, stdio: "pipe" });
}

async function main() {
  delete process.env.ELECTRON_RUN_AS_NODE; // set by some host terminals; it would start VS Code as plain Node
  const extensionDevelopmentPath = path.resolve(__dirname, "../..");
  const extensionTestsPath = path.resolve(__dirname, "suite/index");
  const repo = path.resolve(extensionDevelopmentPath, "..");
  const python = process.env.NEXURA_TEST_PYTHON ??
    path.join(repo, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  if (!fs.existsSync(python)) throw new Error(`Python for the backend not found: ${python} (set NEXURA_TEST_PYTHON)`);

  const ws = fs.mkdtempSync(path.join(os.tmpdir(), "nexura-e2e-"));
  const data = fs.mkdtempSync(path.join(os.tmpdir(), "nexura-e2e-user-"));
  fs.mkdirSync(path.join(ws, ".vscode"));
  fs.writeFileSync(path.join(ws, ".vscode", "settings.json"), JSON.stringify({ "nexura.pythonPath": python }));
  fs.writeFileSync(path.join(ws, "README.md"), "e2e workspace\n");
  git(ws, "init", "-q");
  git(ws, "add", "-A");
  git(ws, "commit", "-qm", "init");

  await runTests({
    extensionDevelopmentPath,
    extensionTestsPath,
    launchArgs: [ws, "--disable-extensions", "--disable-workspace-trust", `--user-data-dir=${path.join(data, "user")}`],
    extensionTestsEnv: { NEXURA_FAKE_LLM: "1", NEXURA_TEST_WORKSPACE: ws },
  });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
