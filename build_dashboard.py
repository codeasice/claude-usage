"""Build a self-contained HTML dashboard from out/usage.sqlite (run extract.py first).

    python build_dashboard.py            # writes out/dashboard.html
    python build_dashboard.py --open     # ...and opens it in the default browser

No external JS/CSS: data is embedded as JSON and charts are hand-built SVG, so the
file works offline and behind corporate proxies.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

from extract import PRICES

HERE = Path(__file__).parent
MODEL_LABELS = {
    "claude-fable-5-1": "Fable 5.1", "claude-fable-5": "Fable 5", "claude-opus-5-5": "Opus 5.5",
    "claude-opus-5": "Opus 5", "claude-opus-4-8": "Opus 4.8", "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-6": "Opus 4.6", "claude-opus-4-5": "Opus 4.5", "claude-sonnet-5": "Sonnet 5",
    "claude-sonnet-4-6": "Sonnet 4.6", "claude-sonnet-4-5": "Sonnet 4.5", "claude-haiku-4-5": "Haiku 4.5",
}


def short_project(p: str | None) -> str:
    if not p:
        return "(unknown)"
    p = p.replace("\\", "/")
    home = str(Path.home()).replace("\\", "/")
    return "~" + p[len(home):] if p.lower().startswith(home.lower()) else p


class Index:
    """Dictionary-encode strings so the embedded JSON stays small."""

    def __init__(self):
        self.items: list[str] = []
        self.pos: dict[str, int] = {}

    def __call__(self, s):
        if s is None:
            return -1
        if s not in self.pos:
            self.pos[s] = len(self.items)
            self.items.append(s)
        return self.pos[s]


def build(db_path: Path) -> dict:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    q = lambda sql, *a: db.execute(sql, a).fetchall()

    sess_project = {r["session_id"]: short_project(r["project"]) for r in q("select session_id, project from sessions")}
    first = [r[0] for r in q("""select min(d) from (select min(substr(ts_local,1,10)) d from api_calls
                                  union all select min(substr(ts_local,1,10)) from history_prompts)""")]
    start = date.fromisoformat(first[0]) if first[0] else date.today()
    days = [(start + timedelta(n)).isoformat() for n in range((date.today() - start).days + 1)]
    day_ix = {d: i for i, d in enumerate(days)}
    proj, tool, detail, files = Index(), Index(), Index(), Index()

    # Fixed model order by all-time cost so a model keeps its color under every filter.
    fam_rows = q("select model_family, sum(est_cost_usd) c from api_calls group by 1 order by c desc")
    fams = [r["model_family"] for r in fam_rows]
    if len(fams) > 8:
        fams = fams[:7] + ["other"]
    fam_ix = {f: i for i, f in enumerate(fams)}
    models = [MODEL_LABELS.get(f, f) if f != "other" else "Other" for f in fams]

    def proj_of(r):
        return proj(sess_project.get(r["session_id"]) or short_project(r["project"]))

    calls = []
    for r in q("select * from api_calls where ts_local is not null"):
        p = PRICES.get(r["model_family"], (0, 0, 0))
        inp, out, read = p
        mult = 2 if r["speed"] == "fast" else 1
        w5, w1 = r["cache_write_5m"] or 0, r["cache_write_1h"] or 0
        unsplit = max((r["cache_write_tokens"] or 0) - w5 - w1, 0)
        c_in = (r["input_tokens"] or 0) * inp * mult / 1e6
        c_out = (r["output_tokens"] or 0) * out * mult / 1e6
        c_cr = (r["cache_read_tokens"] or 0) * read * mult / 1e6
        c_cw = ((w5 + unsplit) * inp * 1.25 + w1 * inp * 2) * mult / 1e6
        ctx = (r["input_tokens"] or 0) + (r["cache_read_tokens"] or 0) + (r["cache_write_tokens"] or 0)
        calls.append([
            day_ix.get(r["ts_local"][:10], 0), proj_of(r), r["is_sidechain"],
            fam_ix.get(r["model_family"], len(fams) - 1), int(r["ts_local"][11:13]),
            r["input_tokens"] or 0, r["output_tokens"] or 0, r["cache_read_tokens"] or 0,
            r["cache_write_tokens"] or 0, round(c_in, 5), round(c_out, 5), round(c_cr, 5),
            round(c_cw, 5), ctx])

    toolcalls = []
    for r in q("select * from tool_calls where ts_local is not null"):
        d = r["detail"]
        if r["category"] == "mcp":
            d = r["mcp_server"]
        toolcalls.append([
            day_ix.get(r["ts_local"][:10], 0), proj_of(r), r["is_sidechain"], tool(r["tool"]),
            int(r["ts_local"][11:13]), r["is_error"] or 0,
            r["latency_ms"] if r["latency_ms"] is not None else -1,
            detail(d) if d else -1,
            files(r["file_path"].replace("\\", "/")) if r["file_path"] and r["category"] == "file" else -1,
            1 if r["denied"] else 0])

    history = [[day_ix[r["ts_local"][:10]], proj(short_project(r["project"])), int(r["ts_local"][11:13])]
               for r in q("select ts_local, project from history_prompts where ts_local is not null")
               if r["ts_local"][:10] in day_ix]

    compactions = [[day_ix.get(local_day(r["ts"]), 0), proj_of(r), 1 if r["agent_id"] else 0, r["pre_tokens"] or 0]
                   for r in q("select * from compactions where ts is not null")]

    prs = {r["session_id"]: r["n"] for r in q("select session_id, count(*) n from pr_links group by 1")}
    sessions = []
    for r in q("select * from sessions where first_ts is not null"):
        sessions.append([
            r["session_id"], day_ix.get(local_day(r["first_ts"]), 0), proj(short_project(r["project"])),
            r["title"] or "", r["active_ms"] or 0, r["api_calls"], r["tool_calls"], r["subagent_count"],
            r["compactions"], prs.get(r["session_id"], 0), round(r["est_cost_usd"] or 0, 2), r["human_prompts"]])

    return dict(built=datetime.now().strftime("%Y-%m-%d %H:%M"), days=days, projects=proj.items,
                models=models, tools=tool.items, details=detail.items, files=files.items,
                calls=calls, toolcalls=toolcalls, history=history, compactions=compactions,
                sessions=sessions)


def local_day(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=HERE / "out" / "usage.sqlite")
    ap.add_argument("--out", type=Path, default=HERE / "out" / "dashboard.html")
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()
    data = json.dumps(build(args.db), separators=(",", ":")).replace("</", "<\\/")
    html = (HERE / "dashboard_template.html").read_text(encoding="utf8").replace("__DATA__", data)
    args.out.write_text(html, encoding="utf8")
    print(f"Wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")
    if args.open:
        webbrowser.open(args.out.resolve().as_uri())


if __name__ == "__main__":
    main()
