// Messages between the extension host and the sidebar webview. The webview never sees the API token:
// it only exchanges these messages with the host, and the host validates every message it receives.

export interface TaskView {
  task_id: number;
  title: string;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  attempts: number;
  reason: string | null;
}

export interface DiffView {
  file: string;
  change_type: string;
  before: string;
  after: string;
  diff: string;
  lines: number;
}

export interface ApprovalView {
  type: string;
  task_id: number;
  reason: string;
  files: string[];
  diffs: DiffView[];
}

export interface LogLine {
  id: number;
  kind: "info" | "tool" | "ok" | "error" | "warn";
  text: string;
}

export interface RunView {
  runId: string;
  prompt: string;
  /** backend run state: running | awaiting_plan | awaiting_approval | finished */
  state: string;
  /** final/DB status: running | completed | completed_with_failures | failed | cancelled */
  status: string;
  tasks: TaskView[];
  planGoal?: string;
  approval?: ApprovalView;
  awaitingPlan: boolean;
}

export type BackendState = "starting" | "ready" | "error" | "stopped";

export interface ProjectView {
  profile: { languages?: string[]; frameworks?: string[]; test_commands?: string[]; run_commands?: string[]; lint_command?: string | null };
  summary: string | null;
  needsApproval: string[];
  granted: string[];
}

export interface IndexView {
  state: "off" | "idle" | "indexing" | "ready" | "error" | "missing_deps";
  done?: number;
  total?: number | null;
  files?: number;
  chunks?: number;
  message?: string;
}

export interface UiState {
  backend: BackendState;
  backendMessage?: string;
  run?: RunView;
  log: LogLine[];
  hasGeminiKey: boolean;
  workspace?: string;
  project?: ProjectView;
  index?: IndexView;
}

export type HostMessage = { type: "state"; state: UiState };

export type WebviewMessage =
  | { type: "ready" }
  | { type: "startRun"; prompt: string }
  | { type: "stop" }
  | { type: "resume" }
  | { type: "undo"; scope: "task" | "run" }
  | { type: "planDecision"; decision: "approve" | "feedback"; text?: string }
  | { type: "approve"; decision: "approve" | "reject"; note?: string; rejectedFiles?: string[] }
  | { type: "rebuildIndex" }
  | { type: "showSummary" }
  | { type: "editProfile" }
  | { type: "enableIndexing" }
  | { type: "openDiff"; file: string }
  | { type: "openPlan" }
  | { type: "setKey"; provider: "gemini" | "groq" }
  | { type: "showLogs" }
  | { type: "restartBackend" };

const MAX_TEXT = 20000;

const isStr = (v: unknown, max = MAX_TEXT): v is string => typeof v === "string" && v.length <= max;

/** Validates an untrusted message from the webview. Returns undefined for anything unexpected. */
export function parseWebviewMessage(raw: unknown): WebviewMessage | undefined {
  if (typeof raw !== "object" || raw === null) return undefined;
  const m = raw as Record<string, unknown>;
  switch (m.type) {
    case "ready":
    case "stop":
    case "resume":
    case "openPlan":
    case "showLogs":
    case "restartBackend":
    case "rebuildIndex":
    case "showSummary":
    case "editProfile":
    case "enableIndexing":
      return { type: m.type };
    case "startRun":
      return isStr(m.prompt) && m.prompt.trim() ? { type: "startRun", prompt: m.prompt } : undefined;
    case "undo":
      return m.scope === "task" || m.scope === "run" ? { type: "undo", scope: m.scope } : undefined;
    case "planDecision":
      if (m.decision === "approve") return { type: "planDecision", decision: "approve" };
      return m.decision === "feedback" && isStr(m.text) && m.text.trim()
        ? { type: "planDecision", decision: "feedback", text: m.text }
        : undefined;
    case "approve":
      if (m.decision !== "approve" && m.decision !== "reject") return undefined;
      return {
        type: "approve", decision: m.decision, note: isStr(m.note, 2000) ? m.note : undefined,
        rejectedFiles: Array.isArray(m.rejectedFiles) && m.rejectedFiles.length <= 200 && m.rejectedFiles.every((f) => isStr(f, 1024))
          ? (m.rejectedFiles as string[]) : undefined,
      };
    case "openDiff":
      return isStr(m.file, 1024) && m.file.length > 0 ? { type: "openDiff", file: m.file } : undefined;
    case "setKey":
      return m.provider === "gemini" || m.provider === "groq" ? { type: "setKey", provider: m.provider } : undefined;
    default:
      return undefined;
  }
}
