#!/usr/bin/env python3
"""Render docs/org-deployment-guide.md -> docs/SC-Nav-Org-Deployment-Guide.pdf.

The PDF is a printable copy of the markdown, never a source of truth; re-run this
whenever the markdown changes. Needs the `markdown` package (`pip install markdown`
— not a server dependency, so it's deliberately absent from requirements.txt) and
a Chrome/Chromium binary (set CHROME to override the macOS default).

    python3 tools/render_deploy_guide.py
"""
import os, pathlib, subprocess, sys, tempfile

import markdown  # pip install markdown

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "org-deployment-guide.md"
OUT = ROOT / "docs" / "SC-Nav-Org-Deployment-Guide.pdf"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

CSS = """
@page { size: letter; margin: 0.75in 0.8in; }
html { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       font-size: 10.5pt; line-height: 1.5; color: #1a1a1a; }
body { margin: 0; }
.cover { min-height: 8.6in; display: flex; flex-direction: column; justify-content: center;
         page-break-after: always; }
.cover h1 { font-size: 26pt; margin: 0 0 0.5em; }
.cover p { max-width: 4.6in; }
.cover .status { margin-top: auto; padding-top: 0.6em; border-top: 1px solid #ccc;
                 font-size: 8.5pt; color: #555; }
h2 { font-size: 15pt; margin: 1.6em 0 0.5em; padding-top: 0.9em; border-top: 1px solid #ddd;
     page-break-after: avoid; }
h3 { font-size: 12pt; margin: 1.3em 0 0.4em; page-break-after: avoid; }
p, ul, ol { margin: 0.55em 0; }
li { margin: 0.2em 0; }
li p { margin: 0.2em 0; }
code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 9.2pt;
       background: #f3f3f3; padding: 0.05em 0.3em; border-radius: 3px; }
pre { background: #f3f3f3; padding: 0.7em 0.9em; border-radius: 4px; overflow-x: auto;
      white-space: pre-wrap; word-break: break-all; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 0.8em 0; font-size: 9.5pt;
        page-break-inside: avoid; }
th, td { border: 1px solid #ccc; padding: 0.45em 0.6em; vertical-align: top; text-align: left; }
th { background: #f0f0f0; font-weight: 600; }
blockquote { margin: 0.9em 0; padding: 0.6em 0.9em; border-left: 4px solid #999;
             background: #f7f7f7; page-break-inside: avoid; }
blockquote p:first-child { margin-top: 0; }
blockquote p:last-child { margin-bottom: 0; }
hr { display: none; }
a { color: #0b57d0; text-decoration: none; }
"""


def main() -> int:
    text = SRC.read_text()
    html = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    # Cover page: the H1 + intro paragraph(s) up to the first <hr>, with the
    # status line moved to the foot of the cover.
    head, _, rest = html.partition("<hr />")
    status_start = head.find("<p><strong>Status:</strong>")
    status_end = head.find("</p>", status_start) + 4
    status = head[status_start:status_end]
    head = head[:status_start] + head[status_end:]
    cover = f'<div class="cover">{head}<div class="status">{status}</div></div>'
    page = f"<!doctype html><meta charset='utf-8'><title>Deploying SC Nav for your org</title><style>{CSS}</style>{cover}{rest}"
    with tempfile.TemporaryDirectory() as td:
        src = pathlib.Path(td) / "guide.html"
        src.write_text(page)
        cmd = [CHROME, "--headless", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
               f"--print-to-pdf={OUT}", f"file://{src}"]
        subprocess.run(cmd, check=True, capture_output=True)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
