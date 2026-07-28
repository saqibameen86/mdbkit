"""Shareable reports (Markdown / HTML).

A convenience layer over the existing analyses, for pasting into a ticket or
attaching to a post-incident review. The terminal stays primary.

HTML is fully self-contained: inline CSS, no JavaScript, no external assets
or CDN references — it opens on an air-gapped machine and sends nothing
anywhere. Reports carry query *shapes* and metrics only, exactly like the
terminal output: literal values never enter a shape.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import List, Optional

from . import __version__

CSS = """
:root { color-scheme: light dark; }
body { font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
       Helvetica, Arial, sans-serif; margin: 0 auto; max-width: 62rem;
       padding: 2rem 1.25rem 4rem; color: #1a1a1a; background: #fff; }
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin: 2rem 0 .6rem; padding-bottom: .3rem;
     border-bottom: 1px solid #e5e5e5; }
.meta { color: #666; font-size: .85rem; margin-bottom: 1.5rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem;
        font-size: .87rem; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #eee; }
th { background: #fafafa; font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
            monospace; font-size: .85em; }
pre { background: #f6f8fa; padding: .8rem 1rem; border-radius: 6px;
      overflow-x: auto; }
.sev { display: inline-block; min-width: 4.2rem; padding: .1rem .45rem;
       border-radius: 4px; font-size: .74rem; font-weight: 700;
       letter-spacing: .03em; text-align: center; }
.CRIT { background: #fde2e1; color: #8c1d18; }
.WARN { background: #fdf0d5; color: #7a4b00; }
.INFO { background: #e7eefc; color: #14417a; }
.OK   { background: #e3f4e6; color: #1c5b2a; }
.finding { margin: .55rem 0; }
.finding .body { margin-left: .4rem; }
.ev { color: #555; font-size: .85rem; margin: .15rem 0 .15rem 5rem; }
.next { color: #14417a; font-size: .85rem; margin: .15rem 0 .15rem 5rem; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #e5e5e5;
         color: #777; font-size: .8rem; }
@media (prefers-color-scheme: dark) {
  body { background: #16181d; color: #e6e6e6; }
  th { background: #21242b; } th, td { border-bottom-color: #2b2f38; }
  h2 { border-bottom-color: #2b2f38; }
  pre { background: #1d2027; }
  .meta, .ev, footer { color: #9aa0aa; }
}
"""


class Report:
    """Accumulates sections, then renders Markdown or self-contained HTML."""

    def __init__(self, title: str, subtitle: str = ""):
        self.title = title
        self.subtitle = subtitle
        self.sections: List[tuple] = []  # (kind, heading, payload)

    def text(self, heading: str, body: str) -> "Report":
        self.sections.append(("text", heading, body))
        return self

    def table(self, heading: str, headers: List[str],
              rows: List[List[str]]) -> "Report":
        self.sections.append(("table", heading, (headers, rows)))
        return self

    def findings(self, heading: str, findings) -> "Report":
        self.sections.append(("findings", heading, findings))
        return self

    def code(self, heading: str, body: str) -> "Report":
        self.sections.append(("code", heading, body))
        return self

    # ---------------------------------------------------------- markdown --
    def to_markdown(self) -> str:
        out = ["# %s" % self.title]
        if self.subtitle:
            out.append("")
            out.append("*%s*" % self.subtitle)
        for kind, heading, payload in self.sections:
            out.append("")
            out.append("## %s" % heading)
            out.append("")
            if kind == "text":
                out.append(payload)
            elif kind == "code":
                out.append("```")
                out.append(payload)
                out.append("```")
            elif kind == "table":
                headers, rows = payload
                out.append("| " + " | ".join(headers) + " |")
                out.append("|" + "|".join(["---"] * len(headers)) + "|")
                for r in rows:
                    out.append("| " + " | ".join(str(c) for c in r) + " |")
            elif kind == "findings":
                for f in payload:
                    out.append("- **[%s] %s** — %s" %
                               (f.severity, f.title, f.detail))
                    for e in f.evidence:
                        out.append("    - %s" % e)
                    if f.next_step:
                        out.append("    - *next:* `%s`" % f.next_step)
        out.append("")
        out.append("---")
        out.append("")
        out.append("Generated offline by mdbkit %s — no data left this machine. "
                   "Not affiliated with MongoDB, Inc." % __version__)
        return "\n".join(out)

    # -------------------------------------------------------------- html --
    def to_html(self) -> str:
        e = html.escape
        parts = ["<!DOCTYPE html>", '<html lang="en"><head>',
                 '<meta charset="utf-8">',
                 '<meta name="viewport" content="width=device-width,'
                 ' initial-scale=1">',
                 '<meta name="robots" content="noindex, nofollow">',
                 "<title>%s</title>" % e(self.title),
                 "<style>%s</style>" % CSS, "</head><body>",
                 "<h1>%s</h1>" % e(self.title)]
        if self.subtitle:
            parts.append('<div class="meta">%s</div>' % e(self.subtitle))
        for kind, heading, payload in self.sections:
            parts.append("<h2>%s</h2>" % e(heading))
            if kind == "text":
                parts.append("<p>%s</p>" % e(payload).replace("\n", "<br>"))
            elif kind == "code":
                parts.append("<pre>%s</pre>" % e(payload))
            elif kind == "table":
                headers, rows = payload
                parts.append("<table><thead><tr>%s</tr></thead><tbody>" %
                             "".join("<th>%s</th>" % e(h) for h in headers))
                for r in rows:
                    cells = []
                    for c in r:
                        c = str(c)
                        cls = ' class="num"' if c.replace(",", "").replace(
                            ".", "").replace("%", "").replace("/s", "").replace(
                            "-", "").isdigit() else ""
                        cells.append("<td%s>%s</td>" % (cls, e(c)))
                    parts.append("<tr>%s</tr>" % "".join(cells))
                parts.append("</tbody></table>")
            elif kind == "findings":
                for f in payload:
                    parts.append(
                        '<div class="finding"><span class="sev %s">%s</span>'
                        '<span class="body"><strong>%s</strong> — %s</span>'
                        "</div>" % (e(f.severity), e(f.severity),
                                    e(f.title), e(f.detail)))
                    for ev in f.evidence:
                        parts.append('<div class="ev">%s</div>' % e(ev))
                    if f.next_step:
                        parts.append('<div class="next">next: <code>%s</code>'
                                     "</div>" % e(f.next_step))
        parts.append("<footer>Generated offline by mdbkit %s — no data left "
                     "this machine. Not affiliated with MongoDB, Inc."
                     "</footer>" % e(__version__))
        parts.append("</body></html>")
        return "\n".join(parts)

    def write(self, path: str, fmt: Optional[str] = None) -> str:
        if fmt is None:
            fmt = "html" if path.lower().endswith((".html", ".htm")) else "md"
        body = self.to_html() if fmt == "html" else self.to_markdown()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path


def stamp(window: str = "") -> str:
    bits = ["generated %s" % datetime.now().strftime("%Y-%m-%d %H:%M")]
    if window:
        bits.insert(0, window)
    return "  ·  ".join(bits)
