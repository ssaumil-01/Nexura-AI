# Nexura AI (VS Code extension)

Nexura plans, builds and verifies code from a plain-language request, inside your open folder, with safeguards:
every change is checkpointed in git (so it can be undone), risky changes wait for your approval, and a task only counts
as done when its acceptance checks actually pass.

## Requirements

- **VS Code 1.85+**, a **trusted workspace** (Nexura edits files and runs commands, so it refuses untrusted folders).
- **git** on your PATH. If the folder is not a repository, Nexura offers to run `git init` and make an initial commit.
- **Python 3.11+**. By default the extension creates its own private virtual environment on first use and installs the
  backend's dependencies there (needs internet once). Or point `nexura.pythonPath` at an interpreter that already has
  `requirements.txt` installed, or select a 3.11+ interpreter with the Python extension.
- A **Gemini API key** (and optionally a Groq key).

## First run

1. Install the `.vsix` (`Extensions: Install from VSIX...`) and open a folder.
2. Run **Nexura: Set Gemini API Key** (stored in VS Code's SecretStorage, never in settings or files).
3. Open the **Nexura** view in the activity bar, describe what you want, and press **Start run**.
4. Review and **approve the plan** (or give feedback to re-plan).
5. Watch tasks run. Approve or reject risky changes; **Open the diff** to see exactly what would change.
6. **Undo last task** / **Undo run** restore the exact files Nexura changed. If you edited one of them since, Nexura asks
   before overwriting.

## Commands

| Command | What it does |
|---|---|
| Nexura: Start Run | Ask for a request and start a run |
| Nexura: Stop | Cancel the active run and stop its processes |
| Nexura: Resume | Continue a stopped/failed run from the first unfinished task |
| Nexura: Undo Last Task / Undo Run | Restore files from the git checkpoints |
| Nexura: Open Plan | Open the live `plan.md` (checkboxes update as tasks finish) |
| Nexura: Set Gemini API Key / Set Groq API Key | Store a key in SecretStorage |
| Nexura: Show Logs | Open the "Nexura" output channel |
| Nexura: Restart Backend | Restart the local Python server |

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `nexura.approvalLevel` | `ask_risky` | `ask_every_edit`, `ask_risky`, or `auto_within_limits`. Protected paths and package installs always need approval. |
| `nexura.model.coder` / `nexura.model.planner` | `gemini-3.1-flash-lite-preview` | Model names |
| `nexura.pythonPath` | empty | Python 3.11+ to run the backend |
| `nexura.protectedPaths` | `[]` | Extra patterns that always need approval, e.g. `migrations/`, `*.sql` |

`.env*` files and `.git/` can never be modified by Nexura, and `.env*` files and anything in `.gitignore` /
`.nexuraignore` are never sent to the model.

## What Nexura will not do

It will not touch files that have unsaved changes in your editor, will not run shell pipelines or arbitrary executables
(only `python`, `pip`, `node`, `npm`, `npx`, `uvicorn`, `pytest`, `streamlit`, `flask` with no shell operators), and will not
let the model decide that a task is finished: the planner's acceptance checks do.

## Troubleshooting

- **"Python 3.11+ was not found"**: install Python, or set `nexura.pythonPath`.
- **Backend keeps restarting / error banner**: open **Nexura: Show Logs**; the first run's `pip install` errors appear there.
  Delete the folder `nexura-ai` under VS Code's global storage to rebuild the private environment.
- **"not a git repository"**: accept the prompt to initialize git, or run `git init` yourself.
- **"uncommitted changes"**: accept "Checkpoint and continue" (your changes are saved to a hidden git ref first).
- **Another run is active on this workspace**: only one run per folder at a time; stop or finish it first.
- **No model answers**: check the API key and the model names in settings; rate-limit errors are retried with backoff.

## Development

```
cd extension
npm run setup        # installs extension and ../ui dependencies
npm run build        # copies the Python backend into ./backend and bundles extension + sidebar
npm run package      # produces nexura-ai-0.1.0.vsix
npm test             # launches VS Code and runs the end-to-end smoke test with a scripted (fake) model
```

The smoke test sets `NEXURA_FAKE_LLM=1`, a **test-only** switch that replaces the models with scripted ones. It needs the
repository's `.venv` (or `NEXURA_TEST_PYTHON`). Not published to the Marketplace.
