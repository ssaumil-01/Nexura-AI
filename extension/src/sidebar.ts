// The sidebar webview. Strict CSP (nonce, no inline styles), restricted resource roots, and every message from the
// page is validated before it is acted on. The page never receives the backend URL or token.
import * as crypto from "crypto";
import * as vscode from "vscode";
import { HostMessage, parseWebviewMessage, WebviewMessage } from "./protocol";
import type { RunController } from "./controller";

export class SidebarProvider implements vscode.WebviewViewProvider {
  static readonly viewType = "nexura.sidebar";
  private view: vscode.WebviewView | undefined;

  constructor(
    private extensionUri: vscode.Uri,
    private controller: RunController,
    private handle: (m: WebviewMessage) => Promise<void> | void,
  ) {
    controller.onDidChange(() => this.push());
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const root = vscode.Uri.joinPath(this.extensionUri, "dist", "webview");
    view.webview.options = { enableScripts: true, localResourceRoots: [root] };
    view.webview.html = this.html(view.webview, root);
    view.webview.onDidReceiveMessage((raw) => {
      const msg = parseWebviewMessage(raw);
      if (!msg) return;   // ignore anything that is not a well-formed, known message
      if (msg.type === "ready") this.push();
      else void Promise.resolve(this.handle(msg)).catch((e) => vscode.window.showErrorMessage(`Nexura: ${e instanceof Error ? e.message : e}`));
    });
    view.onDidDispose(() => (this.view = undefined));
  }

  push(): void {
    const message: HostMessage = { type: "state", state: this.controller.getState() };
    void this.view?.webview.postMessage(message);
  }

  private html(webview: vscode.Webview, root: vscode.Uri): string {
    const nonce = crypto.randomBytes(16).toString("base64");
    const script = webview.asWebviewUri(vscode.Uri.joinPath(root, "main.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(root, "main.css"));
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}'`,
      `img-src ${webview.cspSource} data:`,
      `font-src ${webview.cspSource}`,
    ].join("; ");
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${style}">
<title>Nexura</title>
</head>
<body>
<div id="root"></div>
<script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
  }
}
