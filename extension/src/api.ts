// Typed client for the local Nexura backend. Lives in the extension host only: the token never reaches the webview.
import type { ApprovalView, TaskView } from "./protocol";

export class ApiError extends Error {
  constructor(public status: number, public code: string | undefined, message: string, public detail?: unknown) {
    super(message);
  }
}

export interface RunInfo {
  run_id: string;
  status: string;
  state: string;
  error: string | null;
  prompt: string;
  tasks: TaskView[];
  waiting: string | ApprovalView | null;
  summary: Record<string, unknown> | null;
  plan: { goal?: string } | null;
}

export interface SseEvent {
  id: number;
  event: string;
  data: any;
}

export interface UndoResult {
  applied: boolean;
  restored: string[];
  deleted: string[];
  conflicts: string[];
  error: string | null;
}

export interface RetrievalResult {
  path: string;
  lines: [number, number];
  symbol: string;
  kind: string;
  score: number;
  sources: string[];
  reason: string;
  snippet: string;
}

export interface ProfileView {
  profile: { languages?: string[]; frameworks?: string[]; test_commands?: string[]; run_commands?: string[]; lint_command?: string | null;
             package_managers?: string[]; entry_points?: string[]; subprojects?: string[] };
  detected: Record<string, unknown>;
  overrides: Record<string, unknown>;
  granted_commands: string[];
  approvable_commands: string[];
  needs_approval: string[];
  summary: string | null;
}

export interface IndexStatus {
  state: string;
  done?: number;
  total?: number | null;
  files?: number;
  chunks?: number;
  error?: string | null;
  notice?: string | null;
}

export interface StartRunBody {
  workspace_id: string;
  prompt: string;
  approval_level: string;
  dirty_policy: "checkpoint" | "abort";
  dirty_files: string[];
  git_init: boolean;
  baseline?: boolean;
}

export class ApiClient {
  constructor(private baseUrl: string, private token: string) {}

  private async req<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    const res = await fetch(this.baseUrl + path, {
      method,
      signal,
      headers: { Authorization: `Bearer ${this.token}`, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let data: any;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { detail: text };
    }
    if (!res.ok) {
      const d = data?.detail;
      const message = typeof d === "string" ? d : d?.message ?? `HTTP ${res.status}`;
      throw new ApiError(res.status, typeof d === "object" ? d?.code : undefined, message, d);
    }
    return data as T;
  }

  health() {
    return this.req<{ ok: boolean; active_runs: number }>("GET", "/health");
  }
  registerWorkspace(path: string, trusted: boolean) {
    return this.req<{ workspace_id: string }>("POST", "/workspaces", { path, trusted });
  }
  createRun(body: StartRunBody) {
    return this.req<{ run_id: string }>("POST", "/runs", body);
  }
  getRun(id: string) {
    return this.req<RunInfo>("GET", `/runs/${id}`);
  }
  planDecision(id: string, decision: "approve" | "feedback", text?: string) {
    return this.req("POST", `/runs/${id}/plan`, { decision, text });
  }
  approve(id: string, decision: "approve" | "reject", note?: string, rejectedFiles: string[] = []) {
    return this.req("POST", `/runs/${id}/approve`, { decision, note, rejected_files: rejectedFiles });
  }
  cancel(id: string) {
    return this.req("POST", `/runs/${id}/cancel`);
  }
  resume(id: string) {
    return this.req("POST", `/runs/${id}/resume`);
  }
  undo(id: string, scope: "task" | "run", taskId?: number, confirm = false) {
    return this.req<UndoResult>("POST", `/runs/${id}/undo`, { scope, task_id: taskId, confirm_conflicts: confirm });
  }
  edits(id: string, files: string[]) {
    return this.req("POST", `/runs/${id}/edits`, { files });
  }
  profile(workspaceId: string) {
    return this.req<ProfileView>("GET", `/workspaces/${workspaceId}/profile`);
  }
  putProfile(workspaceId: string, overrides: Record<string, unknown>) {
    return this.req<ProfileView>("PUT", `/workspaces/${workspaceId}/profile`, { overrides });
  }
  grantCommand(workspaceId: string, exe: string) {
    return this.req<{ granted: string[] }>("POST", `/workspaces/${workspaceId}/commands`, { exe });
  }
  getSummary(workspaceId: string) {
    return this.req<{ summary: string | null; version: number | null }>("GET", `/workspaces/${workspaceId}/summary`);
  }
  makeSummary(workspaceId: string) {
    return this.req<{ summary: string; version: number }>("POST", `/workspaces/${workspaceId}/summary`);
  }
  startIndex(workspaceId: string, rebuild: boolean) {
    return this.req("POST", `/workspaces/${workspaceId}/index`, { rebuild });
  }
  indexStatus(workspaceId: string) {
    return this.req<IndexStatus>("GET", `/workspaces/${workspaceId}/index`);
  }
  indexChanges(workspaceId: string, paths: string[]) {
    return this.req("POST", `/workspaces/${workspaceId}/index/changes`, { paths });
  }
  retrieve(workspaceId: string, q: string, k = 8) {
    return this.req<{ query: string; results: RetrievalResult[] }>(
      "GET", `/workspaces/${workspaceId}/retrieve?q=${encodeURIComponent(q)}&k=${k}`);
  }

  /**
   * Streams SSE events. Reconnects with Last-Event-ID after a dropped connection until `run_finished`
   * arrives or the signal aborts. Resolves when the run has finished (or was aborted).
   */
  async streamEvents(id: string, onEvent: (e: SseEvent) => void, signal: AbortSignal, startAfter = 0): Promise<void> {
    let last = startAfter;
    let failures = 0;
    while (!signal.aborted) {
      try {
        const res = await fetch(`${this.baseUrl}/runs/${id}/events`, {
          signal,
          headers: { Authorization: `Bearer ${this.token}`, Accept: "text/event-stream", "Last-Event-ID": String(last) },
        });
        if (!res.ok || !res.body) throw new ApiError(res.status, undefined, `events HTTP ${res.status}`);
        failures = 0;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        let cur: Partial<SseEvent> = {};
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let nl: number;
          while ((nl = buf.search(/\r?\n/)) >= 0) {
            const line = buf.slice(0, nl);
            buf = buf.slice(buf[nl] === "\r" ? nl + 2 : nl + 1);
            if (line === "") {
              if (cur.event && cur.id !== undefined) {
                last = cur.id;
                onEvent(cur as SseEvent);
                if (cur.event === "run_finished") return;
              }
              cur = {};
            } else if (line.startsWith("id:")) cur.id = Number(line.slice(3).trim());
            else if (line.startsWith("event:")) cur.event = line.slice(6).trim();
            else if (line.startsWith("data:")) {
              try {
                cur.data = JSON.parse(line.slice(5).trim());
              } catch {
                cur.data = {};
              }
            }
          }
        }
        // stream ended without run_finished: the server may have restarted, so reconnect
      } catch (e) {
        if (signal.aborted) return;
        if (e instanceof ApiError && (e.status === 401 || e.status === 404)) throw e;
        failures++;
        if (failures > 8) throw e;
      }
      await new Promise((r) => setTimeout(r, Math.min(500 * 2 ** failures, 5000)));
    }
  }
}
