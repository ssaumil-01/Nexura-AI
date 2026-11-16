// Starts and supervises the local Python backend: resolves Python, prepares a private venv on first use,
// launches server.py on 127.0.0.1 with a per-session token, waits for /health and restarts it with backoff.
import { ChildProcess, spawn, spawnSync } from "child_process";
import * as crypto from "crypto";
import * as fs from "fs";
import * as net from "net";
import * as path from "path";
import * as vscode from "vscode";
import { ApiClient } from "./api";
import type { BackendState } from "./protocol";

export const SECRET_GEMINI = "nexura.geminiApiKey";
export const SECRET_GROQ = "nexura.groqApiKey";

const MIN_PYTHON = [3, 11];

function pyVersion(cmd: string): number[] | undefined {
  try {
    const r = spawnSync(cmd, ["-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))"], { encoding: "utf8", timeout: 15000 });
    if (r.status !== 0) return undefined;
    return r.stdout.trim().split(".").map(Number);
  } catch {
    return undefined;
  }
}

function atLeast(v: number[] | undefined): boolean {
  return !!v && (v[0] > MIN_PYTHON[0] || (v[0] === MIN_PYTHON[0] && v[1] >= MIN_PYTHON[1]));
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const port = (srv.address() as net.AddressInfo).port;
      srv.close(() => resolve(port));
    });
  });
}

export class BackendManager implements vscode.Disposable {
  client: ApiClient | undefined;
  state: BackendState = "stopped";
  message = "";
  readonly dataDir: string;
  private proc: ChildProcess | undefined;
  private stopping = false;
  private restarts: number[] = [];
  private starting: Promise<void> | undefined;
  private readonly onChange = new vscode.EventEmitter<void>();
  readonly onDidChange = this.onChange.event;

  constructor(private ctx: vscode.ExtensionContext, private out: vscode.OutputChannel) {
    this.dataDir = (ctx.storageUri ?? ctx.globalStorageUri).fsPath;
  }

  private set(state: BackendState, message = ""): void {
    this.state = state;
    this.message = message;
    this.onChange.fire();
  }

  private backendDir(): string {
    const bundled = this.ctx.asAbsolutePath("backend");
    if (fs.existsSync(path.join(bundled, "server.py"))) return bundled;
    const dev = path.resolve(this.ctx.extensionPath, "..");   // running from the repo (development host)
    if (fs.existsSync(path.join(dev, "server.py"))) return dev;
    throw new Error("The Nexura backend files were not found in the extension.");
  }

  /** setting -> Python extension's interpreter -> private venv (created on first use). */
  async resolvePython(): Promise<string> {
    const cfg = vscode.workspace.getConfiguration("nexura").get<string>("pythonPath")?.trim();
    if (cfg) {
      if (!atLeast(pyVersion(cfg))) throw new Error(`nexura.pythonPath (${cfg}) is not Python ${MIN_PYTHON.join(".")}+.`);
      return cfg;
    }
    try {
      const ext = vscode.extensions.getExtension("ms-python.python");
      if (ext) {
        const api = await ext.activate();
        const folder = vscode.workspace.workspaceFolders?.[0]?.uri;
        const exec: string | undefined = api?.settings?.getExecutionDetails?.(folder)?.execCommand?.[0];
        if (exec && atLeast(pyVersion(exec))) return exec;
      }
    } catch (e) {
      this.out.appendLine(`[backend] could not read the Python extension interpreter: ${e}`);
    }
    return this.ensureVenv();
  }

  private async ensureVenv(): Promise<string> {
    const venv = path.join(this.ctx.globalStorageUri.fsPath, "venv");
    const py = process.platform === "win32" ? path.join(venv, "Scripts", "python.exe") : path.join(venv, "bin", "python");
    const req = path.join(this.backendDir(), "requirements.txt");
    const stamp = crypto.createHash("sha256").update(fs.readFileSync(req)).digest("hex");
    const marker = path.join(venv, ".nexura-requirements");
    if (fs.existsSync(py) && fs.existsSync(marker) && fs.readFileSync(marker, "utf8") === stamp) return py;

    const base = ["python3", "python", "py"].find((c) => atLeast(pyVersion(c)));
    if (!base) {
      throw new Error(`Python ${MIN_PYTHON.join(".")}+ was not found. Install it, or set "nexura.pythonPath".`);
    }
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Nexura: setting up the backend (first run only)", cancellable: false },
      async (progress) => {
        fs.mkdirSync(venv, { recursive: true });
        if (!fs.existsSync(py)) {
          progress.report({ message: "creating a virtual environment..." });
          await this.run(base, ["-m", "venv", venv]);
        }
        progress.report({ message: "installing dependencies (this can take a few minutes)..." });
        await this.run(py, ["-m", "pip", "install", "--disable-pip-version-check", "-r", req]);
        fs.writeFileSync(marker, stamp);
      },
    );
    return py;
  }

  private run(cmd: string, args: string[]): Promise<void> {
    return new Promise((resolve, reject) => {
      const p = spawn(cmd, args, { windowsHide: true });
      let tail = "";
      const keep = (b: Buffer) => {
        this.out.append(b.toString());
        tail = (tail + b.toString()).slice(-800);
      };
      p.stdout.on("data", keep);
      p.stderr.on("data", keep);
      p.on("error", reject);
      p.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`${path.basename(cmd)} ${args.slice(0, 3).join(" ")} failed (exit ${code}):\n${tail}`))));
    });
  }

  private async env(port: number, token: string): Promise<NodeJS.ProcessEnv> {
    const cfg = vscode.workspace.getConfiguration("nexura");
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      NEXURA_TOKEN: token,
      NEXURA_PORT: String(port),
      NEXURA_DATA_DIR: this.dataDir,
      PYTHONUNBUFFERED: "1",
      PYTHONIOENCODING: "utf-8",
      CODER_MODEL_NAME: cfg.get<string>("model.coder") || "gemini-3.1-flash-lite-preview",
      PLANNER_MODEL_NAME: cfg.get<string>("model.planner") || "gemini-3.1-flash-lite-preview",
      NEXURA_EXTRA_PROTECTED: JSON.stringify(cfg.get<string[]>("protectedPaths") ?? []),
    };
    const gemini = await this.ctx.secrets.get(SECRET_GEMINI);
    const groq = await this.ctx.secrets.get(SECRET_GROQ);
    if (gemini) env.GOOGLE_API_KEY = gemini;
    if (groq) env.GROQ_API_KEY = groq;
    return env;
  }

  start(): Promise<void> {
    if (this.starting) return this.starting;
    this.starting = this.doStart().finally(() => (this.starting = undefined));
    return this.starting;
  }

  private async doStart(): Promise<void> {
    this.stopping = false;
    this.set("starting", "starting the Nexura backend...");
    try {
      fs.mkdirSync(this.dataDir, { recursive: true });
      const python = await this.resolvePython();
      const port = await freePort();
      const token = crypto.randomBytes(24).toString("hex");
      const dir = this.backendDir();
      this.out.appendLine(`[backend] ${python} server.py on 127.0.0.1:${port} (data: ${this.dataDir})`);
      const proc = spawn(python, ["server.py"], { cwd: dir, env: await this.env(port, token), windowsHide: true });
      this.proc = proc;
      proc.stdout?.on("data", (b: Buffer) => this.out.append(b.toString()));
      proc.stderr?.on("data", (b: Buffer) => this.out.append(b.toString()));
      proc.on("exit", (code) => this.onExit(proc, code));
      const client = new ApiClient(`http://127.0.0.1:${port}`, token);
      await this.waitHealthy(client, proc);
      this.client = client;
      this.set("ready");
    } catch (e) {
      this.client = undefined;
      this.set("error", e instanceof Error ? e.message : String(e));
      throw e;
    }
  }

  private async waitHealthy(client: ApiClient, proc: ChildProcess): Promise<void> {
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      if (proc.exitCode !== null) throw new Error(`The backend exited during start-up (code ${proc.exitCode}). See the "Nexura" output.`);
      try {
        if ((await client.health()).ok) return;
      } catch {
        /* not up yet */
      }
      await new Promise((r) => setTimeout(r, 250));
    }
    throw new Error("The backend did not become healthy within 60 seconds.");
  }

  private onExit(proc: ChildProcess, code: number | null): void {
    if (proc !== this.proc) return;
    this.proc = undefined;
    this.client = undefined;
    if (this.stopping) {
      this.set("stopped");
      return;
    }
    const now = Date.now();
    this.restarts = this.restarts.filter((t) => now - t < 5 * 60000);
    this.restarts.push(now);
    this.out.appendLine(`[backend] exited unexpectedly (code ${code}); restart ${this.restarts.length}/5`);
    if (this.restarts.length > 5) {
      this.set("error", "The backend keeps crashing. See the Nexura output for details.");
      return;
    }
    this.set("starting", "restarting the backend...");
    setTimeout(() => void this.start().catch(() => undefined), Math.min(1000 * 2 ** this.restarts.length, 15000));
  }

  async stop(): Promise<void> {
    this.stopping = true;
    const p = this.proc;
    this.proc = undefined;
    this.client = undefined;
    if (p && p.exitCode === null) {
      if (process.platform === "win32" && p.pid) spawnSync("taskkill", ["/pid", String(p.pid), "/t", "/f"], { windowsHide: true });
      else p.kill("SIGTERM");
    }
    this.set("stopped");
  }

  async restart(): Promise<void> {
    await this.stop();
    await this.start();
  }

  async ready(): Promise<ApiClient> {
    if (this.state !== "ready") await this.start();
    if (!this.client) throw new Error(this.message || "The Nexura backend is not running.");
    return this.client;
  }

  dispose(): void {
    void this.stop();
    this.onChange.dispose();
  }
}
