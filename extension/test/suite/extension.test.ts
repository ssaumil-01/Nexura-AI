import * as assert from "assert";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import type { NexuraApi } from "../../src/extension";

const ws = process.env.NEXURA_TEST_WORKSPACE as string;

async function until<T>(what: string, fn: () => T | undefined | false, ms = 90000): Promise<T> {
  const end = Date.now() + ms;
  for (;;) {
    const v = fn();
    if (v) return v;
    if (Date.now() > end) throw new Error(`timed out waiting for: ${what}`);
    await new Promise((r) => setTimeout(r, 100));
  }
}

suite("Nexura end-to-end (fake LLM)", () => {
  let api: NexuraApi;

  suiteSetup(async () => {
    const ext = vscode.extensions.getExtension("nexura-local.nexura-ai");
    assert.ok(ext, "extension is installed in the test host");
    api = (await ext.activate()) as NexuraApi;
    await api.waitReady();
  });

  test("backend starts and is healthy", () => {
    assert.strictEqual(api.backend.state, "ready", api.backend.message);
  });

  test("plan -> approve -> build -> verify -> undo", async () => {
    const runId = await api.controller.startRun("make a file");
    assert.ok(runId, "run started");
    await until("plan review", () => api.controller.getState().run?.awaitingPlan);
    assert.ok(api.controller.getState().run!.tasks.length >= 1, "the plan is shown");
    await api.controller.decidePlan("approve");
    const run = await until("run to finish", () => api.controller.getState().run?.state === "finished" && api.controller.getState().run);
    assert.strictEqual(run.status, "completed");
    assert.deepStrictEqual(run.tasks.map((t) => t.status), ["done"]);
    assert.strictEqual(fs.readFileSync(path.join(ws, "hello.txt"), "utf8").trim(), "Hello from Nexura");
    assert.ok(api.controller.status.text.includes("done"), api.controller.status.text);

    await api.controller.undo("run");                       // restores the exact pre-run state
    await until("file removed by undo", () => !fs.existsSync(path.join(ws, "hello.txt")));
  });

  test("a risky change asks for approval and shows a native diff", async () => {
    await api.controller.startRun("add a risky dependency manifest");
    await until("plan review", () => api.controller.getState().run?.awaitingPlan);
    await api.controller.decidePlan("approve");
    const a = await until("approval request", () => api.controller.getState().run?.approval);
    assert.ok(a.files.includes("package.json"), `files: ${a.files}`);
    assert.ok(a.reason.includes("dependency manifest"), a.reason);
    assert.ok(!fs.existsSync(path.join(ws, "package.json")), "nothing is written before approval");

    await api.controller.openDiff("package.json");
    const diffTab = vscode.window.tabGroups.all.flatMap((g) => g.tabs).find((t) => t.label.includes("package.json (Nexura"));
    assert.ok(diffTab, "the diff editor is open");

    await api.controller.approve("approve");
    const run = await until("run to finish", () => api.controller.getState().run?.state === "finished" && api.controller.getState().run);
    assert.strictEqual(run.status, "completed");
    assert.ok(fs.existsSync(path.join(ws, "package.json")));
  });

  test("webview messages are validated", async () => {
    const { parseWebviewMessage } = await import("../../src/protocol");
    assert.strictEqual(parseWebviewMessage({ type: "rm -rf" }), undefined);
    assert.strictEqual(parseWebviewMessage({ type: "startRun", prompt: "  " }), undefined);
    assert.strictEqual(parseWebviewMessage("nope"), undefined);
    assert.deepStrictEqual(parseWebviewMessage({ type: "undo", scope: "run" }), { type: "undo", scope: "run" });
  });
});
