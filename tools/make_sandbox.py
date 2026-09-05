#!/usr/bin/env python3
"""Build a self-contained translation project for an end-to-end verification run.

The finishing stages write the Obsidian vault, `exports/`, and `exports/print/`.
Pointing them at a shipped volume would overwrite the book. So this copies a real
project into a sandbox root, trims it to a few chapters, and repoints
`project.root` and `project.vault_book_dir` inside the sandbox. Nothing the run
does can reach the original.

    uv run python tools/make_sandbox.py --config fixtures/testbook/pipeline/config.json \
        --book 1 --chapters 1,2,3

Prints the sandbox config path to use with `book.py --config`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Scripts the Flow shells out to, plus what they import.
SCRIPTS = (
    "assemble.py",
    "assemble_print.py",
    "build_pass2.py",
    "harvest_beatplan.py",
    "harvest_reprocess.py",
    "epub.css",
)
SCRIPT_DIRS = ("checks",)
# Print theme + fonts, needed by assemble_print.py -> Typst.
PRINT_ITEMS = ("theme.typ", "copy.json", "fonts")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--book", type=int, required=True)
    parser.add_argument("--chapters", required=True, help="comma-joined hui")
    parser.add_argument("--name", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--into", default="", help="parent dir for the scratch project")
    args = parser.parse_args()

    source_config = Path(args.config).expanduser().resolve()
    cfg = json.loads(source_config.read_text(encoding="utf-8"))
    # The config always lives at <root>/pipeline/config.json, so its location is a more
    # reliable root than the `root` field — which lets the committed fixture carry a
    # FIXTURE_ROOT placeholder instead of somebody's absolute path.
    source_root = source_config.parent.parent
    cfg["project"]["root"] = str(source_root)
    cfg["project"]["vault_book_dir"] = str(
        Path(
            cfg["project"]
            .get("vault_book_dir", "")
            .replace("FIXTURE_ROOT", str(source_root))
        ).expanduser()
    )
    slug = cfg["project"]["slug"]
    huis = [int(x) for x in args.chapters.split(",") if x.strip()]
    name = args.name or f"{slug}-b{args.book}-verify"

    # Under the repo, not under ~/fanyi: a fresh clone has no ~/fanyi, and a scratch
    # tree is easier to inspect and delete where you cloned. `.run/` is gitignored.
    sandbox = Path(args.into).expanduser() / name if args.into else (
        Path(__file__).resolve().parent.parent / ".run" / name
    )
    if sandbox.exists():
        if not args.force:
            print(f"{sandbox} already exists — pass --force to rebuild")
            return 1
        shutil.rmtree(sandbox)

    (sandbox / "pipeline").mkdir(parents=True)
    (sandbox / "source" / "chapters").mkdir(parents=True)
    (sandbox / "cutlists").mkdir(parents=True)
    (sandbox / "exports").mkdir(parents=True)

    # 1. Chinese source — the IP wall. Copied read-only; the run never writes here.
    pattern = cfg["project"]["source_pattern"]
    for hui in huis:
        source = source_root / pattern.format(n=hui)
        if not source.is_file():
            print(f"missing source chapter: {source}")
            return 1
        target = sandbox / pattern.format(n=hui)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    # 2. The project's own pipeline scripts, unmodified — the whole point is to
    #    exercise the real ones.
    for script in SCRIPTS:
        origin = source_root / "pipeline" / script
        if origin.is_file():
            shutil.copy2(origin, sandbox / "pipeline" / script)
    for directory in SCRIPT_DIRS:
        origin = source_root / "pipeline" / directory
        if origin.is_dir():
            shutil.copytree(origin, sandbox / "pipeline" / directory)

    # 3. Print theme + fonts.
    for item in PRINT_ITEMS:
        origin = source_root / "print" / item
        if origin.is_dir() or origin.is_symlink():
            target = sandbox / "print" / item
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(origin, target, symlinks=False, dirs_exist_ok=True)
        elif origin.is_file():
            (sandbox / "print").mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, sandbox / "print" / item)

    # 3b. The pandoc reference docx. Without it assemble.py falls back to Pandoc's
    #     defaults (Consolas body, no first-line indent) and its own KDP style check
    #     fails — a sandbox artifact that would look like a real regression.
    ref = source_root / "exports" / "ref.docx"
    if ref.is_file():
        shutil.copy2(ref, sandbox / "exports" / "ref.docx")

    # 4. A sandbox "vault": real front/back matter, no chapters (the run writes those).
    lo, hi = min(huis), max(huis)
    book_dir = sandbox / "vault" / f"Book {args.book} — 回{lo}-{hi} (translation)"
    book_dir.mkdir(parents=True)
    real_vault = Path(cfg["project"]["vault_book_dir"]).expanduser()
    copied_matter = 0
    for candidate in sorted(real_vault.glob("Book *")):
        front = candidate / "_Front Matter.md"
        back = candidate / "_Back Matter.md"
        if front.is_file() and back.is_file():
            shutil.copy2(front, book_dir / "_Front Matter.md")
            shutil.copy2(back, book_dir / "_Back Matter.md")
            copied_matter = 1
            break
    if not copied_matter:
        print(f"WARNING: no front/back matter found under {real_vault}")

    # 5. The config, repointed and trimmed. Everything else — voice, tier policy,
    #    naming, verse policy — is the real project's, byte for byte.
    cfg["project"] = {
        **cfg["project"],
        "root": str(sandbox),
        "vault_book_dir": str(sandbox / "vault"),
        "slug": f"{slug}-verify",
    }
    original = cfg["books"][str(args.book)]
    cfg["books"] = {
        str(args.book): {
            **original,
            "hui": [lo, hi],
            "status": "SANDBOX",
        }
    }
    target_config = sandbox / "pipeline" / "config.json"
    target_config.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(f"sandbox: {sandbox}")
    print(f"  source   : {len(huis)} chapter(s) {huis}")
    print(f"  scripts  : {sorted(p.name for p in (sandbox / 'pipeline').glob('*.py'))}")
    print(f"  vault    : {book_dir}")
    print(f"  book {args.book}   : hui {lo}-{hi} \"{original.get('title', '')}\"")
    print(f"\nrun it with:\n  --config {target_config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
