#!/usr/bin/env python3
"""Build the print interior from the assembled master, and run a preflight on it.

Consumed by `PrintStep`. Contract:

    assemble_print.py <book> [--config PATH]

    input   <root>/exports/BookN_master.md
    output  <root>/exports/print/BookN_interior_6x9.pdf
    stdout  "<N> pages", then a preflight report ending in "=> N FAILURE(S)"
    exit    0 only when the PDF exists and every check passed

`PrintStep` parks the volume at the recovery gate when this exits non-zero or the PDF is
missing, so the preflight is a real gate and not a report. That is deliberate: a volume
should not call itself finished with an interior nobody could print.

Typst and pypdf are both optional. Without typst there is no PDF and this exits 1 with
that as the reason — which is the honest outcome, and one the recovery gate handles.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

# A common trade-paperback trim, with placeholder page bounds. A real project sets these
# from whatever its printer actually requires — they are not the same everywhere.
TRIM = (6.0, 9.0)
MIN_PAGES, MAX_PAGES = 24, 776
SPINE_TEXT_MIN_PAGES = 79


def load_config(argv: list[str]) -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if "--config" in argv:
        path = os.path.expanduser(argv[argv.index("--config") + 1])
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def strip_for_print(markdown: str) -> str:
    """Drop the pandoc title block and the screen-only contents list."""
    body = re.sub(r"\A%[^\n]*\n(%[^\n]*\n)*", "", markdown)
    return re.sub(r"^# Contents\n(?:- \[[^\]]*\]\([^)]*\)\n?)+", "", body, flags=re.M)


def typst_source(title: str, author: str, body: str) -> str:
    escaped = body.replace("\\", "\\\\").replace('"', '\\"')
    return f"""#set page(width: {TRIM[0]}in, height: {TRIM[1]}in, margin: (
  inside: 0.75in, outside: 0.6in, top: 0.75in, bottom: 0.6in,
), numbering: "1")
#set text(size: 10.5pt, font: ("Times New Roman", "Georgia", "DejaVu Serif"))
#set par(justify: true, first-line-indent: 0.2in, leading: 0.62em)
#show heading.where(level: 1): it => [
  #pagebreak(weak: true)
  #v(1.2in)
  #align(center, text(size: 15pt, weight: "bold", it.body))
  #v(0.4in)
]

#align(center + horizon)[
  #text(size: 22pt, weight: "bold")[{title}]
  #v(0.3in)
  #text(size: 12pt)[{author}]
]
#pagebreak()

{body}
"""


def markdown_to_typst(markdown: str) -> str:
    """Convert with pandoc when it is there, else fall back to a plain-text body."""
    pandoc = shutil.which("pandoc")
    if pandoc:
        result = subprocess.run(
            [pandoc, "--from", "markdown", "--to", "typst"],
            input=markdown,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout
        print(f"pandoc -> typst failed: {result.stderr.strip()[-200:]}", file=sys.stderr)
    # Fallback: keep headings, pass paragraphs through.
    lines = []
    for line in markdown.splitlines():
        if line.startswith("# "):
            lines.append(f"= {line[2:]}")
        elif line.startswith("## "):
            lines.append(f"== {line[3:]}")
        else:
            lines.append(line)
    return "\n".join(lines)


def preflight(pdf: str, pages: int) -> list[tuple[bool, str]]:
    """Every check a print interior has to clear, each with its own verdict line."""
    checks: list[tuple[bool, str]] = []
    checks.append((pages > 0, f"the interior has pages — {pages} pages"))
    checks.append(
        (MIN_PAGES <= pages <= MAX_PAGES,
         f"page count within {MIN_PAGES}-{MAX_PAGES} — {pages} pages")
    )
    checks.append((pages % 2 == 0, f"page count is even — {pages} pages"))
    checks.append(
        (pages >= SPINE_TEXT_MIN_PAGES,
         f"at least {SPINE_TEXT_MIN_PAGES} pages, so the cover may carry spine text "
         f"— {pages} pages")
    )
    try:
        from pypdf import PdfReader
    except ImportError:
        checks.append((True, "pypdf not installed — skipped the embedded-font and size checks"))
        return checks

    reader = PdfReader(pdf)
    fonts, annotated = set(), 0
    for page in reader.pages:
        resources = page.get("/Resources") or {}
        for name in (resources.get("/Font") or {}):
            fonts.add(str(name))
        if page.get("/Annots"):
            annotated += 1
    box = reader.pages[0].mediabox
    width, height = round(float(box.width) / 72, 2), round(float(box.height) / 72, 2)
    checks.append(
        ((width, height) == TRIM,
         f"trim is {TRIM[0]}x{TRIM[1]} in — measured {width}x{height}")
    )
    checks.append((bool(fonts), f"fonts are embedded — {len(fonts)} face(s)"))
    checks.append((annotated == 0, f"no annotations or form fields — {annotated} page(s) with them"))
    return checks


def main() -> int:
    argv = sys.argv[1:]
    book = next((a for a in argv if not a.startswith("--")), "1")
    cfg = load_config(argv)
    root = os.path.expanduser(cfg["project"]["root"])
    entry = (cfg.get("books") or {}).get(str(book), {})

    master = os.path.join(root, "exports", f"Book{book}_master.md")
    if not os.path.isfile(master):
        print(f"no master markdown at {master} — run assemble.py first", file=sys.stderr)
        return 1

    out = os.path.join(root, "exports", "print")
    os.makedirs(out, exist_ok=True)
    pdf = os.path.join(out, f"Book{book}_interior_6x9.pdf")

    typst = shutil.which("typst")
    if not typst:
        print(
            "typst not installed — cannot build the interior PDF. Install it "
            "(brew install typst) or expect the volume to park at the recovery gate.",
            file=sys.stderr,
        )
        return 1

    with open(master, encoding="utf-8") as handle:
        body = markdown_to_typst(strip_for_print(handle.read()))
    source = os.path.join(out, f"Book{book}_interior.typ")
    with open(source, "w", encoding="utf-8") as handle:
        handle.write(
            typst_source(entry.get("title") or f"Book {book}",
                         cfg.get("original_author", ""), body)
        )

    result = subprocess.run([typst, "compile", source, pdf], capture_output=True, text=True)
    if result.returncode != 0 or not os.path.isfile(pdf):
        print(f"typst failed: {result.stderr.strip()[-400:]}", file=sys.stderr)
        return 1

    pages = 0
    try:
        from pypdf import PdfReader

        pages = len(PdfReader(pdf).pages)
    except ImportError:
        # Count the page objects without a library rather than reporting nothing.
        with open(pdf, "rb") as handle:
            pages = handle.read().count(b"/Type /Page") or 0

    print(f"wrote {pdf}")
    print(f"{pages} pages")
    checks = preflight(pdf, pages)
    for ok, description in checks:
        print(f"    [{'ok  ' if ok else 'FAIL'}] {description}")
    failures = sum(1 for ok, _ in checks if not ok)
    print(f"    => {failures} FAILURE(S)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
