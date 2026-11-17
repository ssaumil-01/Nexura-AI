"""Phase 5: git checkpoints, undo, approvals, protected paths, locks, change detection, secrets, trust."""
import hashlib
import subprocess

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command

import config
from core import checkpoints, validator
from core.checkpoints import GitError
from core.runner import DirtyWorkspace, start_run, undo
from core.schemas import Action, ProjectContext
from core.state_manager import StateManager, WorkspaceBusy, make_checkpointer
from core.task_manager import load_tasks
from tests.conftest import ALWAYS_OK


def git(root, *a):
    return subprocess.run(["git", *a], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def repo_state(root):
    return {"head": git(root, "rev-parse", "HEAD"), "branches": git(root, "branch", "--list"),
            "staged": git(root, "diff", "--cached", "--name-only"), "status": git(root, "status", "--porcelain")}


def plan_of(*titles):
    steps = [{"step_id": i + 1, "title": t, "description": "d", "dependencies": [], "acceptance": ALWAYS_OK} for i, t in enumerate(titles)]
    return {"project_id": "p", "goal": "g", "steps": steps}


def msg(*calls):
    return AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"c{i}"} for i, (n, a) in enumerate(calls)])


COMPLETE = ("task_complete", {"summary": "ok"})


@pytest.fixture
def env(tmp_path, monkeypatch, fake_llm, git_repo):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    import agents.llm as llm_mod
    import core.orchestrator as orch
    monkeypatch.setattr(llm_mod, "_override", fake_llm)
    holder = {"plan": plan_of("one")}

    def stub_planner(state, config):
        ctx = config["configurable"]["project"]
        p = holder["plan"]
        ctx.state_manager.save_plan(ctx.run_id, p, load_tasks(p))
        return {"plan": p, "tasks": p["steps"], "current_task_index": 0, "plan_feedback": None,
                "task_status": {str(t["step_id"]): "pending" for t in p["steps"]}, "task_errors": {}, "run_status": "running"}
    monkeypatch.setattr(orch, "planner_node", stub_planner)

    class E:
        sm = StateManager(tmp_path / "data" / "nexura.db")
        ws = git_repo
        llm = fake_llm
        plan = holder

        @staticmethod
        def start(script, plan=None, level="ask_risky", **kw):
            E.llm.script[:] = script
            E.plan["plan"] = plan or plan_of("one")
            ctx, state = start_run(E.sm, "proj", E.ws, "do it", approval_level=level, **kw)
            graph = orch.build_graph(checkpointer=make_checkpointer(tmp_path / "data" / f"ck_{ctx.run_id}.db"))
            cfg = {"configurable": {"thread_id": ctx.run_id, "project": ctx}, "recursion_limit": 300}
            out = graph.invoke(state, cfg)                 # -> plan review interrupt
            out = graph.invoke(Command(resume="yes"), cfg)  # -> runs (or an approval interrupt)
            return ctx, graph, cfg, out
    return E


def interrupt_payload(out):
    return out["__interrupt__"][0].value


# ─── checkpoints ────────────────────────────────────────────

def test_snapshot_includes_untracked_respects_gitignore_and_leaves_repo_untouched(git_repo):
    (git_repo / ".gitignore").write_text("ignored.txt\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-qm", "ignore")
    (git_repo / "untracked.txt").write_text("u")
    (git_repo / "ignored.txt").write_text("i")
    (git_repo / "README.md").write_text("changed, unstaged\n")
    (git_repo / "staged.txt").write_text("s")
    git(git_repo, "add", "staged.txt")
    before = repo_state(git_repo)
    commit = checkpoints.snapshot(git_repo, "run1", 1)
    assert repo_state(git_repo) == before                      # HEAD, branches, index, working tree all untouched
    files = git(git_repo, "ls-tree", "-r", "--name-only", commit).splitlines()
    assert "untracked.txt" in files and "staged.txt" in files and "README.md" in files and "ignored.txt" not in files
    assert git(git_repo, "show", f"{commit}:README.md") == "changed, unstaged"
    assert checkpoints.has_checkpoint(git_repo, "run1", 1) and not checkpoints.has_checkpoint(git_repo, "run1", 2)
    assert git(git_repo, "for-each-ref", "refs/nexura") .endswith("refs/nexura/run1/1")


def test_repo_detection_dirty_files_and_init(tmp_path, git_repo):
    assert checkpoints.is_repo(git_repo)
    (git_repo / "sub").mkdir()
    assert not checkpoints.is_repo(git_repo / "sub")           # inside a repo is not a repo root
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("x")
    assert not checkpoints.is_repo(plain)
    with pytest.raises(GitError) as e:
        checkpoints.require_repo(plain)
    assert e.value.code == "NOT_A_REPO"
    checkpoints.init_repo(plain)
    assert checkpoints.is_repo(plain) and checkpoints.dirty_files(plain) == []
    (plain / "new.txt").write_text("n")
    (plain / "a.txt").write_text("changed")
    assert sorted(checkpoints.dirty_files(plain)) == ["a.txt", "new.txt"]


def test_start_run_git_rules(env, tmp_path):
    plain = tmp_path / "plainws"
    with pytest.raises(GitError) as e:
        start_run(env.sm, "p2", plain, "x")
    assert e.value.code == "NOT_A_REPO"
    ctx, _ = start_run(env.sm, "p2", plain, "x", git_init=True)           # user-approved init
    assert checkpoints.is_repo(plain)
    env.sm.update_run(ctx.run_id, "failed")
    (env.ws / "dirty.txt").write_text("d")
    with pytest.raises(DirtyWorkspace):
        start_run(env.sm, "proj", env.ws, "x", dirty_policy="abort")
    ctx, _ = start_run(env.sm, "proj", env.ws, "x", dirty_policy="checkpoint")
    assert checkpoints.has_checkpoint(env.ws, ctx.run_id, "base")           # user's uncommitted state is remembered


# ─── undo ───────────────────────────────────────────────────

def two_task_run(env):
    plan = plan_of("edit and create", "more")
    script = [
        msg(("read_file", {"target_file": "README.md"})),
        msg(("patch_file", {"target_file": "README.md", "search_text": "hi", "replace_text": "hello"}),
            ("create_file", {"target_file": "a.txt", "content": "A"}), COMPLETE),
        msg(("create_file", {"target_file": "src/b.txt", "content": "B"}), COMPLETE),
    ]
    return env.start(script, plan)


def test_full_run_checkpoints_and_exact_undo(env):
    before = repo_state(env.ws)
    ctx, graph, cfg, out = two_task_run(env)
    assert graph.get_state(cfg).values["run_status"] == "completed"
    assert (env.ws / "README.md").read_text() == "hello\n" and (env.ws / "src" / "b.txt").exists()
    assert repo_state(env.ws)["head"] == before["head"] and repo_state(env.ws)["branches"] == before["branches"]
    assert repo_state(env.ws)["staged"] == ""                                   # user's index never touched
    assert checkpoints.has_checkpoint(env.ws, ctx.run_id, 1) and checkpoints.has_checkpoint(env.ws, ctx.run_id, 2)

    # undo only task 2
    r = undo(env.sm, ctx.run_id, 2)
    assert r.applied and r.deleted == ["src/b.txt"] and not (env.ws / "src" / "b.txt").exists()
    assert (env.ws / "a.txt").exists() and (env.ws / "README.md").read_text() == "hello\n"
    # undo the whole run -> exactly the pre-run state (src/b.txt already gone; ignore that conflict)
    r = undo(env.sm, ctx.run_id, None, confirm_conflicts=True)
    assert r.applied
    assert (env.ws / "README.md").read_text() == "hi\n" and not (env.ws / "a.txt").exists()
    assert repo_state(env.ws)["status"] == before["status"]


def test_undo_warns_about_user_edits_and_needs_confirmation(env):
    ctx, graph, cfg, out = two_task_run(env)
    (env.ws / "a.txt").write_text("the user edited this afterwards")
    r = undo(env.sm, ctx.run_id, None)
    assert not r.applied and r.conflicts == ["a.txt"]
    assert (env.ws / "a.txt").read_text() == "the user edited this afterwards" and (env.ws / "README.md").read_text() == "hello\n"
    r = undo(env.sm, ctx.run_id, None, confirm_conflicts=True)
    assert r.applied and not (env.ws / "a.txt").exists() and (env.ws / "README.md").read_text() == "hi\n"
    assert undo(env.sm, "nope").error


# ─── approvals ──────────────────────────────────────────────

def test_env_and_git_can_never_be_written_even_with_approval(env):
    script = [msg(("create_file", {"target_file": ".env", "content": "K=1"}), ("create_file", {"target_file": ".git/hooks/pre-commit", "content": "x"})),
              msg(COMPLETE)]
    ctx, graph, cfg, out = env.start(script, level="auto_within_limits")
    assert "__interrupt__" not in out and not (env.ws / ".env").exists()
    for f in (".env", ".git/hooks/pre-commit"):
        r = validator.validate(Action(action="create_file", path=f, content="x"), ProjectContext(run_id="r", root=env.ws, allow_protected=True))
        assert r.code == "NOT_WRITABLE"


def test_delete_requires_approval_and_reject_keeps_the_file(env):
    (env.ws / "old.txt").write_text("keep me")
    script = [msg(("delete_file", {"target_file": "old.txt"})), msg(("list_directory", {}), COMPLETE)]
    ctx, graph, cfg, out = env.start(script)
    p = interrupt_payload(out)
    assert p["type"] == "approval" and "delete: old.txt" in p["reason"] and p["files"] == ["old.txt"]
    assert p["diffs"][0]["change_type"] == "deleted" and "-keep me" in p["diffs"][0]["diff"]
    out = graph.invoke(Command(resume={"decision": "reject", "note": "no, keep it"}), cfg)
    assert (env.ws / "old.txt").exists() and graph.get_state(cfg).values["run_status"] == "completed"
    assert "REJECTED by user: no, keep it" in str(env.llm.calls[1][-1].content)


def test_delete_approved_runs_and_is_audited_as_user(env):
    (env.ws / "old.txt").write_text("bye")
    ctx, graph, cfg, out = env.start([msg(("delete_file", {"target_file": "old.txt"}), COMPLETE)])
    graph.invoke(Command(resume={"decision": "approve"}), cfg)
    assert not (env.ws / "old.txt").exists()
    fc = env.sm.list_file_changes(ctx.run_id)[0]
    assert (fc["file"], fc["change_type"], fc["approval_source"], fc["sha256_after"]) == ("old.txt", "deleted", "user", None)
    audit = [r for r in ctx.logger.read() if r["agent"] == "audit"]
    assert audit and "approval=user" in audit[0]["output_summary"] and "old.txt" in audit[0]["output_summary"]
    r = undo(env.sm, ctx.run_id)
    assert r.applied and (env.ws / "old.txt").read_text() == "bye"      # undo restores a deleted file


def test_approval_levels(env):
    edit = [msg(("create_file", {"target_file": "x.txt", "content": "1"}), COMPLETE)]
    ctx, graph, cfg, out = env.start(list(edit), level="ask_risky")
    assert "__interrupt__" not in out and (env.ws / "x.txt").exists()             # plain edit: no question
    (env.ws / "x.txt").unlink()
    env.sm.update_run(ctx.run_id, "completed")
    ctx, graph, cfg, out = env.start(list(edit), level="ask_every_edit")
    assert interrupt_payload(out)["type"] == "approval" and not (env.ws / "x.txt").exists()
    graph.invoke(Command(resume={"decision": "approve"}), cfg)
    assert (env.ws / "x.txt").exists()
    env.sm.update_run(ctx.run_id, "completed")
    # manifests are risky at ask_risky, automatic at auto_within_limits
    man = [msg(("create_file", {"target_file": "package.json", "content": "{}"}), COMPLETE)]
    ctx, graph, cfg, out = env.start(list(man), level="ask_risky")
    assert "dependency manifest: package.json" in interrupt_payload(out)["reason"]
    (env.ws / "package.json").unlink(missing_ok=True)
    env.sm.update_run(ctx.run_id, "cancelled")
    ctx, graph, cfg, out = env.start(list(man), level="auto_within_limits")
    assert "__interrupt__" not in out and (env.ws / "package.json").exists()


def test_protected_path_needs_approval_at_every_level(env):
    script = [msg(("create_file", {"target_file": "yarn.lock", "content": "lock"}), COMPLETE)]
    ctx, graph, cfg, out = env.start(script, level="auto_within_limits")
    assert "protected path: yarn.lock" in interrupt_payload(out)["reason"] and not (env.ws / "yarn.lock").exists()
    graph.invoke(Command(resume={"decision": "approve"}), cfg)
    assert (env.ws / "yarn.lock").read_text() == "lock" and not ctx.allow_protected   # approval is scoped to that action


def test_install_needs_approval_and_global_installs_are_refused(env):
    script = [msg(("install_dependency", {"command": "pip install requests"})), msg(("install_dependency", {"command": "pip install --user x"})),
              msg(COMPLETE)]
    ctx, graph, cfg, out = env.start(script, level="auto_within_limits")
    assert "install packages: pip install requests" in interrupt_payload(out)["reason"]
    graph.invoke(Command(resume={"decision": "reject", "note": "no installs"}), cfg)
    assert "BAD_INSTALL" in str(env.llm.calls[2][-1].content) or "BAD_INSTALL" in str(env.llm.calls[1][-1].content)


def test_repeated_rejections_fail_the_task(env):
    script = [msg(("delete_file", {"target_file": "old.txt"}), COMPLETE) for _ in range(5)]
    (env.ws / "old.txt").write_text("x")
    ctx, graph, cfg, out = env.start(script)
    for _ in range(config.MAX_RETRIES):
        if "__interrupt__" not in out:
            break
        out = graph.invoke(Command(resume={"decision": "reject"}), cfg)
    v = graph.get_state(cfg).values
    assert v["task_errors"]["1"].startswith("APPROVAL_REJECTED") and (env.ws / "old.txt").exists()


def test_size_limits_trigger_approval_even_in_auto_mode(env, monkeypatch):
    monkeypatch.setattr(config, "RISK_MAX_LINES", 5)
    script = [msg(("create_file", {"target_file": "big.txt", "content": "\n".join(str(i) for i in range(20))}), COMPLETE)]
    ctx, graph, cfg, out = env.start(script, level="auto_within_limits")
    assert "limits: more than 5 changed lines" in interrupt_payload(out)["reason"]


# ─── dirty files, trust, ignore, secrets ────────────────────

def test_dirty_editor_files_are_refused(env):
    (env.ws / "open.py").write_text("x")
    ctx, graph, cfg, out = env.start([msg(("read_file", {"target_file": "open.py"}), ("write_file", {"target_file": "open.py", "content": "y"})),
                                      msg(COMPLETE)], dirty_files=["Open.py"])
    assert (env.ws / "open.py").read_text() == "x"
    assert "DIRTY_FILE" in "".join(str(m.content) for m in env.llm.calls[1])


def test_untrusted_workspace_rejects_every_mutation_but_allows_reads(env):
    ctx = ProjectContext(run_id="r", root=env.ws, trusted=False)
    for a in (Action(action="create_file", path="n.txt", content="1"), Action(action="run_command", command="python --version"),
              Action(action="create_directory", path="d"), Action(action="install_dependency", command="pip install x")):
        assert validator.validate(a, ctx).code == "UNTRUSTED_WORKSPACE", a.action
    assert validator.validate(Action(action="read_file", path="README.md"), ctx).ok
    from core import execution_manager as em
    assert em.run(Action(action="create_file", path="n.txt", content="1"), ctx).code == "UNTRUSTED_WORKSPACE"
    ctx2, _ = start_run(env.sm, "proj", env.ws, "x", trusted=False)
    assert ctx2.trusted is False


def test_ignored_and_secret_files(env):
    (env.ws / ".gitignore").write_text("generated/\n*.log\n")
    git(env.ws, "add", ".gitignore")
    git(env.ws, "commit", "-qm", "gi")
    (env.ws / ".env").write_text("API_KEY=supersecretvalue123")
    (env.ws / "keys.py").write_text('TOKEN = "ghp_' + "a" * 36 + '"\nOK = 1\n')
    script = [msg(("create_file", {"target_file": "generated/x.txt", "content": "1"}), ("read_file", {"target_file": ".env"}),
                  ("read_file", {"target_file": "keys.py"})), msg(COMPLETE)]
    ctx, graph, cfg, out = env.start(script)
    assert not (env.ws / "generated").exists()
    result = str(env.llm.calls[1][-1].content) + "".join(str(m.content) for m in env.llm.calls[1])
    assert "IGNORED_PATH" in result and "SECRET_FILE" in result
    assert "supersecretvalue123" not in result and "ghp_" + "a" * 36 not in result and "[REDACTED]" in result
    assert "secret_redacted" in [e["type"] for e in env.sm.list_events(ctx.run_id)]
    first_prompt = str(env.llm.calls[0][0].content)
    assert "supersecretvalue123" not in first_prompt and ".env" not in first_prompt.split("WORKSPACE CONTEXT")[1].split("RECENT EXECUTION")[0]


# ─── concurrency lock + external modification ───────────────

def test_one_active_run_per_workspace_and_stale_lock_takeover(env):
    ctx, _ = start_run(env.sm, "proj", env.ws, "first")
    with pytest.raises(WorkspaceBusy):
        start_run(env.sm, "proj", env.ws, "second")
    env.sm.update_run(ctx.run_id, "completed")                       # finishing releases the lock
    ctx2, _ = start_run(env.sm, "proj", env.ws, "third")
    import sqlite3
    c = sqlite3.connect(env.sm.db_path)
    c.execute("UPDATE locks SET pid=999999")                         # holder "crashed"
    c.commit()
    c.close()
    ctx3, _ = start_run(env.sm, "proj", env.ws, "after crash")
    assert ctx3.run_id != ctx2.run_id


def test_external_modification_pauses_for_approval(env):
    (env.ws / "shared.txt").write_text("v1")
    sneaky = lambda: (  # noqa: E731  runs while the agent "thinks": the user edits the file
        (env.ws / "shared.txt").write_text("v2 by the user"),
        msg(("write_file", {"target_file": "shared.txt", "content": "agent version"}), COMPLETE))[1]
    script = [msg(("read_file", {"target_file": "shared.txt"})), sneaky]
    ctx, graph, cfg, out = env.start(script, level="auto_within_limits")
    p = interrupt_payload(out)
    assert "shared.txt was modified outside Nexura" in p["reason"] and (env.ws / "shared.txt").read_text() == "v2 by the user"
    graph.invoke(Command(resume={"decision": "approve"}), cfg)
    assert (env.ws / "shared.txt").read_text() == "agent version"


def test_editor_reported_edit_marks_conflict(env):
    from core import orchestrator
    ctx = ProjectContext(run_id="r", root=env.ws)
    (env.ws / "f.txt").write_text("x")
    ctx.external_edits.add(ctx.key(env.ws / "f.txt"))
    assert orchestrator._conflicts(ctx, [{"file": "f.txt", "change_type": "modified"}]) == ["f.txt"]
    assert orchestrator._conflicts(ctx, [{"file": "new.txt", "change_type": "created"}]) == []


# ─── diff preview ───────────────────────────────────────────

def test_preview_batches_and_does_not_touch_disk(env):
    from core import approvals
    ctx = ProjectContext(run_id="r", root=env.ws)
    batch = Action(action="batch_file_operations", metadata={"operations": [
        {"operation": "create", "target_file": "n.py", "content": "a\n"},
        {"operation": "patch", "target_file": "n.py", "search_text": "a", "replace_text": "b"},
        {"operation": "patch", "target_file": "README.md", "search_text": "hi", "replace_text": "yo"}]})
    pv = {p["file"]: p for p in approvals.preview(batch, ctx)}
    assert pv["n.py"]["change_type"] == "created" and pv["n.py"]["after"] == "b\n" and pv["README.md"]["after"] == "yo\n"
    assert "+yo" in pv["README.md"]["diff"] and not (env.ws / "n.py").exists() and (env.ws / "README.md").read_text() == "hi\n"
    assert hashlib.sha256(b"hi\n").hexdigest()
