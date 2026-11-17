import * as vscode from "vscode";
import { BackendManager, SECRET_GEMINI, SECRET_GROQ } from "./backend";
import { RunController } from "./controller";
import { DIFF_SCHEME, DiffProvider } from "./diff";
import type { WebviewMessage } from "./protocol";
import { SidebarProvider } from "./sidebar";

/** Returned from activate() so the end-to-end test can drive the extension without UI prompts. */
export interface NexuraApi {
  controller: RunController;
  backend: BackendManager;
  waitReady(): Promise<void>;
}

async function setKey(ctx: vscode.ExtensionContext, backend: BackendManager, controller: RunController, secret: string, label: string) {
  const value = await vscode.window.showInputBox({ prompt: `${label} API key`, password: true, ignoreFocusOut: true });
  if (!value) return;
  await ctx.secrets.store(secret, value.trim());   // SecretStorage only: never settings, logs or the repo
  await controller.refreshKeyFlag();
  void vscode.window.showInformationMessage(`Nexura: ${label} key saved.`);
  if (backend.state !== "stopped") await backend.restart().catch(() => undefined);   // the backend reads keys at start-up
}

export function activate(context: vscode.ExtensionContext): NexuraApi {
  const out = vscode.window.createOutputChannel("Nexura");
  const backend = new BackendManager(context, out);
  const diffs = new DiffProvider();
  const controller = new RunController(context, backend, out, diffs);

  const handle = async (m: WebviewMessage): Promise<void> => {
    switch (m.type) {
      case "startRun": await controller.startRun(m.prompt); break;
      case "stop": await controller.stop(); break;
      case "resume": await controller.resume(); break;
      case "undo": await controller.undo(m.scope); break;
      case "planDecision": await controller.decidePlan(m.decision, m.text); break;
      case "approve": await controller.approve(m.decision, m.note); break;
      case "openDiff": await controller.openDiff(m.file); break;
      case "openPlan": await controller.openPlan(); break;
      case "setKey": await setKey(context, backend, controller, m.provider === "gemini" ? SECRET_GEMINI : SECRET_GROQ, m.provider === "gemini" ? "Gemini" : "Groq"); break;
      case "showLogs": out.show(true); break;
      case "restartBackend": await backend.restart().catch(() => undefined); break;
    }
  };

  const sidebar = new SidebarProvider(context.extensionUri, controller, handle);
  context.subscriptions.push(
    out, backend, controller, controller.status,
    vscode.workspace.registerTextDocumentContentProvider(DIFF_SCHEME, diffs),
    vscode.window.registerWebviewViewProvider(SidebarProvider.viewType, sidebar),
    vscode.commands.registerCommand("nexura.startRun", async (prompt?: string) => {
      const text = typeof prompt === "string" ? prompt : await vscode.window.showInputBox({
        prompt: "What should Nexura build or change?", placeHolder: "e.g. Add a /health endpoint and a test for it", ignoreFocusOut: true });
      if (text?.trim()) await controller.startRun(text.trim());
    }),
    vscode.commands.registerCommand("nexura.stopRun", () => controller.stop()),
    vscode.commands.registerCommand("nexura.resumeRun", () => controller.resume()),
    vscode.commands.registerCommand("nexura.undoLastTask", () => controller.undo("task")),
    vscode.commands.registerCommand("nexura.undoRun", () => controller.undo("run")),
    vscode.commands.registerCommand("nexura.openPlan", () => controller.openPlan()),
    vscode.commands.registerCommand("nexura.setGeminiKey", () => setKey(context, backend, controller, SECRET_GEMINI, "Gemini")),
    vscode.commands.registerCommand("nexura.setGroqKey", () => setKey(context, backend, controller, SECRET_GROQ, "Groq")),
    vscode.commands.registerCommand("nexura.showLogs", () => out.show(true)),
    vscode.commands.registerCommand("nexura.restartBackend", () => backend.restart()),
    vscode.commands.registerCommand("nexura.debugRetrieval", () => controller.debugRetrieval()),
  );

  const started = backend.start().catch((e) => out.appendLine(`[backend] failed to start: ${e instanceof Error ? e.message : e}`));
  return { controller, backend, waitReady: () => started };
}

export function deactivate(): void {
  // BackendManager.dispose() (registered in subscriptions) stops the Python process.
}
