import { useEffect, useRef, useState } from "react";
import type { ApprovalView, HostMessage, IndexView, LogLine, ProjectView, RunView, TaskView, UiState, WebviewMessage } from "../../extension/src/protocol";

const vscode = acquireVsCodeApi();
const send = (m: WebviewMessage) => vscode.postMessage(m);

const EMPTY: UiState = { backend: "starting", log: [], hasGeminiKey: false };

const ICON: Record<TaskView["status"], string> = { pending: "○", running: "◐", done: "✔", failed: "✖", skipped: "–" };

export function App() {
  const [state, setState] = useState<UiState>(EMPTY);

  useEffect(() => {
    const onMessage = (e: MessageEvent<HostMessage>) => {
      if (e.data?.type === "state") setState(e.data.state);
    };
    window.addEventListener("message", onMessage);
    send({ type: "ready" });
    return () => window.removeEventListener("message", onMessage);
  }, []);

  const run = state.run;
  const active = !!run && run.state !== "finished" && run.state !== "idle";

  return (
    <main className="app">
      <BackendBanner state={state} />
      {!state.hasGeminiKey && state.backend !== "error" && <KeyBanner />}
      {state.backend === "ready" && <ProjectCard project={state.project} index={state.index} />}
      <PromptBox disabled={active || state.backend !== "ready"} />
      {run && <RunPanel run={run} active={active} />}
      {run?.approval && <ApprovalCard a={run.approval} />}
      <Log lines={state.log} />
      <footer className="links">
        <button className="link" onClick={() => send({ type: "showLogs" })}>Show logs</button>
        <button className="link" onClick={() => send({ type: "setKey", provider: "gemini" })}>Gemini key</button>
        <button className="link" onClick={() => send({ type: "setKey", provider: "groq" })}>Groq key</button>
      </footer>
    </main>
  );
}

function BackendBanner({ state }: { state: UiState }) {
  if (state.backend === "ready") return null;
  const text = state.backend === "starting" ? state.backendMessage || "Starting the Nexura backend…" : state.backendMessage || "The backend is not running.";
  return (
    <div className={`banner ${state.backend === "error" ? "error" : ""}`} role="status">
      <span>{text}</span>
      {state.backend !== "starting" && <button onClick={() => send({ type: "restartBackend" })}>Restart backend</button>}
    </div>
  );
}

function KeyBanner() {
  return (
    <div className="banner" role="note">
      <span>Add your Gemini API key to start.</span>
      <button onClick={() => send({ type: "setKey", provider: "gemini" })}>Set key</button>
    </div>
  );
}

function ProjectCard({ project, index }: { project?: ProjectView; index?: IndexView }) {
  if (!project && (!index || index.state === "off")) return null;
  const p = project?.profile ?? {};
  const known = [...(p.languages ?? []), ...(p.frameworks ?? [])];
  const pct = index?.total ? Math.floor(((index.done ?? 0) / index.total) * 100) : 0;
  return (
    <section aria-label="Project">
      <h2>Project</h2>
      {known.length > 0 && <p>{known.join(", ")}</p>}
      {p.test_commands?.[0] && <p className="hint">Tests: {p.test_commands[0]}</p>}
      {p.run_commands?.[0] && <p className="hint">Run: {p.run_commands[0]}</p>}
      {project?.needsApproval && project.needsApproval.length > 0 && (
        <p className="hint">Not allowed yet for this workspace: {project.needsApproval.join(", ")} (use Edit profile to review).</p>
      )}
      {project?.summary && <details><summary>Summary</summary><p className="summary">{project.summary}</p></details>}
      {index && index.state === "indexing" && (
        <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Indexing">
          <div className="bar" style={{ width: `${pct}%` }} />
        </div>
      )}
      {index && index.state !== "off" && (
        <p className="hint" aria-live="polite">
          {index.state === "indexing" ? `Indexing ${pct}%` : index.state === "ready" ? `Index ready (${index.files ?? 0} files)` :
            index.state === "missing_deps" ? "Indexing packages are not installed" : index.message || `Index: ${index.state}`}
        </p>
      )}
      <div className="row wrap">
        <button onClick={() => send({ type: "editProfile" })}>Edit profile</button>
        <button onClick={() => send({ type: "showSummary" })}>Summary</button>
        {index?.state === "off" || !index ? (
          <button onClick={() => send({ type: "enableIndexing" })}>Enable indexing</button>
        ) : (
          <button disabled={index.state === "indexing"} onClick={() => send({ type: "rebuildIndex" })}>Rebuild index</button>
        )}
      </div>
    </section>
  );
}

function PromptBox({ disabled }: { disabled: boolean }) {
  const [text, setText] = useState("");
  const submit = () => {
    if (text.trim() && !disabled) {
      send({ type: "startRun", prompt: text.trim() });
      setText("");
    }
  };
  return (
    <section aria-label="New run">
      <label htmlFor="prompt">What should Nexura build or change?</label>
      <textarea
        id="prompt" rows={4} value={text} disabled={disabled} placeholder="e.g. Add a /health endpoint and a test for it"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit(); }}
      />
      <div className="row">
        <button className="primary" disabled={disabled || !text.trim()} onClick={submit}>Start run</button>
        <span className="hint">Ctrl+Enter</span>
      </div>
    </section>
  );
}

function RunPanel({ run, active }: { run: RunView; active: boolean }) {
  const finished = run.state === "finished";
  return (
    <section aria-label="Run">
      <h2>{run.planGoal || run.prompt.slice(0, 80)}</h2>
      <p className="status" aria-live="polite">
        {run.awaitingPlan ? "Waiting for you to review the plan" : run.state === "awaiting_approval" ? "Waiting for your approval" : finished ? `Finished: ${run.status.replace(/_/g, " ")}` : "Running…"}
      </p>
      {run.awaitingPlan && <PlanDecision />}
      <ol className="tasks">
        {run.tasks.map((t) => (
          <li key={t.task_id} className={`task ${t.status}`}>
            <span className="ico" aria-hidden="true">{ICON[t.status]}</span>
            <span className="title">{t.title}</span>
            <span className="sr">{t.status}</span>
            {t.reason && <span className="reason">{t.reason}</span>}
            {t.attempts > 0 && <span className="attempts">{t.attempts} retr{t.attempts === 1 ? "y" : "ies"}</span>}
          </li>
        ))}
      </ol>
      <div className="row wrap">
        {active && <button onClick={() => send({ type: "stop" })}>Stop</button>}
        {finished && run.status !== "completed" && <button onClick={() => send({ type: "resume" })}>Resume</button>}
        {finished && <button onClick={() => send({ type: "undo", scope: "task" })}>Undo last task</button>}
        {finished && <button onClick={() => send({ type: "undo", scope: "run" })}>Undo run</button>}
        <button onClick={() => send({ type: "openPlan" })}>Open plan</button>
      </div>
    </section>
  );
}

function PlanDecision() {
  const [feedback, setFeedback] = useState("");
  const [asking, setAsking] = useState(false);
  return (
    <div className="decision">
      {!asking ? (
        <div className="row">
          <button className="primary" onClick={() => send({ type: "planDecision", decision: "approve" })}>Approve plan</button>
          <button onClick={() => setAsking(true)}>Give feedback</button>
        </div>
      ) : (
        <>
          <label htmlFor="fb">What should change in the plan?</label>
          <textarea id="fb" rows={3} value={feedback} onChange={(e) => setFeedback(e.target.value)} />
          <div className="row">
            <button className="primary" disabled={!feedback.trim()} onClick={() => { send({ type: "planDecision", decision: "feedback", text: feedback.trim() }); setAsking(false); setFeedback(""); }}>Re-plan</button>
            <button onClick={() => setAsking(false)}>Cancel</button>
          </div>
        </>
      )}
    </div>
  );
}

function ApprovalCard({ a }: { a: ApprovalView }) {
  const [note, setNote] = useState("");
  const [rejected, setRejected] = useState<string[]>([]);
  const toggle = (f: string) => setRejected((r) => (r.includes(f) ? r.filter((x) => x !== f) : [...r, f]));
  const perFile = a.diffs.length > 1;
  return (
    <section className="approval" aria-label="Approval needed">
      <h2>Approval needed (task {a.task_id})</h2>
      <p>{a.reason}</p>
      {a.files.length > 0 && (
        <ul className="files">
          {a.diffs.map((d) => (
            <li key={d.file}>
              {perFile && (
                <label className="inline">
                  <input type="checkbox" checked={!rejected.includes(d.file)} onChange={() => toggle(d.file)} />
                  <span className="sr">Approve {d.file}</span>
                </label>
              )}
              <button className="link" onClick={() => send({ type: "openDiff", file: d.file })} title="Open the diff in the editor">
                {d.file}
              </button>
              <span className="badge">{d.change_type}</span>
            </li>
          ))}
        </ul>
      )}
      {perFile && <p className="hint">Untick a file to reject only its changes; the rest is approved.</p>}
      <label htmlFor="note">Note for the agent (optional)</label>
      <input id="note" type="text" value={note} onChange={(e) => setNote(e.target.value)} />
      <div className="row">
        <button className="primary" onClick={() => send({ type: "approve", decision: "approve", note: note || undefined, rejectedFiles: rejected.length ? rejected : undefined })}>
          {rejected.length ? `Approve ${a.diffs.length - rejected.length} of ${a.diffs.length}` : "Approve"}
        </button>
        <button onClick={() => send({ type: "approve", decision: "reject", note: note || undefined })}>Reject</button>
      </div>
    </section>
  );
}

function Log({ lines }: { lines: LogLine[] }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => end.current?.scrollIntoView({ block: "nearest" }), [lines.length]);
  if (lines.length === 0) return null;
  return (
    <section aria-label="Activity">
      <h2>Activity</h2>
      <div className="log" role="log" aria-live="off">
        {lines.slice(-80).map((l) => (
          <div key={l.id} className={`line ${l.kind}`}>{l.text}</div>
        ))}
        <div ref={end} />
      </div>
    </section>
  );
}
