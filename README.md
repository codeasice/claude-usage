# claude-usage

Extract usage metadata from your local Claude Code data (`~/.claude`) and view it as a self-contained HTML dashboard.

Records tokens, estimated cost, models, tools, latency, errors, subagents, compactions and activity timing. **Prompt and response text is never extracted.**

Python 3.10+ and the standard library are all you need: no dependencies, no network calls.

## Usage

```
python extract.py                    # ~/.claude -> out/usage.sqlite + out/csv/*.csv
python build_dashboard.py --open     # -> out/dashboard.html
```

`extract.py --no-titles` omits Claude Code's auto-generated session titles.

Everything is written to `out/`, which is gitignored. Treat it as personal data.

## What's in the dashboard

- Filters: date range, project, main thread vs subagents, light/dark
- Headline numbers: estimated cost, API calls, output tokens, cache hit rate, sessions, prompts, tool calls, tool error rate, compactions
- Daily cost by model; cost by token type; main thread vs subagent cost
- Weekday x hour heatmaps of your prompts and of Claude's API calls
- Top tools, MCP servers and shell programs, with error rate and latency
- Context size per call; weekly prompt history
- Sortable tables of sessions, most-touched files, skills and subagent types

Every chart has a table view.

## Notes

- **Cost is an API-equivalent estimate** (token counts x list price, from `PRICES` in `extract.py`), not a bill. The cost Claude Code stores itself only covers the last process of a resumed session, so it is not used.
- Claude Code prunes old transcripts locally, so rerun `extract.py` regularly if you want long-term history. Prompt history (`history.jsonl`) usually goes back further.
- Update `PRICES` in `extract.py` when model pricing changes.
