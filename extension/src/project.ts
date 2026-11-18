// The "open project" flow for existing code: shows the detected profile and repo summary, asks (once) before an optional
// executable is allowed for this workspace, manages the local code index (consent for the model download, background progress,
// rebuild) and forwards file changes made outside runs to the backend index.
import * as path from "path";
import * as vscode from "vscode";
import { ApiClient, ApiError, ProfileView } from "./api";
import { BackendManager } from "./backend";
import type { IndexView, ProjectView } from "./protocol";

const CONSENT_KEY = "nexura.modelDownloadConsent";
const OFFLINE_DOCS = "https://huggingface.co/docs/huggingface_hub/guides/download#download-files-to-local-folder";
const SKIP = /(^|\/)(\.git|node_modules|__pycache__|\.venv|venv|dist|build|\.next|\.cache|\.nexura)(\/|$)/;

function rel(uri: vscode.Uri): string {
  return vscode.workspace.asRelativePath(uri, false).replace(/\\/g, "/");
}

export class ProjectManager implements vscode.Disposable {
  project: ProjectView | undefined;
  index: IndexView = { state: "off" };
  readonly indexStatus: vscode.StatusBarItem;
  private workspaceId: string | undefined;
  private poll: NodeJS.Timeout | undefined;
  private watcher: vscode.FileSystemWatcher | undefined;
  private pending = new Set<string>();
  private timer: NodeJS.Timeout | undefined;
  private opened = false;
  private readonly emitter = new vscode.EventEmitter<void>();
  readonly onDidChange = this.emitter.event;

  constructor(private ctx: vscode.ExtensionContext, private backend: BackendManager, private out: vscode.OutputChannel,
              private isRunActive: () => boolean) {
    this.indexStatus = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 49);
    this.indexStatus.command = "nexura.rebuildIndex";
    backend.onDidChange(() => {
      if (backend.state === "ready" && !this.opened) void this.open();
      if (backend.state !== "ready") this.opened = false;
    });
  }

  private get indexingOn(): boolean {
    return !!vscode.workspace.getConfiguration("nexura").get<boolean>("indexing.enabled");
  }

  private fire(): void {
    const s = this.index;
    const pct = s.total ? Math.floor(((s.done ?? 0) / s.total) * 100) : 0;
    this.indexStatus.text = s.state === "indexing" ? `$(sync~spin) Nexura index ${pct}%`
      : s.state === "ready" ? "$(database) Nexura index ready"
      : s.state === "error" ? "$(warning) Nexura index error"
      : s.state === "missing_deps" ? "$(warning) Nexura index: install packages" : "";
    this.indexStatus.tooltip = s.message ?? (s.files !== undefined ? `${s.files} files, ${s.chunks ?? 0} chunks` : "Nexura code index");
    if (this.indexStatus.text) this.indexStatus.show();
    else this.indexStatus.hide();
    this.emitter.fire();
  }

  private async client(): Promise<{ c: ApiClient; id: string } | undefined> {
    const folder = vscode.workspace.workspaceFolders?.[0];
    if (!folder || !vscode.workspace.isTrusted) return undefined;
    try {
      const c = await this.backend.ready();
      this.workspaceId ??= (await c.registerWorkspace(folder.uri.fsPath, true)).workspace_id;
      return { c, id: this.workspaceId };
    } catch (e) {
      this.out.appendLine(`[project] backend not ready: ${e instanceof Error ? e.message : e}`);
      return undefined;
    }
  }

  /** On workspace open (backend ready): profile, one-time executable approval, background indexing. */
  async open(): Promise<void> {
    this.opened = true;
    this.workspaceId = undefined;
    const k = await this.client();
    if (!k) return;
    try {
      this.apply(await k.c.profile(k.id));
      await this.askExecutables(k);
    } catch (e) {
      this.out.appendLine(`[project] profile: ${e instanceof Error ? e.message : e}`);
    }
    this.watch(k);
    if (this.indexingOn) await this.startIndex(false);
  }

  private apply(p: ProfileView): void {
    this.project = { profile: p.profile, summary: p.summary, needsApproval: p.needs_approval, granted: p.granted_commands };
    this.fire();
  }

  /** The profile's commands may need executables that are not on the default list: ask once, per workspace, revocably. */
  private async askExecutables(k: { c: ApiClient; id: string }): Promise<void> {
    const need = this.project?.needsApproval ?? [];
    if (need.length === 0) return;
    const pick = await vscode.window.showInformationMessage(
      `This project's commands use ${need.join(", ")}. Allow Nexura to run ${need.length > 1 ? "them" : "it"} in this workspace? (Only for this folder; you can revoke it by editing the profile.)`,
      "Allow", "Not now");
    if (pick !== "Allow") return;
    for (const exe of need) await k.c.grantCommand(k.id, exe);
    this.apply(await k.c.profile(k.id));
  }

  async profile(): Promise<ProfileView | undefined> {
    const k = await this.client();
    return k ? k.c.profile(k.id) : undefined;
  }

  // ── commands ──
  async editProfile(): Promise<void> {
    const k = await this.client();
    if (!k) return;
    const view = await k.c.profile(k.id);
    const fields: { label: string; key: "test_commands" | "run_commands" | "lint_command"; list: boolean }[] = [
      { label: "Test commands", key: "test_commands", list: true },
      { label: "Run commands", key: "run_commands", list: true },
      { label: "Lint command", key: "lint_command", list: false },
    ];
    const pick = await vscode.window.showQuickPick(
      fields.map((f) => ({ label: f.label, description: [view.profile[f.key]].flat().filter(Boolean).join(" | ") || "(none)", f })),
      { title: "Nexura: Edit Project Profile", placeHolder: "Which field do you want to override? (the detected value is replaced)" });
    if (!pick) return;
    const current = [view.profile[pick.f.key]].flat().filter(Boolean).join(" | ");
    const value = await vscode.window.showInputBox({
      prompt: `${pick.f.label}${pick.f.list ? " (separate several with ' | ')" : ""}. Empty = no command.`, value: current, ignoreFocusOut: true });
    if (value === undefined) return;
    const parts = value.split("|").map((s) => s.trim()).filter(Boolean);
    const next = await k.c.putProfile(k.id, { [pick.f.key]: pick.f.list ? parts : (parts[0] ?? null) });
    this.apply(next);
    await this.askExecutables(k);
    void vscode.window.showInformationMessage("Nexura: project profile updated.");
  }

  async showSummary(): Promise<void> {
    const k = await this.client();
    if (!k) return;
    try {
      let { summary } = await k.c.getSummary(k.id);
      if (!summary) {
        summary = await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: "Nexura: writing a project summary..." },
          async () => (await k.c.makeSummary(k.id)).summary);
      }
      const view = await k.c.profile(k.id);
      this.apply(view);
      const doc = await vscode.workspace.openTextDocument({
        language: "markdown",
        content: `# Project summary\n\n${summary}\n\n## Detected profile\n\n\`\`\`json\n${JSON.stringify(view.profile, null, 2)}\n\`\`\`\n`,
      });
      await vscode.window.showTextDocument(doc, { preview: true });
    } catch (e) {
      void vscode.window.showErrorMessage(`Nexura: cannot show the summary: ${e instanceof ApiError ? e.message : e}`);
    }
  }

  /** Turns code indexing on: optional packages, one-time consent for the local model download, then a background build. */
  async enableIndexing(): Promise<void> {
    const cfg = vscode.workspace.getConfiguration("nexura");
    if (!cfg.get<boolean>("indexing.enabled")) {
      const pick = await vscode.window.showInformationMessage(
        "Code indexing lets Nexura find the right files in larger projects. It stays on this computer: the index is stored outside your project, " +
        "and nothing is sent anywhere. It needs a few extra Python packages and a one-time download of a small local model (about 130 MB).",
        { modal: true }, "Enable indexing");
      if (pick !== "Enable indexing") return;
      await cfg.update("indexing.enabled", true, vscode.ConfigurationTarget.Workspace);
      await cfg.update("retrievalMode", "v2", vscode.ConfigurationTarget.Workspace);
    }
    if (!(await this.ensureModelConsent())) return;
    await this.backend.restart().catch(() => undefined);   // the backend reads these settings at start-up
    await this.open();
  }

  private async ensureModelConsent(): Promise<boolean> {
    if (this.ctx.globalState.get<boolean>(CONSENT_KEY)) return true;
    const pick = await vscode.window.showInformationMessage(
      "Nexura needs to download a small embedding model once (BAAI/bge-small-en-v1.5, about 130 MB, from huggingface.co). " +
      "After that, indexing works offline. Your code is never uploaded.",
      { modal: true }, "Download model", "Offline instructions");
    if (pick === "Offline instructions") {
      void vscode.env.openExternal(vscode.Uri.parse(OFFLINE_DOCS));
      return false;
    }
    if (pick !== "Download model") return false;
    await this.ctx.globalState.update(CONSENT_KEY, true);
    return true;
  }

  hasConsent(): boolean {
    return !!this.ctx.globalState.get<boolean>(CONSENT_KEY);
  }

  async startIndex(rebuild: boolean): Promise<void> {
    if (!this.indexingOn) {
      await this.enableIndexing();
      return;
    }
    const k = await this.client();
    if (!k) return;
    try {
      await k.c.startIndex(k.id, rebuild);
      this.index = { state: "indexing", done: 0, total: 0 };
      this.fire();
      this.pollIndex(k);
    } catch (e) {
      if (e instanceof ApiError && e.code === "INDEX_DEPS_MISSING") {
        this.index = { state: "missing_deps", message: e.message };
        this.fire();
        const pick = await vscode.window.showWarningMessage(
          "Code indexing needs extra Python packages.", "Install them", "Not now");
        if (pick === "Install them") await this.installPackages();
      } else if (e instanceof ApiError && e.code === "INDEXING_DISABLED") {
        await this.enableIndexing();
      } else {
        this.index = { state: "error", message: e instanceof Error ? e.message : String(e) };
        this.fire();
      }
    }
  }

  private async installPackages(): Promise<void> {
    try {
      await this.backend.installIndexPackages();
      await this.backend.restart();
      await this.open();
    } catch (e) {
      void vscode.window.showErrorMessage(`Nexura: could not install the indexing packages: ${e instanceof Error ? e.message : e}`);
    }
  }

  private pollIndex(k: { c: ApiClient; id: string }): void {
    if (this.poll) clearInterval(this.poll);
    const tick = async () => {
      try {
        const s = await k.c.indexStatus(k.id);
        const state = s.state === "indexing" ? "indexing" : s.state === "ready" ? "ready" : s.state === "failed" || s.error ? "error" : s.state === "cancelled" ? "off" : "idle";
        this.index = { state, done: s.done, total: s.total, files: s.files, chunks: s.chunks, message: s.error ?? s.notice ?? undefined };
        if (state === "error" && /ModelNotAvailable|NEXURA_ALLOW_MODEL_DOWNLOAD/.test(s.error ?? "")) {
          this.index.message = "The local model is not installed. Accept the download prompt or install it offline.";
        }
        this.fire();
        if (state !== "indexing" && this.poll) {
          clearInterval(this.poll);
          this.poll = undefined;
        }
      } catch {
        /* transient; the next tick retries */
      }
    };
    this.poll = setInterval(() => void tick(), 1000);
    void tick();
  }

  // ── changes outside runs ──
  private watch(k: { c: ApiClient; id: string }): void {
    this.watcher?.dispose();
    const folder = vscode.workspace.workspaceFolders?.[0];
    if (!folder) return;
    this.watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(folder, "**/*"));
    const note = (uri: vscode.Uri) => {
      const r = rel(uri);
      if (r.startsWith("..") || path.isAbsolute(r) || SKIP.test(r) || !this.indexingOn || this.index.state === "off") return;
      this.pending.add(r);
      this.timer ??= setTimeout(() => void this.flush(k), 1000);
    };
    this.watcher.onDidChange(note);
    this.watcher.onDidCreate(note);
    this.watcher.onDidDelete(note);
  }

  private async flush(k: { c: ApiClient; id: string }): Promise<void> {
    const paths = [...this.pending].slice(0, 1000);
    this.pending.clear();
    this.timer = undefined;
    if (this.isRunActive() || paths.length === 0 || this.index.state === "indexing") return;   // a run re-indexes its own edits
    try {
      await k.c.indexChanges(k.id, paths);
    } catch (e) {
      this.out.appendLine(`[project] index update skipped: ${e instanceof Error ? e.message : e}`);
    }
  }

  dispose(): void {
    if (this.poll) clearInterval(this.poll);
    if (this.timer) clearTimeout(this.timer);
    this.watcher?.dispose();
    this.indexStatus.dispose();
    this.emitter.dispose();
  }
}
