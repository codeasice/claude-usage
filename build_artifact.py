"""Build the dashboard as a page ready to publish as a claude.ai Artifact.

Same template and data as build_dashboard.py; only the page wrapper changes:
the Artifact host supplies <!doctype>/<head>/<body>, and theme tokens move to
:root so the page background follows the viewer's light/dark setting.

    python build_artifact.py                          # out/usage.sqlite -> out/artifact.html
    python build_artifact.py --from-dashboard out/dashboard.html   # reuse an already-built dashboard's data
    python build_artifact.py --title "Team usage"

The output contains the same personal data as the dashboard. Artifacts are
private until you share them; anyone you share with sees everything in it.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HERE = Path(__file__).parent


def page_from_template(template: str, data_json: str, title: str) -> str:
    style = re.search(r"<style>(.*?)</style>", template, re.S).group(1)
    body = re.search(r"<body>(.*)</body>", template, re.S).group(1)

    # Theme tokens: scoped to .viz-root in the template; the Artifact contract wants them on :root.
    style = style.replace(".viz-root {\n  color-scheme: light;", ":root {\n  color-scheme: light;", 1)
    style = style.replace(':root:where(:not([data-theme="light"])) .viz-root', ':root:not([data-theme="light"])')
    style = style.replace(':root[data-theme="dark"] .viz-root', ':root[data-theme="dark"]')
    style = style.replace("body { margin: 0; }", "body { margin: 0; background: var(--page); color: var(--text-primary); }")
    style += """
.filters label { min-width: 0; max-width: 100%; }
.filters select { min-width: 0; max-width: min(360px, 70vw); text-overflow: ellipsis; }
@media (max-width: 600px) {
  .viz-root { padding: 16px 16px 48px; }
  .tile.hero { grid-column: 1 / -1; }
  .tile.hero .value { font-size: 36px; }
  .tile .value { font-size: 22px; }
  .card { padding: 12px 14px; }
}
"""
    body = body.replace("__DATA__", data_json).replace("<h1>Claude usage</h1>", f"<h1>{title}</h1>")
    return f"<title>{title}</title>\n<style>{style}</style>\n{body.strip()}\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=HERE / "out" / "usage.sqlite")
    ap.add_argument("--from-dashboard", type=Path, help="take the data embedded in an existing dashboard.html")
    ap.add_argument("--out", type=Path, default=HERE / "out" / "artifact.html")
    ap.add_argument("--title", default="Claude Usage")
    args = ap.parse_args()

    if args.from_dashboard:
        html = args.from_dashboard.read_text(encoding="utf8")
        data = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    else:
        from build_dashboard import build
        data = json.dumps(build(args.db), separators=(",", ":")).replace("</", "<\\/")

    template = (HERE / "dashboard_template.html").read_text(encoding="utf8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page_from_template(template, data, args.title), encoding="utf8")
    print(f"Wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
