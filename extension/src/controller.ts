// Drives a run from the editor: starts it, follows its events, asks the user for plan/approval decisions,
// keeps the status bar, output channel and sidebar state in sync, and reports unsaved/edited files to the backend.
import * as path from "path";
import * as vscode from "vscode";
import { ApiClient, ApiError, SseEvent } from "./api";
import { BackendManager, SECRET_GEMINI } from "./backend";
import { DiffProvider } from "./diff";
import type { ApprovalView, LogLine, RunView, UiState } from "./protocol";

const MAX_LOG = 300;

function rel(uri: vscode.Uri): string {
  return vscode.workspace.asRelativePath(uri, false).replace(/\\/g, "/");
}

export class RunController implements vscode.Disposable {
  private run: RunView | undefined;
  private log: LogLine[] = [];
  private logId = 0;
  private abort: AbortController | undefined;
  private syncing: Promise<void> = Promise.resolve();
  private watchers: vscode.Disposable[] = [];
  private pendingEdits = new Set<string>();
  private editTimer: NodeJS.Timeout | undefined;
  private hasKey = false;
  private lastApprovalKey = "";
  readonly status: vscode.StatusBarItem;
  private readonly emitter = new vscode.EventEmitter<void>();
  readonly onDidChange = this.emitter.event;

  constructor(
    private ctx: vscode.ExtensionContext,
    private backend: BackendManager,
    private out: vscode.OutputChannel,
    readonly diffs: DiffProvider,
  ) {
    this.status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
    this.status.command = "workbench.view.extension.nexura";
    this.status.show();
    backend.onDidChange(() => this.render());
    void this.refreshKeyFlag();
    this.render();
  }

  // ── state for the sidebar ──
  getState(): UiState {
    return {
      backend: this.backend.state,
      backendMessage: this.backend.message || undefined,
      run: this.run,
      log: this.log,
      hasGeminiKey: this.hasKey,
      workspace: vscode.workspace.workspaceFolders?.[0]?.name,
    };
  }

  async refreshKeyFlag(): Promise<void> {
    this.hasKey = !!(await this.ctx.secrets.get(SECRET_GEMINI));
    this.render();
  }

  isActive(): boolean {
    return !!this.run && this.run.state !== "finished" && this.run.state !== "idle";
  }

  private add(kind: LogLine["kind"], text: string): void {
    this.log.push({ id: ++this.logId, kind, text });
    if (this.log.length > MAX_LOG) this.log.splice(0, this.log.length - MAX_LOG);
    this.out.appendLine(`[${kind}] ${text}`);
  }

  private render(): void {
    const r = this.run;
    const b = this.backend.state;
    let text = "$(sparkle) Nexura";
    let tip = `Nexura (backend: ${b})`;
    let bg: vscode.ThemeColor | undefined;
    if (b === "error") {
      text = "$(error) Nexura: backend error";
      tip = this.backend.message;
      bg = new vscode.ThemeColor("statusBarItem.errorBackground");
    } else if (b === "starting") text = "$(loading~spin) Nexura: starting";
    else if (r?.state === "awaiting_approval") {
      text = "$(question) Nexura: approval needed";
      bg = new vscode.ThemeColor("statusBarItem.warningBackground");
    } else if (r?.state === "awaiting_plan") {
      text = "$(question) Nexura: review the plan";
      bg = new vscode.ThemeColor("statusBarItem.warningBackground");
    } else if (r && r.state !== "finished") text = "$(sync~spin) Nexura: running";
    else if (r && (r.status === "failed" || r.status === "completed_with_failures")) {
      text = "$(error) Nexura: " + r.status.replace(/_/g, " ");
      bg = new vscode.ThemeColor("statusBarItem.errorBackground");
    } else if (r?.status === "completed") text = "$(check) Nexura: done";
    this.status.text = text;
    this.status.tooltip = tip;
    this.status.backgroundColor = bg;
    this.emitter.fire();
  }

  // ── commands ──
  async startRun(prompt: string): Promise<string | undefined> {
    const folder = vscode.workspace.workspaceFolders?.[0];
    if (!folder) {
      void vscode.window.showErrorMessage("Nexura: open a folder first.");
      return undefined;
    }
    if (!vscode.workspace.isTrusted) {
      void vscode.window.showErrorMessage("Nexura needs a trusted workspace. Use 'Manage Workspace Trust' first.");
      return undefined;
    }
    if (this.isActive()) {
      void vscode.window.showWarningMessage("Nexura: a run is already in progress.");
      return undefined;
    }
    let client: ApiClient;
    try {
      client = await this.backend.ready();
    } catch (e) {
      void vscode.window.showErrorMessage(`Nexura backend: ${e instanceof Error ? e.message : e}`);
      return undefined;
    }
    const { workspace_id } = await client.registerWorkspace(folder.uri.fsPath, vscode.workspace.isTrusted);
    const dirtyFiles = vscode.workspace.textDocuments
      .filter((d) => d.isDirty && d.uri.scheme === "file" && !d.isUntitled)
      .map((d) => rel(d.uri));
    let gitInit = false;
    let dirtyPolicy: "checkpoint" | "abort" = "abort";
    for (;;) {
      try {
        const { run_id } = await client.createRun({
          workspace_id, prompt, git_init: gitInit, dirty_policy: dirtyPolicy, dirty_files: dirtyFiles,
          approval_level: vscode.workspace.getConfiguration("nexura").get<string>("approvalLevel") ?? "ask_risky",
        });
        this.attach(run_id, prompt, client);
        return run_id;
      } catch (e) {
        if (!(e instanceof ApiError)) throw e;
        if (e.code === "NOT_A_REPO") {
          const pick = await vscode.window.showWarningMessage(
            "This folder is not a git repository. Nexura needs git so every change can be checkpointed and undone.",
            { modal: true }, "Initialize git + checkpoint");
          if (!pick) return undefined;
          gitInit = true;
        } else if (e.code === "DIRTY_WORKSPACE") {
          const pick = await vscode.window.showWarningMessage(
            "You have uncommitted changes. Nexura can save a checkpoint of them first and then continue.",
            { modal: true }, "Checkpoint and continue");
          if (!pick) return undefined;
          dirtyPolicy = "checkpoint";
        } else {
          void vscode.window.showErrorMessage(`Nexura: ${e.message}`);
          return undefined;
        }
      }
    }
  }

  private attach(runId: string, prompt: string, client: ApiClient): void {
    this.abort?.abort();
    this.diffs.clear();
    this.lastApprovalKey = "";
    this.log = [];
    this.run = { runId, prompt, state: "running", status: "running", tasks: [], awaitingPlan: false };
    this.add("info", `Run ${runId} started: ${prompt.slice(0, 120)}`);
    this.watchEdits(runId, client);
    const ac = (this.abort = new AbortController());
    void client
      .streamEvents(runId, (e) => this.onEvent(e, client), ac.signal)
      .catch((e) => this.add("error", `Lost connection to the backend: ${e instanceof Error ? e.message : e}`))
      .finally(async () => {
        await this.sync(client);
        this.stopWatching();
        void vscode.commands.executeCommand("workbench.files.action.refreshFilesExplorer");
      });
    this.render();
  }

  private onEvent(e: SseEvent, client: ApiClient): void {
    const d = e.data ?? {};
    switch (e.event) {
      case "plan_ready":
        this.add("info", `Plan ready: ${(d.tasks ?? []).length} steps. Review it in the sidebar.`);
        break;
      case "task_started":
        this.add("info", `Task ${d.task_id}: ${d.title}`);
        break;
      case "tool_call":
        this.add("tool", `${d.tool}`);
        break;
      case "tool_result":
        this.add(d.ok ? "ok" : "warn", `${d.tool}: ${d.ok ? "ok" : (d.code ?? "failed")}${d.summary ? " - " + String(d.summary).slice(0, 120) : ""}`);
        break;
      case "verification":
        this.add(d.ok ? "ok" : "warn", `verification ${d.ok ? "passed" : "failed"}${d.error_type ? " (" + d.error_type + ")" : ""}`);
        break;
      case "task_done":
        this.add("ok", `Task ${d.task_id} done`);
        break;
      case "task_failed":
        this.add("error", `Task ${d.task_id} failed: ${d.reason ?? ""}`);
        break;
      case "approval_required":
        this.add("warn", `Approval needed: ${d.reason}`);
        void this.notifyApproval(d as ApprovalView);
        break;
      case "error":
        this.add("error", String(d.message));
        break;
      case "run_finished":
        this.add(d.run_status === "completed" ? "ok" : "error", `Run finished: ${d.run_status}`);
        break;
    }
    this.syncing = this.syncing.then(() => this.sync(client));
  }

  private async sync(client: ApiClient): Promise<void> {
    if (!this.run) return;
    try {
      const info = await client.getRun(this.run.runId);
      const waiting = info.waiting && typeof info.waiting === "object" ? (info.waiting as ApprovalView) : undefined;
      this.run = {
        ...this.run, state: info.state, status: info.status, tasks: info.tasks, planGoal: info.plan?.goal,
        awaitingPlan: info.state === "awaiting_plan", approval: waiting,
      };
    } catch {
      /* transient: the next event re-syncs */
    }
    this.render();
  }

  private async notifyApproval(a: ApprovalView): Promise<void> {
    const key = `${a.task_id}:${a.reason}:${a.files.join(",")}`;
    if (key === this.lastApprovalKey) return;
    this.lastApprovalKey = key;
    const pick = await vscode.window.showWarningMessage(`Nexura needs your approval: ${a.reason}`, "Review diff", "Approve", "Reject");
    if (pick === "Review diff") await this.openDiff(a.diffs[0]?.file);
    else if (pick === "Approve") await this.approve("approve");
    else if (pick === "Reject") await this.approve("reject");
  }

  async openDiff(file?: string): Promise<void> {
    const d = this.run?.approval?.diffs.find((x) => x.file === file) ?? this.run?.approval?.diffs[0];
    if (d) await this.diffs.open(d);
  }

  private async client(): Promise<ApiClient | undefined> {
    try {
      return await this.backend.ready();
    } catch (e) {
      void vscode.window.showErrorMessage(`Nexura backend: ${e instanceof Error ? e.message : e}`);
      return undefined;
    }
  }

  private async guard<T>(fn: (c: ApiClient, id: string) => Promise<T>): Promise<T | undefined> {
    const c = await this.client();
    if (!c || !this.run) return undefined;
    try {
      const out = await fn(c, this.run.runId);
      await this.sync(c);
      return out;
    } catch (e) {
      void vscode.window.showErrorMessage(`Nexura: ${e instanceof Error ? e.message : e}`);
      return undefined;
    }
  }

  decidePlan(decision: "approve" | "feedback", text?: string) {
    return this.guard((c, id) => c.planDecision(id, decision, text));
  }
  async approve(decision: "approve" | "reject", note?: string) {
    await this.guard((c, id) => c.approve(id, decision, note));
    void vscode.commands.executeCommand("workbench.files.action.refreshFilesExplorer");
  }
  stop() {
    return this.guard((c, id) => c.cancel(id));
  }

  async resume(runId?: string): Promise<void> {
    const c = await this.client();
    const id = runId ?? this.run?.runId;
    if (!c || !id) return;
    try {
      await c.resume(id);
      this.attach(id, this.run?.prompt ?? "(resumed run)", c);
    } catch (e) {
      void vscode.window.showErrorMessage(`Nexura: cannot resume: ${e instanceof Error ? e.message : e}`);
    }
  }

  async undo(scope: "task" | "run"): Promise<void> {
    const c = await this.client();
    const run = this.run;
    if (!c || !run) {
      void vscode.window.showInformationMessage("Nexura: there is no run to undo.");
      return;
    }
    const lastDone = [...run.tasks].reverse().find((t) => t.status === "done");
    if (scope === "task" && !lastDone) {
      void vscode.window.showInformationMessage("Nexura: no completed task to undo.");
      return;
    }
    try {
      let res = await c.undo(run.runId, scope, lastDone?.task_id);
      if (!res.applied && res.conflicts.length) {
        const pick = await vscode.window.showWarningMessage(
          `You edited these files after Nexura changed them. Undoing will overwrite your edits:\n${res.conflicts.join("\n")}`,
          { modal: true }, "Overwrite my edits");
        if (!pick) return;
        res = await c.undo(run.runId, scope, lastDone?.task_id, true);
      }
      if (res.error) void vscode.window.showErrorMessage(`Nexura undo: ${res.error}`);
      else {
        this.add("info", `Undone: restored ${res.restored.length}, deleted ${res.deleted.length}`);
        void vscode.window.showInformationMessage(`Nexura: restored ${res.restored.length} file(s), removed ${res.deleted.length}.`);
        void vscode.commands.executeCommand("workbench.files.action.refreshFilesExplorer");
      }
      this.render();
    } catch (e) {
      void vscode.window.showErrorMessage(`Nexura undo: ${e instanceof Error ? e.message : e}`);
    }
  }

  async openPlan(): Promise<void> {
    if (!this.run) return;
    const file = path.join(this.backend.dataDir, "runs", this.run.runId, "plan.md");
    try {
      await vscode.commands.executeCommand("vscode.open", vscode.Uri.file(file));
    } catch {
      void vscode.window.showInformationMessage("Nexura: the plan file does not exist yet.");
    }
  }

  // ── unsaved/edited files ──
  private watchEdits(runId: string, client: ApiClient): void {
    this.stopWatching();
    this.watchers.push(
      vscode.workspace.onDidChangeTextDocument((e) => {
        if (e.document.uri.scheme !== "file" || e.contentChanges.length === 0 || !this.isActive()) return;
        const r = rel(e.document.uri);
        if (r.startsWith("..") || path.isAbsolute(r) || r.startsWith(".git/")) return;
        this.pendingEdits.add(r);
        this.editTimer ??= setTimeout(() => {
          const files = [...this.pendingEdits];
          this.pendingEdits.clear();
          this.editTimer = undefined;
          client.edits(runId, files).catch(() => undefined);
        }, 500);
      }),
    );
  }

  private stopWatching(): void {
    this.watchers.forEach((w) => w.dispose());
    this.watchers = [];
    if (this.editTimer) clearTimeout(this.editTimer);
    this.editTimer = undefined;
    this.pendingEdits.clear();
  }

  dispose(): void {
    this.abort?.abort();
    this.stopWatching();
    this.status.dispose();
    this.emitter.dispose();
  }
}
