#!/usr/bin/env python3
"""Assemble the harvested chapters into a manuscript: master markdown, .docx, .epub.

Consumed by `AssembleStep`. Contract:

    assemble.py <book> [--config PATH]

    input   <vault_book_dir>/Book N — 回LO-HI (translation)/回NN (edited).md
            plus _Front Matter.md and _Back Matter.md from the same folder
    output  <root>/exports/BookN_master.md   (always)
            <root>/exports/BookN.docx        (if pandoc is installed)
            <root>/exports/BookN.epub        (if pandoc is installed)

Pandoc is optional on purpose: a fresh clone should get a manuscript and a green test
run without installing anything, and `AssembleStep` only requires the master markdown.
The docx and epub are built when the tool is there and skipped with a line on stdout
when it is not.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys


def load_config(argv: list[str]) -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if "--config" in argv:
        path = os.path.expanduser(argv[argv.index("--config") + 1])
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def book_folder(cfg: dict, book: str) -> str | None:
    base = os.path.expanduser(cfg.get("project", {}).get("vault_book_dir", ""))
    entry = (cfg.get("books") or {}).get(str(book))
    if not base or not entry:
        return None
    low, high = entry.get("hui", [0, 0])
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        directory = os.path.join(base, name)
        if os.path.isdir(directory) and (
            f"回{low}-{high}" in name or f"回{low:02d}-{high:02d}" in name
        ):
            return directory
    return None


def strip_front_matter(text: str) -> str:
    """Drop a leading YAML block, which a vault note may carry and a book must not."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip()
    return text


def promote_headings(markdown: str) -> str:
    """Lift every heading one level, so matter authored at ## becomes a chapter-level #.

    Chapter titles must be the top heading for an ebook reader to build navigation from
    them; a document whose only # is the book title gets no table of contents.
    """
    return re.sub(
        r"^(#{2,6})(\s)",
        lambda m: "#" * (len(m.group(1)) - 1) + m.group(2),
        markdown,
        flags=re.M,
    )


def main() -> int:
    argv = sys.argv[1:]
    book = next((a for a in argv if not a.startswith("--")), "1")
    cfg = load_config(argv)
    root = os.path.expanduser(cfg["project"]["root"])
    entry = (cfg.get("books") or {}).get(str(book), {})
    low, high = entry.get("hui", [0, 0])

    folder = book_folder(cfg, book)
    if not folder:
        print(f"no book folder for book {book} under the configured vault", file=sys.stderr)
        return 1

    chapters = sorted(
        name for name in os.listdir(folder) if re.match(r"^回\d+ \(edited\)\.md$", name)
    )
    if not chapters:
        print(f"no chapter files in {folder}", file=sys.stderr)
        return 1

    parts: list[str] = []
    title = entry.get("title") or f"Book {book}"
    subtitle = entry.get("subtitle") or ""
    parts.append(f"% {title}\n% {cfg.get('original_author', '')}\n")
    parts.append(f"# {title}\n\n{subtitle}\n")

    front = os.path.join(folder, "_Front Matter.md")
    if os.path.isfile(front):
        with open(front, encoding="utf-8") as handle:
            parts.append(promote_headings(strip_front_matter(handle.read())))

    # A manual contents list, not a generated field: ebook converters do not run
    # "update table", so an uncomputed field renders empty and the navigation is lost.
    parts.append("# Contents\n")
    entries = []
    for name in chapters:
        hui = int(re.search(r"\d+", name).group(0))
        entries.append(f"- [Chapter {hui}](#chapter-{hui})")
    parts.append("\n".join(entries) + "\n")

    for name in chapters:
        hui = int(re.search(r"\d+", name).group(0))
        with open(os.path.join(folder, name), encoding="utf-8") as handle:
            body = strip_front_matter(handle.read())
        # The vault names chapters 回NN; a reader wants "Chapter N".
        body = re.sub(r"^#\s*回\s*\d+\s*$", f"# Chapter {hui}", body, count=1, flags=re.M)
        if not body.lstrip().startswith("#"):
            body = f"# Chapter {hui}\n\n{body}"
        parts.append(body.rstrip() + "\n")

    back = os.path.join(folder, "_Back Matter.md")
    if os.path.isfile(back):
        with open(back, encoding="utf-8") as handle:
            parts.append(promote_headings(strip_front_matter(handle.read())))

    exports = os.path.join(root, "exports")
    os.makedirs(exports, exist_ok=True)
    master = os.path.join(exports, f"Book{book}_master.md")
    with open(master, "w", encoding="utf-8") as handle:
        handle.write("\n\n".join(parts))
    print(f"wrote {master} ({len(chapters)} chapters, 回{low}-{high})")

    pandoc = shutil.which("pandoc")
    if not pandoc:
        print("pandoc not installed — skipping .docx and .epub (the master is enough)")
        return 0

    for target, extra in (
        (os.path.join(exports, f"Book{book}.docx"), []),
        (os.path.join(exports, f"Book{book}.epub"), ["--toc", "--toc-depth=1"]),
    ):
        reference = os.path.join(exports, "ref.docx")
        args = [pandoc, master, "-o", target, "--from", "markdown", *extra]
        if target.endswith(".docx") and os.path.isfile(reference):
            args.append(f"--reference-doc={reference}")
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"wrote {target}")
        else:
            print(f"pandoc failed for {target}: {result.stderr.strip()[-300:]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
