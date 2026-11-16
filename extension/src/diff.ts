// Native VS Code diff for proposed changes. "Before" is what is on disk now, "after" is what Nexura wants to write;
// both are served as read-only virtual documents so nothing is written until the user approves.
import * as vscode from "vscode";
import type { DiffView } from "./protocol";

export const DIFF_SCHEME = "nexura-diff";

export class DiffProvider implements vscode.TextDocumentContentProvider {
  private docs = new Map<string, string>();
  private counter = 0;

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.docs.get(uri.toString()) ?? "";
  }

  private virtual(side: "before" | "after", file: string, text: string): vscode.Uri {
    const uri = vscode.Uri.from({ scheme: DIFF_SCHEME, path: `/${side}/${file}`, query: String(++this.counter) });
    this.docs.set(uri.toString(), text);
    return uri;
  }

  async open(d: DiffView): Promise<void> {
    const left = this.virtual("before", d.file, d.before);
    const right = this.virtual("after", d.file, d.after);
    const label = d.change_type === "deleted" ? "deleted" : d.change_type === "created" ? "new file" : "proposed changes";
    await vscode.commands.executeCommand("vscode.diff", left, right, `${d.file} (Nexura: ${label})`, { preview: true });
  }

  clear(): void {
    this.docs.clear();
  }
}
