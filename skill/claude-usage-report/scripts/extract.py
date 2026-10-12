"""Extract Claude Code and Claude Cowork usage metadata into SQLite + CSV.

Sources:
    Claude Code  ~/.claude (projects/**.jsonl, history.jsonl, stats-cache.json)
    Cowork       the desktop app's local-agent-mode-sessions folder (Windows:
                 %APPDATA%/Claude, macOS: ~/Library/Application Support/Claude).
                 Each Cowork session holds Claude Code-format transcripts plus an
                 Agent SDK audit.jsonl, of which only numeric/status fields are read.

Deliberately excludes the *content* of prompts, replies, thinking, and tool
inputs/outputs. What is kept: timestamps, sizes, token counts, costs, models,
tool names, and light tool metadata (Bash program name, file paths touched,
skill / subagent / MCP server names, WebFetch domain).

Usage:
    python extract.py                       # writes ./out/usage.sqlite + ./out/csv/*.csv
    python extract.py --claude-dir PATH --out DIR
    python extract.py --no-titles           # also drop the AI-generated session titles
    python extract.py --no-cowork           # Claude Code only
    python extract.py --cowork-dir PATH     # non-default Cowork location

Stdlib only. Full rebuild every run (no incremental state).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SCHEMA = {
    "sessions": """
        session_id TEXT PRIMARY KEY, project TEXT, first_ts TEXT, last_ts TEXT,
        active_ms INTEGER, wall_ms INTEGER, version TEXT, entrypoint TEXT,
        git_branch TEXT, title TEXT, source TEXT, session_type TEXT,
        scheduled_task_id TEXT, archived INTEGER, recorded_cost_usd REAL,
        human_prompts INTEGER, api_calls INTEGER,
        tool_calls INTEGER, subagent_count INTEGER, compactions INTEGER,
        est_cost_usd REAL, last_process_cost_usd REAL, api_ms INTEGER, tool_ms INTEGER,
        lines_added INTEGER, lines_removed INTEGER""",
    "agents": """
        agent_id TEXT, session_id TEXT, agent_type TEXT, workflow_id TEXT,
        first_ts TEXT, last_ts TEXT, api_calls INTEGER, tool_calls INTEGER, source TEXT,
        PRIMARY KEY (session_id, agent_id)""",
    "api_calls": """
        message_id TEXT, session_id TEXT, agent_id TEXT, is_sidechain INTEGER,
        project TEXT, cwd TEXT, git_branch TEXT, ts TEXT, ts_local TEXT, model TEXT,
        input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
        cache_write_tokens INTEGER, cache_write_5m INTEGER, cache_write_1h INTEGER,
        web_search INTEGER, web_fetch INTEGER, stop_reason TEXT, tool_uses INTEGER,
        has_thinking INTEGER, has_text INTEGER, speed TEXT, service_tier TEXT,
        effort TEXT, permission_mode TEXT, version TEXT, model_family TEXT,
        est_cost_usd REAL, source TEXT""",
    "tool_calls": """
        tool_use_id TEXT, message_id TEXT, session_id TEXT, agent_id TEXT,
        is_sidechain INTEGER, project TEXT, ts TEXT, ts_local TEXT, tool TEXT,
        category TEXT, mcp_server TEXT, detail TEXT, file_path TEXT, file_ext TEXT,
        background INTEGER, result_ts TEXT, latency_ms INTEGER, is_error INTEGER,
        denied TEXT, interrupted INTEGER, result_chars INTEGER, input_chars INTEGER,
        source TEXT""",
    "prompts": """
        session_id TEXT, project TEXT, ts TEXT, ts_local TEXT, kind TEXT,
        source TEXT, chars INTEGER, slash_command TEXT, permission_mode TEXT,
        app TEXT""",
    "turns": """
        session_id TEXT, project TEXT, ts TEXT, ts_local TEXT,
        duration_ms INTEGER, message_count INTEGER, source TEXT""",
    "compactions": """
        session_id TEXT, agent_id TEXT, project TEXT, ts TEXT, trigger TEXT,
        pre_tokens INTEGER, post_tokens INTEGER, duration_ms INTEGER, source TEXT""",
    "session_model_cost": """
        session_id TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
        thinking_tokens INTEGER, cache_read_tokens INTEGER,
        cache_write_tokens INTEGER, web_search INTEGER, cost_usd REAL""",
    "pr_links": "session_id TEXT, ts TEXT, repo TEXT, number INTEGER, url TEXT, PRIMARY KEY (session_id, url)",
    "scheduled_fires": "session_id TEXT, ts TEXT, task_id TEXT, cron TEXT",
    "history_prompts": """
        ts TEXT, ts_local TEXT, project TEXT, session_id TEXT, chars INTEGER,
        pasted_items INTEGER, slash_command TEXT""",
    "stats_daily": """
        date TEXT PRIMARY KEY, messages INTEGER, sessions INTEGER, tool_calls INTEGER""",
    "stats_daily_model_tokens": "date TEXT, model TEXT, tokens INTEGER",
    # Cowork audit.jsonl: one row per Agent SDK `result` (a finished turn). Cost is per turn, not cumulative.
    "cowork_turns": """
        session_id TEXT, ts TEXT, ts_local TEXT, subtype TEXT, is_error INTEGER,
        duration_ms INTEGER, duration_api_ms INTEGER, ttft_ms INTEGER, num_turns INTEGER,
        total_cost_usd REAL, stop_reason TEXT, terminal_reason TEXT,
        permission_denials INTEGER, origin TEXT""",
    "rate_limit_events": """
        session_id TEXT, ts TEXT, ts_local TEXT, status TEXT, rate_limit_type TEXT,
        resets_at TEXT, using_overage INTEGER, overage_status TEXT""",
}

# Tables whose rows carry the app they came from.
SOURCE_COL = {"sessions": "source", "agents": "source", "api_calls": "source", "tool_calls": "source",
              "prompts": "app", "turns": "source", "compactions": "source"}

SLASH_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
# Leading tag -> prompt kind for harness-injected "user" turns (content itself is never stored).
TAG_KINDS = [
    ("<command-name>", "slash_command"), ("<command-message>", "slash_command"),
    ("<bash-input>", "bash_mode"), ("<bash-stdout>", "bash_output"), ("<bash-stderr>", "bash_output"),
    ("<local-command-stdout>", "local_command_output"), ("<local-command-stderr>", "local_command_output"),
    ("[Request interrupted", "interrupt"),
]

# Anthropic first-party list prices, $ per million tokens: (input, output, cache_read).
# Cache writes: 1.25x input (5m TTL), 2x input (1h TTL). Fast mode: 2x everything.
# Bedrock/Vertex ids are priced at first-party rates (an approximation).
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25), "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20), "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50), "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50), "claude-opus-4-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5": (2.0, 10.0, 0.20), "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30), "claude-haiku-4-5": (1.0, 5.0, 0.10),
}


def model_family(model: str | None) -> str | None:
    """Normalize provider prefixes/date suffixes: 'us.anthropic.claude-opus-5-5' -> 'claude-opus-5-5'."""
    if not model:
        return None
    m = re.sub(r"^(?:[a-z]{2}\.)?anthropic\.", "", model)
    m = re.sub(r"(-\d{8})?(-v\d+(:\d+)?)?(@\d+)?$", "", m)
    return m


def est_cost(a: dict) -> float | None:
    p = PRICES.get(model_family(a.get("model")))
    if not p:
        return None
    inp, out, read = p
    w5 = a.get("cache_write_5m") or 0
    w1 = a.get("cache_write_1h") or 0
    unsplit = max((a.get("cache_write_tokens") or 0) - w5 - w1, 0)
    usd = ((a.get("input_tokens") or 0) * inp + (a.get("output_tokens") or 0) * out
           + (a.get("cache_read_tokens") or 0) * read
           + (w5 + unsplit) * inp * 1.25 + w1 * inp * 2.0) / 1e6
    return usd * (2 if a.get("speed") == "fast" else 1)


FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit", "MultiEdit"}


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def local(s):
    dt = parse_ts(s)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else None


def ms_between(a, b):
    da, db = parse_ts(a), parse_ts(b)
    return int((db - da).total_seconds() * 1000) if da and db else None


def text_len(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for b in content:
            if isinstance(b, dict):
                n += len(b.get("text") or "") if b.get("type") == "text" else 0
            elif isinstance(b, str):
                n += len(b)
        return n
    return 0


def result_len(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text") or "") for b in content if isinstance(b, dict))
    return 0


def bash_program(cmd: str) -> str:
    """First meaningful program in a shell command, skipping cd/env prefixes."""
    for seg in re.split(r"&&|\|\||;|\n", cmd or ""):
        toks = seg.strip().split()
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks.pop(0)
        if not toks or toks[0] in ("cd", "export", "set", "echo", "#"):
            continue
        prog = toks[0].strip("\"'()")
        prog = re.split(r"[\\/]", prog)[-1]
        if prog in ("uv", "npx", "python", "python3", "py", "glab", "git", "gh", "make", "npm", "aws", "docker") and len(toks) > 1:
            sub = toks[1] if not toks[1].startswith("-") else (toks[2] if len(toks) > 2 else "")
            return f"{prog} {sub}".strip() if prog in ("git", "glab", "gh", "aws", "docker", "npm", "uv", "make") else prog
        return prog
    return ""


def tool_meta(name: str, inp: dict):
    """Return (category, mcp_server, detail, file_path, background)."""
    inp = inp if isinstance(inp, dict) else {}
    if name.startswith("mcp__"):
        parts = name.split("__")
        return "mcp", parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "", None, 0
    bg = 1 if inp.get("run_in_background") else 0
    if name == "Bash":
        return "shell", None, bash_program(inp.get("command", "")), None, bg
    if name in FILE_TOOLS:
        return "file", None, None, inp.get("file_path") or inp.get("notebook_path"), 0
    if name in ("Grep", "Glob"):
        return "search", None, None, inp.get("path"), 0
    if name == "Skill":
        return "skill", None, inp.get("skill"), None, 0
    if name in ("Agent", "Task"):
        return "agent", None, inp.get("subagent_type") or "general-purpose", None, bg
    if name == "Workflow":
        return "agent", None, "workflow", None, 0
    if name in ("WebFetch", "WebSearch"):
        return "web", None, urlparse(inp.get("url", "")).netloc or None, None, 0
    return "harness", None, None, None, 0


def default_cowork_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Claude"
    else:
        base = Path.home() / ".config" / "Claude"
    return base / "local-agent-mode-sessions"


def long_path(p: Path) -> Path:
    """Cowork paths nest past Windows' 260-char MAX_PATH; the extended-length prefix lifts it."""
    if sys.platform != "win32":
        return p
    s = str(p.resolve())
    return Path(s if s.startswith("\\\\?\\") else "\\\\?\\" + s)


def ms_to_iso(ms):
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def project_dir_of(path: Path) -> Path:
    """The projects/<encoded-cwd> directory a transcript sits under."""
    for parent in path.parents:
        if parent.parent.name == "projects":
            return parent
    return path.parent


def file_ext(p):
    if not p:
        return None
    suf = Path(p).suffix.lower()
    return suf or Path(p).name


class Extractor:
    def __init__(self, claude_dir: Path, keep_titles: bool, cowork_dir: Path | None = None):
        self.root = claude_dir
        self.cowork_dir = cowork_dir
        self.keep_titles = keep_titles
        self.source = "claude_code"     # app of the files being read right now
        self.cowork_project = {}        # Cowork session id -> project label from its metadata
        self.rows = defaultdict(list)
        self.sessions = {}
        self.agents = {}

    # -- per-file ------------------------------------------------------------
    def session(self, sid, project):
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = dict(
                session_id=sid, project=project, first_ts=None, last_ts=None,
                active_ms=0, wall_ms=None, version=None, entrypoint=None,
                git_branch=None, title=None, source=self.source, session_type=None,
                scheduled_task_id=None, archived=None, recorded_cost_usd=None,
                human_prompts=0, api_calls=0,
                tool_calls=0, subagent_count=0, compactions=0, est_cost_usd=0.0, last_process_cost_usd=None,
                api_ms=None, tool_ms=None, lines_added=None, lines_removed=None)
        return s

    def process_file(self, path: Path, project_dir: Path):
        rel = path.relative_to(project_dir).parts
        is_sub = "subagents" in rel
        agent_id = path.stem.removeprefix("agent-") if is_sub else None
        workflow_id = next((p for p in rel if p.startswith("wf_")), None)
        agent_type = None
        meta = path.with_suffix(".meta.json")
        if is_sub and meta.exists():
            try:
                agent_type = json.loads(meta.read_text(encoding="utf8")).get("agentType")
            except (OSError, ValueError):
                pass

        msgs = {}           # message_id -> aggregated api call
        pending = {}        # tool_use_id -> tool row
        project = None
        sid = None
        cost_state = None
        first_user_seen = False

        with path.open(encoding="utf8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t = d.get("type")
                sid = d.get("sessionId") or d.get("session_id") or sid
                ts = d.get("timestamp")
                if project is None and d.get("cwd"):
                    project = d["cwd"]
                if not sid:
                    continue
                if self.source == "cowork":
                    # The transcript cwd is a throwaway VM path; use the folder the user picked.
                    project = self.cowork_project.get(sid, "Cowork: no folder")
                s = self.session(sid, project)
                if not is_sub:
                    if project and not s["project"]:
                        s["project"] = project
                    if ts:
                        s["first_ts"] = min(filter(None, [s["first_ts"], ts]))
                        s["last_ts"] = max(filter(None, [s["last_ts"], ts]))
                    s["version"] = d.get("version") or s["version"]
                    s["entrypoint"] = d.get("entrypoint") or s["entrypoint"]
                    s["git_branch"] = s["git_branch"] or d.get("gitBranch")

                if t == "assistant":
                    m = d.get("message") or {}
                    mid = m.get("id") or d.get("uuid")
                    if m.get("model") == "<synthetic>":
                        continue
                    a = msgs.get(mid)
                    if a is None:
                        a = msgs[mid] = dict(
                            message_id=mid, session_id=sid, agent_id=agent_id,
                            is_sidechain=int(bool(d.get("isSidechain"))),
                            project=project, cwd=d.get("cwd"), git_branch=d.get("gitBranch"),
                            ts=ts, ts_local=local(ts), model=m.get("model"),
                            tool_uses=0, has_thinking=0, has_text=0, stop_reason=None,
                            effort=d.get("effort"), permission_mode=d.get("permissionMode"),
                            version=d.get("version"))
                    u = m.get("usage") or {}
                    cc = u.get("cache_creation") or {}
                    stu = u.get("server_tool_use") or {}
                    a.update(
                        input_tokens=u.get("input_tokens") or 0,
                        output_tokens=max(a.get("output_tokens") or 0, u.get("output_tokens") or 0),
                        cache_read_tokens=u.get("cache_read_input_tokens") or 0,
                        cache_write_tokens=u.get("cache_creation_input_tokens") or 0,
                        cache_write_5m=cc.get("ephemeral_5m_input_tokens") or 0,
                        cache_write_1h=cc.get("ephemeral_1h_input_tokens") or 0,
                        web_search=stu.get("web_search_requests") or 0,
                        web_fetch=stu.get("web_fetch_requests") or 0,
                        speed=u.get("speed"), service_tier=u.get("service_tier"))
                    a["stop_reason"] = m.get("stop_reason") or a["stop_reason"]
                    for b in m.get("content") or []:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "thinking":
                            a["has_thinking"] = 1
                        elif bt == "text":
                            a["has_text"] = 1
                        elif bt == "tool_use":
                            a["tool_uses"] += 1
                            name = b.get("name", "")
                            cat, srv, detail, fpath, bg = tool_meta(name, b.get("input"))
                            pending[b.get("id")] = dict(
                                tool_use_id=b.get("id"), message_id=mid, session_id=sid,
                                agent_id=agent_id, is_sidechain=a["is_sidechain"],
                                project=project, ts=ts, ts_local=local(ts), tool=name,
                                category=cat, mcp_server=srv, detail=detail,
                                file_path=fpath, file_ext=file_ext(fpath), background=bg,
                                result_ts=None, latency_ms=None, is_error=None,
                                denied=None, interrupted=None, result_chars=None,
                                input_chars=len(json.dumps(b.get("input") or {})))

                elif t == "user":
                    m = d.get("message") or {}
                    content = m.get("content")
                    blocks = content if isinstance(content, list) else []
                    results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
                    tur = d.get("toolUseResult") if isinstance(d.get("toolUseResult"), dict) else {}
                    for r in results:
                        row = pending.get(r.get("tool_use_id"))
                        if row is None:
                            continue
                        row["result_ts"] = ts
                        row["latency_ms"] = ms_between(row["ts"], ts)
                        row["is_error"] = int(bool(r.get("is_error")))
                        row["denied"] = d.get("toolDenialKind")
                        row["interrupted"] = int(bool(tur.get("interrupted"))) if tur else None
                        row["result_chars"] = result_len(r.get("content"))
                    if results or d.get("isMeta") or d.get("isCompactSummary"):
                        continue
                    origin = (d.get("origin") or {}).get("kind") if isinstance(d.get("origin"), dict) else None
                    src = d.get("promptSource")
                    if is_sub and not first_user_seen:
                        kind = "subagent_task"
                    elif origin == "human" or src in ("typed", "queued", "suggestion_accepted"):
                        kind = "human"
                    elif origin:
                        kind = origin
                    elif is_sub:
                        kind = "subagent_followup"
                    else:
                        kind = src or "other"
                    first_user_seen = True
                    raw = content if isinstance(content, str) else " ".join(
                        b.get("text", "") for b in blocks if isinstance(b, dict))
                    slash = SLASH_RE.search(raw or "")
                    if kind == "other":
                        head = (raw or "").lstrip()
                        kind = next((k for tag, k in TAG_KINDS if head.startswith(tag)), "other")
                        # The Cowork desktop UI sends typed prompts with no origin/promptSource.
                        if kind == "other" and self.source == "cowork" and not is_sub:
                            kind = "human"
                    self.rows["prompts"].append(dict(
                        session_id=sid, project=project, ts=ts, ts_local=local(ts),
                        kind=kind, source=src, chars=text_len(content),
                        slash_command=slash.group(1) if slash else None,
                        permission_mode=d.get("permissionMode")))
                    if kind == "human" and not is_sub:
                        s["human_prompts"] += 1

                elif t == "system":
                    st = d.get("subtype")
                    if st == "turn_duration" and not is_sub:
                        self.rows["turns"].append(dict(
                            session_id=sid, project=project, ts=ts, ts_local=local(ts),
                            duration_ms=d.get("durationMs"), message_count=d.get("messageCount")))
                        s["active_ms"] += d.get("durationMs") or 0
                    elif st == "compact_boundary":
                        cm = d.get("compactMetadata") or {}
                        self.rows["compactions"].append(dict(
                            session_id=sid, agent_id=agent_id, project=project, ts=ts,
                            trigger=cm.get("trigger"), pre_tokens=cm.get("preTokens"),
                            post_tokens=cm.get("postTokens"), duration_ms=cm.get("durationMs")))
                        if not is_sub:
                            s["compactions"] += 1
                    elif st == "scheduled_task_fire":
                        self.rows["scheduled_fires"].append(dict(
                            session_id=sid, ts=ts, task_id=d.get("taskId"), cron=d.get("cron")))

                elif t == "cost-state" and not is_sub:
                    cost_state = d
                elif t == "ai-title" and not is_sub and self.keep_titles:
                    s["title"] = d.get("aiTitle")
                elif t == "pr-link":
                    self.rows["pr_links"].append(dict(
                        session_id=sid, ts=ts, repo=d.get("prRepository"),
                        number=d.get("prNumber"), url=d.get("prUrl")))

        if not sid:
            return
        s = self.sessions[sid]
        for a in msgs.values():
            a["model_family"] = model_family(a["model"])
            a["est_cost_usd"] = est_cost(a)
        self.rows["api_calls"].extend(msgs.values())
        self.rows["tool_calls"].extend(pending.values())
        s["api_calls"] += len(msgs)
        s["tool_calls"] += len(pending)
        if is_sub:
            s["subagent_count"] += 1
            tss = [m["ts"] for m in msgs.values() if m["ts"]]
            self.agents[(sid, agent_id)] = dict(
                agent_id=agent_id, session_id=sid, agent_type=agent_type,
                workflow_id=workflow_id, first_ts=min(tss, default=None),
                last_ts=max(tss, default=None), api_calls=len(msgs), tool_calls=len(pending))
        s["est_cost_usd"] += sum(a["est_cost_usd"] or 0 for a in msgs.values())
        if cost_state:
            # cost-state covers only the LAST process of a resumed session; prefer est_cost_usd.
            s.update(last_process_cost_usd=cost_state.get("totalCostUSD"),
                     api_ms=cost_state.get("totalAPIDuration"),
                     tool_ms=cost_state.get("totalToolDuration"),
                     lines_added=cost_state.get("totalLinesAdded"),
                     lines_removed=cost_state.get("totalLinesRemoved"))
            for model, mu in (cost_state.get("modelUsage") or {}).items():
                self.rows["session_model_cost"].append(dict(
                    session_id=sid, model=model, input_tokens=mu.get("inputTokens"),
                    output_tokens=mu.get("outputTokens"), thinking_tokens=mu.get("thinkingTokens"),
                    cache_read_tokens=mu.get("cacheReadInputTokens"),
                    cache_write_tokens=mu.get("cacheCreationInputTokens"),
                    web_search=mu.get("webSearchRequests"), cost_usd=mu.get("costUSD")))

    # -- whole tree ----------------------------------------------------------
    def process_files(self, files, label):
        # main transcripts first so subagent rows can find their session's project
        files = sorted(files, key=lambda p: ("subagents" in p.parts, str(p)))
        for i, p in enumerate(files, 1):
            self.process_file(p, project_dir_of(p))
            if i % 100 == 0:
                print(f"  {label}: {i}/{len(files)} transcripts", file=sys.stderr)
        for table, col in SOURCE_COL.items():
            for r in self.rows[table]:
                r.setdefault(col, self.source)
        for a in self.agents.values():
            a.setdefault("source", self.source)

    def run(self):
        proj_root = self.root / "projects"
        if proj_root.exists():
            self.process_files([p for p in proj_root.rglob("*.jsonl") if p.name != "journal.jsonl"], "Claude Code")
        if self.cowork_dir and self.cowork_dir.exists():
            self.source = "cowork"
            self.read_cowork(long_path(self.cowork_dir))
            self.source = "claude_code"
        for s in self.sessions.values():
            s["wall_ms"] = ms_between(s["first_ts"], s["last_ts"])
        first_pr = {}
        for r in sorted(self.rows["pr_links"], key=lambda r: r["ts"] or ""):
            first_pr.setdefault((r["session_id"], r["url"]), r)
        self.rows["pr_links"] = list(first_pr.values())
        self.rows["sessions"] = list(self.sessions.values())
        self.rows["agents"] = list(self.agents.values())
        self.read_history()
        self.read_stats_cache()

    # -- Cowork ----------------------------------------------------------------
    def read_cowork(self, root: Path):
        """<account>/<org>/local_<id>.json is session metadata; <account>/<org>/<session>/
        holds .claude/projects/**.jsonl transcripts and audit.jsonl."""
        for meta_path in root.glob("*/*/local_*.json"):
            try:
                m = json.loads(meta_path.read_text(encoding="utf8"))
            except (OSError, ValueError):
                continue
            # Allowlist: the same file holds the account email, the opening prompt and the
            # system prompt, none of which are read.
            sid = m.get("cliSessionId") or m.get("sessionId")
            if not sid:
                continue
            folders = m.get("userSelectedFolders") or []
            scheduled = m.get("sessionType") == "scheduled"
            project = folders[0] if folders else ("Cowork: scheduled" if scheduled else "Cowork: no folder")
            self.cowork_project[sid] = project
            s = self.session(sid, project)
            s.update(session_type=m.get("sessionType") or "interactive",
                     scheduled_task_id=m.get("scheduledTaskId"), archived=int(bool(m.get("isArchived"))),
                     first_ts=ms_to_iso(m.get("createdAt")), last_ts=ms_to_iso(m.get("lastActivityAt")),
                     entrypoint="local-agent")
            if self.keep_titles:
                s["title"] = m.get("title")
        self.process_files([p for p in root.glob("*/*/*/.claude/projects/**/*.jsonl")
                            if p.name != "journal.jsonl"], "Cowork")
        for audit in root.glob("*/*/*/audit.jsonl"):
            self.read_cowork_audit(audit)

    def read_cowork_audit(self, path: Path):
        turn_ms, cost = defaultdict(int), defaultdict(float)
        with path.open(encoding="utf8", errors="replace") as fh:
            for line in fh:
                # cheap prefilter: only `result` and rate-limit records are used
                if '"type":"result"' not in line and '"rate_limit_event"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t, sid = d.get("type"), d.get("session_id")
                ts = d.get("timestamp") or d.get("_audit_timestamp")
                if t == "result":
                    # `result` (the reply text) is never read.
                    origin = d.get("origin")
                    self.rows["cowork_turns"].append(dict(
                        session_id=sid, ts=ts, ts_local=local(ts), subtype=d.get("subtype"),
                        is_error=int(bool(d.get("is_error"))), duration_ms=d.get("duration_ms"),
                        duration_api_ms=d.get("duration_api_ms"), ttft_ms=d.get("ttft_ms"),
                        num_turns=d.get("num_turns"), total_cost_usd=d.get("total_cost_usd"),
                        stop_reason=d.get("stop_reason"), terminal_reason=d.get("terminal_reason"),
                        permission_denials=len(d.get("permission_denials") or []),
                        origin=origin.get("kind") if isinstance(origin, dict) else None))
                    turn_ms[sid] += d.get("duration_ms") or 0
                    cost[sid] += d.get("total_cost_usd") or 0
                elif t == "rate_limit_event":
                    info = d.get("rate_limit_info") or {}
                    resets = info.get("resetsAt")
                    if isinstance(resets, (int, float)):
                        resets = ms_to_iso(resets * 1000 if resets < 1e11 else resets)
                    self.rows["rate_limit_events"].append(dict(
                        session_id=sid, ts=ts, ts_local=local(ts), status=info.get("status"),
                        rate_limit_type=info.get("rateLimitType"), resets_at=resets,
                        using_overage=int(bool(info.get("isUsingOverage"))),
                        overage_status=info.get("overageStatus")))
        for sid in turn_ms:
            s = self.sessions.get(sid)
            if s is None:
                continue
            # Cowork transcripts may lack turn_duration records; the SDK's own duration is authoritative.
            s["active_ms"] = turn_ms[sid] or s["active_ms"]
            s["recorded_cost_usd"] = (s["recorded_cost_usd"] or 0) + cost[sid]

    def read_history(self):
        hp = self.root / "history.jsonl"
        if not hp.exists():
            return
        for line in hp.open(encoding="utf8", errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts_ms = d.get("timestamp")
            ts = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).isoformat() if ts_ms else None
            disp = d.get("display") or ""
            slash = re.match(r"^/(\S+)", disp)
            self.rows["history_prompts"].append(dict(
                ts=ts, ts_local=local(ts), project=d.get("project"),
                session_id=d.get("sessionId"), chars=len(disp),
                pasted_items=len(d.get("pastedContents") or {}),
                slash_command=slash.group(1) if slash else None))

    def read_stats_cache(self):
        sp = self.root / "stats-cache.json"
        if not sp.exists():
            return
        d = json.loads(sp.read_text(encoding="utf8"))
        for r in d.get("dailyActivity") or []:
            self.rows["stats_daily"].append(dict(
                date=r.get("date"), messages=r.get("messageCount"),
                sessions=r.get("sessionCount"), tool_calls=r.get("toolCallCount")))
        for r in d.get("dailyModelTokens") or []:
            for model, n in (r.get("tokensByModel") or {}).items():
                self.rows["stats_daily_model_tokens"].append(dict(date=r.get("date"), model=model, tokens=n))


def write(rows, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    (out / "csv").mkdir(exist_ok=True)
    db_path = out / "usage.sqlite"
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    for table, ddl in SCHEMA.items():
        db.execute(f"CREATE TABLE {table} ({ddl})")
        col_ddl = re.sub(r",\s*PRIMARY KEY\s*\([^)]*\)", "", ddl)
        cols = [c.strip().split()[0] for c in col_ddl.split(",") if c.strip()]
        data = rows.get(table, [])
        db.executemany(
            f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [[r.get(c) for c in cols] for r in data])
        with (out / "csv" / f"{table}.csv").open("w", newline="", encoding="utf8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerows([[r.get(c) for c in cols] for r in data])
        print(f"  {table:26s} {len(data):>8,} rows", file=sys.stderr)
    db.commit()
    db.close()
    return db_path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--claude-dir", type=Path, default=Path.home() / ".claude")
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "out")
    ap.add_argument("--no-titles", action="store_true", help="omit AI-generated session titles")
    ap.add_argument("--cowork-dir", type=Path, default=default_cowork_dir(),
                    help="Claude desktop local-agent-mode-sessions folder")
    ap.add_argument("--no-cowork", action="store_true", help="skip Claude Cowork sessions")
    args = ap.parse_args()
    cowork = None if args.no_cowork else args.cowork_dir
    ex = Extractor(args.claude_dir, keep_titles=not args.no_titles, cowork_dir=cowork)
    found = f" + {cowork}" if cowork and cowork.exists() else ""
    print(f"Reading {args.claude_dir}{found} ...", file=sys.stderr)
    ex.run()
    db = write(ex.rows, args.out)
    print(f"Wrote {db}", file=sys.stderr)


if __name__ == "__main__":
    main()
