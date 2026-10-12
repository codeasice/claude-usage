---
name: claude-usage-report
description: Build a personal Claude Code and Claude Cowork (plus Cursor, if installed) usage dashboard (estimated cost, tokens, models, tools, MCP servers, sessions, activity heatmaps, Cowork turn stats, Cursor conversations and committed lines) from the user's own local data, as an HTML file and optionally a private claude.ai Artifact. Use when the user asks for a usage report, usage dashboard, "how much have I spent on Claude Code", token/cost breakdown across sessions, or wants to see which tools and projects they use most. Not for the current session only (that is explain-usage).
---

# Claude usage report

Bundled scripts (Python 3.10+, stdlib only, no network) read the user's **own** local Claude data and produce a self-contained dashboard. The skill ships with no data: every run reads whoever is running it.

- `scripts/extract.py` — reads `~/.claude`, the Cowork sessions folder and Cursor's local data (if present), writes `usage.sqlite` + CSVs. Prompt and response text is never read.
- `scripts/build_dashboard.py` — writes `dashboard.html` (opens in any browser, works offline).
- `scripts/build_artifact.py` — writes `artifact.html`, the same dashboard shaped for publishing as a claude.ai Artifact.

Data sources: Claude Code in `~/.claude`; Cowork in `%APPDATA%\Claude\local-agent-mode-sessions` (Windows) or `~/Library/Application Support/Claude/local-agent-mode-sessions` (macOS); Cursor in `%APPDATA%\Cursor` / `~/Library/Application Support/Cursor` and `~/.cursor/ai-tracking`. Cursor keeps no cost locally, so it is excluded from cost charts unless a usage CSV is passed with `--cursor-usage-csv`. With no Cursor data, Cursor is left out of the dashboard entirely.

## Privacy rules (always)

- Output is personal data: session titles, project and file paths, tool names. Write it only to a folder the user chose, never into a git repo without a `.gitignore` for it.
- Before publishing an Artifact, tell the user what it contains and that it is private until they share it from the page's Share menu; anyone they share it with sees everything in it. Offer `--no-titles` (drops auto-generated session titles).
- Never send the data anywhere else, and never include it in a reply beyond headline numbers.

## Step 1: work out where the scripts can run

The scripts must run where the data lives: on the user's own computer.

**Claude Code (local terminal):** run them directly. Pick an output folder with the user (default `~/claude-usage/out`).

```
python "${CLAUDE_SKILL_DIR}/scripts/extract.py" --out OUT
python "${CLAUDE_SKILL_DIR}/scripts/build_dashboard.py" --db OUT/usage.sqlite --out OUT/dashboard.html
python "${CLAUDE_SKILL_DIR}/scripts/build_artifact.py" --db OUT/usage.sqlite --out OUT/artifact.html
```

Add `--no-titles` to `extract.py` if the user wants titles left out; `--no-cowork` to skip Cowork, `--no-cursor` to skip Cursor.

**Cowork (sandboxed shell):** the sandbox cannot see `~/.claude` or the Claude app's data folder, so do not try to request those as connected folders. Instead:

1. Ask the user for (or use) a connected working folder, e.g. `claude-usage`.
2. Copy the four files from `${CLAUDE_SKILL_DIR}/scripts/` into it.
3. Ask the user to run, in a terminal on their computer, from that folder:
   ```
   python extract.py
   python build_dashboard.py
   ```
   (This writes `out/usage.sqlite` and `out/dashboard.html` next to the scripts. On Windows use `py` if `python` is not found.)
4. When they confirm, run in the sandbox (connected-folder path):
   ```
   python build_artifact.py --from-dashboard out/dashboard.html
   ```
   `--from-dashboard` reuses the data the user's machine built, so dates stay in their local time zone and paths stay shortened to `~`.

## Step 2: deliver

- Always: present `dashboard.html` (file card / open in browser).
- If the Artifact tool is available and the user wants a link they can reopen or share: load the `artifact-design` skill's page contract, then publish `artifact.html` with icon `chart`. It is about 1 MB per ~10k API calls and must stay under 16 MB; if it is larger, suggest `extract.py --no-cowork` or a fresh run after old transcripts are pruned.
- To refresh later: rerun the scripts and publish the same file path again (or pass the artifact's URL) so the link stays the same.

Close with 2–3 headline numbers from the dashboard (estimated cost for the last 30 days, top model, top tool), and remind the user that cost is an API-equivalent estimate at list prices, not a bill.

## Maintenance

Model prices live in `PRICES` in `extract.py`, display names in `MODEL_LABELS` in `build_dashboard.py`. Unknown models show as $0 cost; if a user's dashboard shows a model with zero cost, tell them its price is missing.
