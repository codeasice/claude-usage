"""Generate a dashboard filled with made-up data, safe to commit or share publicly.

    python make_sample.py               # -> sample/dashboard_sample.html

Reads nothing from your machine: every project, title, file, tool and number is
generated from a fixed random seed, in the same shape build_dashboard.build() emits.
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

from build_dashboard import MODEL_LABELS
from extract import PRICES

HERE = Path(__file__).parent
SEED = 42
DAYS = 120
END = date(2026, 6, 30)

PROJECTS = [
    ("~/dev/acme-api", 0, 0.30), ("~/dev/storefront-web", 0, 0.22), ("~/dev/data-pipeline", 0, 0.14),
    ("~/dev/infra-terraform", 0, 0.08), ("~/dev/mobile-app", 0, 0.06),
    ("~/Documents/Quarterly planning", 1, 0.09), ("Cowork: no folder", 1, 0.06), ("Cowork: scheduled", 1, 0.05),
]
MODELS = [("claude-opus-5-5", 0.55), ("claude-sonnet-5", 0.30), ("claude-haiku-4-5", 0.10), ("claude-fable-5-1", 0.05)]
TITLES = [
    "Fix flaky checkout integration test", "Add pagination to orders endpoint", "Refactor auth middleware",
    "Investigate slow dashboard query", "Write migration for user preferences", "Bump dependencies and fix types",
    "Draft Q3 roadmap summary", "Summarize customer feedback themes", "Set up CI caching",
    "Port image resizer to a worker", "Explain the billing retry logic", "Add dark mode to settings page",
    "Clean up unused feature flags", "Weekly status digest", "Triage open bug reports",
    "Add rate limiting to public API", "Document deployment runbook", "Debug memory leak in sync job",
    "Morning inbox summary", "Prototype CSV export",
]
FILES = {
    "~/dev/acme-api": ["src/routes/orders.ts", "src/routes/users.ts", "src/middleware/auth.ts", "src/db/migrations/0042_prefs.sql", "test/orders.test.ts", "package.json"],
    "~/dev/storefront-web": ["src/pages/checkout.tsx", "src/components/Cart.tsx", "src/styles/theme.css", "e2e/checkout.spec.ts", "README.md"],
    "~/dev/data-pipeline": ["pipeline/sync_job.py", "pipeline/transforms.py", "tests/test_transforms.py", "pyproject.toml"],
    "~/dev/infra-terraform": ["modules/cache/main.tf", "envs/prod/variables.tf", ".github/workflows/ci.yml"],
    "~/dev/mobile-app": ["lib/screens/settings.dart", "lib/theme.dart", "pubspec.yaml"],
    "~/Documents/Quarterly planning": ["roadmap-draft.md", "feedback-themes.md", "notes.txt"],
}
# tool -> (weight, details to pick from, latency median ms, error rate)
TOOLS = {
    "Read": (0.26, None, 40, 0.01), "Edit": (0.14, None, 60, 0.04), "Write": (0.04, None, 50, 0.01),
    "Bash": (0.22, ["git status", "git diff", "npm test", "pytest", "ls", "rg", "npm run", "terraform", "docker compose"], 2500, 0.09),
    "Grep": (0.09, None, 120, 0.0), "Glob": (0.05, None, 80, 0.0), "TodoWrite": (0.04, None, 10, 0.0),
    "WebFetch": (0.02, ["docs.example.com", "developer.example.org"], 1800, 0.06),
    "Skill": (0.02, ["docx", "xlsx", "release-notes"], 400, 0.0),
    "Agent": (0.03, ["general-purpose", "Explore", "Plan"], 60000, 0.02),
    "mcp__issue-tracker__search_issues": (0.03, "issue-tracker", 900, 0.03),
    "mcp__issue-tracker__create_issue": (0.01, "issue-tracker", 1100, 0.05),
    "mcp__team-chat__read_channel": (0.02, "team-chat", 700, 0.02),
    "mcp__calendar__list_events": (0.02, "calendar", 600, 0.01),
    "mcp__docs-search__query": (0.01, "docs-search", 1500, 0.04),
}


class Index:
    def __init__(self):
        self.items, self.pos = [], {}

    def __call__(self, s):
        if s not in self.pos:
            self.pos[s] = len(self.items)
            self.items.append(s)
        return self.pos[s]


def pick(rng, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[-1] for p in pairs])[0]


def hour(rng, weekend):
    if rng.random() < (0.5 if weekend else 0.08):
        return rng.randrange(24)
    return min(23, max(0, int(rng.gauss(13, 3))))


def generate() -> dict:
    rng = random.Random(SEED)
    days = [(END - timedelta(DAYS - 1 - i)).isoformat() for i in range(DAYS)]
    proj, tool, detail, files, limits = Index(), Index(), Index(), Index(), Index()
    for p, _, _ in PROJECTS:
        proj(p)
    for t in TOOLS:
        tool(t)
    fams = [m for m, _ in MODELS]
    out = dict(calls=[], toolcalls=[], history=[], compactions=[], sessions=[], cwTurns=[], rateLimits=[])

    for di, d in enumerate(days):
        weekend = date.fromisoformat(d).weekday() >= 5
        ramp = 0.4 + 0.6 * di / DAYS                     # usage grows over the period
        n_sessions = max(0, int(rng.gauss(4.5 * ramp, 1.5) * (0.3 if weekend else 1)))
        for _ in range(n_sessions):
            pname = pick(rng, PROJECTS)
            app = next(a for p, a, _ in PROJECTS if p == pname)
            pi = proj(pname)
            scheduled = pname == "Cowork: scheduled"
            h0 = 7 if scheduled else hour(rng, weekend)
            n_calls = max(3, int(rng.lognormvariate(3.0, 0.8)))
            n_sub = rng.choice([0, 0, 0, 1, 2]) if app == 0 else 0
            sess_cost = sess_tools = 0
            ctx = rng.randint(15_000, 30_000)
            comps = 0
            for ci in range(n_calls):
                sc = 1 if n_sub and rng.random() < 0.25 else 0
                fam = pick(rng, MODELS) if not sc else rng.choice(["claude-sonnet-5", "claude-haiku-4-5"])
                inp_p, out_p, read_p = PRICES[fam]
                ctx = min(190_000, ctx + rng.randint(500, 6000))
                if ctx > 160_000 and rng.random() < 0.3:
                    out["compactions"].append([di, pi, sc, ctx, app]); ctx = 30_000; comps += 1
                cw = rng.randint(800, 9000)
                inp = rng.randint(5, 400)
                cr = max(0, ctx - cw - inp)
                o = int(rng.lognormvariate(6.2, 0.9))
                c = [inp * inp_p / 1e6, o * out_p / 1e6, cr * read_p / 1e6, cw * inp_p * 1.25 / 1e6]
                hr = min(23, h0 + ci // 25)
                out["calls"].append([di, pi, sc, fams.index(fam), hr, inp, o, cr, cw,
                                     *(round(x, 5) for x in c), inp + cr + cw, app])
                sess_cost += sum(c)
                for _ in range(rng.choice([0, 1, 1, 1, 2, 3])):
                    tname = pick(rng, [(k, v[0]) for k, v in TOOLS.items()])
                    _, det, lat, err = TOOLS[tname]
                    di_ = -1
                    if isinstance(det, list):
                        di_ = detail(rng.choice(det))
                    elif isinstance(det, str):
                        di_ = detail(det)
                    fi = -1
                    if tname in ("Read", "Edit", "Write") and pname in FILES:
                        fi = files(pname.replace("~", "/home/user") + "/" + rng.choice(FILES[pname]))
                    out["toolcalls"].append([di, pi, sc, tool(tname), hr, int(rng.random() < err),
                                             int(rng.lognormvariate(0, 0.7) * lat), di_, fi,
                                             int(rng.random() < 0.01), app])
                    sess_tools += 1
            prompts = 1 if scheduled else max(1, n_calls // rng.randint(6, 14))
            for k in range(prompts):
                out["history"].append([di, pi, min(23, h0 + k // 6), app])
            active = n_calls * rng.randint(9_000, 20_000)
            recorded = -1
            if app == 1:
                for k in range(prompts):
                    dur = int(rng.lognormvariate(11.3, 0.8))
                    cost = round(sess_cost / prompts * rng.uniform(0.9, 1.1), 5)
                    out["cwTurns"].append([di, pi, min(23, h0 + k // 6), dur, rng.randint(900, 4000), cost,
                                           int(rng.random() < 0.03), int(rng.random() < 0.08), int(scheduled)])
                    recorded = (recorded if recorded > 0 else 0) + cost
                recorded = round(recorded, 2)
            out["sessions"].append([
                "%08x-sample" % rng.getrandbits(32), di, pi,
                rng.choice(TITLES) if rng.random() < 0.85 else "", active, n_calls, sess_tools, n_sub,
                comps, int(app == 0 and rng.random() < 0.12), round(sess_cost, 2), prompts, app,
                int(scheduled), recorded])
        # a few rate-limit status events on heavy days
        if not weekend and rng.random() < 0.25:
            for _ in range(rng.randint(1, 4)):
                out["rateLimits"].append([di, proj("Cowork: no folder"), limits(rng.choice(["five_hour", "five_hour", "overage"])),
                                          limits(rng.choice(["allowed", "allowed_warning"]))])

    return dict(built="2026-06-30 09:00 (sample data)", days=days, projects=proj.items,
                models=[MODEL_LABELS.get(f, f) for f in fams], tools=tool.items, details=detail.items,
                files=files.items, limitLabels=limits.items, **out)


def main():
    data = json.dumps(generate(), separators=(",", ":")).replace("</", "<\\/")
    html = (HERE / "dashboard_template.html").read_text(encoding="utf8")
    html = html.replace("<title>Claude usage</title>", "<title>Claude usage (sample)</title>")
    html = html.replace("<h1>Claude usage</h1>",
                        "<h1>Claude usage <span style=\"color:var(--text-muted);font-weight:400\">· sample data</span></h1>\n"
                        "  <p class=\"sub\">Every project, title, file and number on this page is randomly generated. "
                        "Run extract.py and build_dashboard.py to see your own.</p>")
    html = html.replace("__DATA__", data)
    out = HERE / "sample" / "dashboard_sample.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf8")
    print(f"Wrote {out} ({out.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
