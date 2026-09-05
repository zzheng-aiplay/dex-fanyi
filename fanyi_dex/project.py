"""Reads a translation project's own `pipeline/config.json`.

That file is the project's single source of truth for naming, voice, tier
policy, and book ranges. Nothing here restates it — this module only locates
chapters and output paths, and checks that a project actually carries the config
the beat-plan stage needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Keys the beat-plan prompt is assembled from. A project that has not written its tier
# policy yet, and an unabridged project that never will, both fail this check by design
# rather than crashing mid-flow.
REQUIRED_KEYS = (
    ("structure", "tier_handling"),
    ("structure", "verbatim_policy"),
    ("cast_tiers", "floor_rule"),
    ("cast_tiers", "tier1"),
    ("cast_tiers", "tier2"),
)


class ConfigUnsupported(RuntimeError):
    """The project config lacks what this stage needs."""


@dataclass(frozen=True)
class Chapter:
    hui: int
    local: int


class Project:
    def __init__(self, config_path: str) -> None:
        self.config_path = str(Path(config_path).expanduser())
        with open(self.config_path, encoding="utf-8") as handle:
            self.cfg = json.load(handle)
        # A project config normally carries an absolute root. The committed fixture
        # carries the token FIXTURE_ROOT instead, so the repo holds no machine-specific
        # path — resolved here, against the config's own location, so the fixture works
        # when pointed at directly and not only after tools/make_sandbox.py rewrites it.
        here = Path(self.config_path).resolve().parent.parent
        for key in ("root", "vault_book_dir"):
            value = self.cfg["project"].get(key)
            if isinstance(value, str) and "FIXTURE_ROOT" in value:
                self.cfg["project"][key] = value.replace("FIXTURE_ROOT", str(here))
        self.root = Path(self.cfg["project"]["root"]).expanduser()
        self.source_pattern = self.cfg["project"]["source_pattern"]
        # `books` may be absent — an unabridged project has no volume plan — and may
        # carry annotation keys like "_note" alongside the numbered entries, so keep only
        # the numeric ones.
        raw_books = self.cfg.get("books") or {}
        self.books = {k: v for k, v in raw_books.items() if str(k).isdigit()}

    # -- identity ---------------------------------------------------------

    @property
    def source_work(self) -> str:
        return self.cfg.get("source_work", "this work")

    @property
    def name(self) -> str:
        return self.cfg.get("project", {}).get("name") or self.root.name

    # -- validation -------------------------------------------------------

    def missing_keys(self) -> list[str]:
        missing = []
        if not self.books:
            missing.append("books")
        for section, key in REQUIRED_KEYS:
            value = self.cfg.get(section, {})
            if not isinstance(value, dict) or not value.get(key):
                missing.append(f"{section}.{key}")
        return missing

    def require_beatplan_support(self) -> None:
        missing = self.missing_keys()
        if missing:
            raise ConfigUnsupported(
                f"{self.name} cannot run the beat-plan stage — "
                f"config is missing: {', '.join(missing)}"
            )

    # -- chapters ---------------------------------------------------------

    def chapter_range(self, book: int) -> tuple[int, int]:
        entry = self.books.get(str(book))
        if entry is None:
            raise ValueError(
                f"book {book} not in config (have: {', '.join(sorted(self.books))})"
            )
        lo, hi = entry["hui"]
        return int(lo), int(hi)

    def chapters(self, book: int, only: list[int] | None = None) -> list[Chapter]:
        """Chapters of `book`, each with its per-book local index.

        `local` must stay the book-relative position even when `only` filters the
        set, because downstream numbering depends on it.
        """
        lo, hi = self.chapter_range(book)
        out = []
        for local, hui in enumerate(range(lo, hi + 1), 1):
            if only and hui not in only:
                continue
            out.append(Chapter(hui=hui, local=local))
        return out

    def source_path(self, hui: int) -> Path:
        return self.root / self.source_pattern.format(n=hui)

    def read_source(self, hui: int) -> str:
        path = self.source_path(hui)
        if not path.is_file():
            raise FileNotFoundError(f"missing source chapter: {path}")
        return path.read_text(encoding="utf-8").strip()

    def book_title(self, book: int) -> str:
        return self.books.get(str(book), {}).get("title", f"Book {book}")

    # -- outputs ----------------------------------------------------------

    def stage_dir(self, book: int, stage: str = "beatplan") -> Path:
        """Where this runner writes. Namespaced under `dex/` so it can never be
        confused with artifacts the Workflow-tool path produced."""
        return self.root / "pipeline" / "run" / "dex" / stage / f"book{book}"

    def plan_path(self, book: int, hui: int, stage: str = "beatplan") -> Path:
        return self.stage_dir(book, stage) / f"h{hui:03d}.json"

    def combined_path(self, book: int, stage: str = "beatplan") -> Path:
        """The `{"plans": [...]}` file handed to harvest_beatplan.py unchanged."""
        return self.stage_dir(book, stage) / f"{stage}_output.json"

    def failure_path(self, book: int, hui: int, stage: str = "beatplan") -> Path:
        return self.stage_dir(book, stage) / f"h{hui:03d}.FAILED.txt"

    def pipeline_script(self, name: str) -> Path:
        return self.root / "pipeline" / name

    # -- pass 1 (two-pass transcreation) ----------------------------------

    def missing_pass1_keys(self) -> list[str]:
        missing = []
        if not self.books:
            missing.append("books")
        for section, key in PASS1_REQUIRED_KEYS:
            value = self.cfg.get(section, {})
            if not isinstance(value, dict) or not value.get(key):
                missing.append(f"{section}.{key}")
        return missing

    def require_pass1_support(self) -> None:
        missing = self.missing_pass1_keys()
        if missing:
            raise ConfigUnsupported(
                f"{self.name} cannot run pass 1 — config is missing: {', '.join(missing)}"
            )

    def pass1_warnings(self) -> list[str]:
        """Non-fatal config gaps worth naming before a run."""
        warnings = []
        if not self.cfg.get("structure", {}).get("units"):
            warnings.append(
                "structure.units is empty — agents will relabel Chinese units 1:1 "
                "(九尺 -> 'nine feet', ~33% inflation)"
            )
        return warnings

    def pass1_dir(self, book: int) -> Path:
        return self.root / "pipeline" / "run" / "dex" / "pass1" / f"book{book}"

    def chapter_dir(self, book: int, hui: int) -> Path:
        return self.pass1_dir(book) / f"h{hui:03d}"

    def phase_path(self, book: int, hui: int, name: str) -> Path:
        return self.chapter_dir(book, hui) / f"{name}.json"

    def pass1_combined(self, book: int) -> Path:
        """The `{"chapters": [...]}` file harvest_reprocess.py consumes."""
        return self.pass1_dir(book) / "chapters_output.json"

    # -- approved beat-plan input -----------------------------------------

    def items_candidates(self, book: int) -> list[ItemsCandidate]:
        """Every plausible approved-beat-plan file for `book`, best-covered first.

        A project accumulates several generations of these under different names, and
        which one is canonical differs per volume: one volume's decisions file may be
        complete while its `applied` file is short a chapter, and the next volume can be
        the reverse. Two files can also both be complete and still not be
        interchangeable, when one is a further-compressed edition of the other. So this
        reports every candidate with its coverage and refuses to choose.
        """
        # Discovered by CONTENT, not by a hardcoded list of filenames: any JSON in
        # cutlists/ that parses as an items file and binds at least one chapter of this
        # volume is a candidate. That finds a generation named something nobody
        # anticipated, and needs no maintenance when a project renames its artifacts.
        cutlists = self.root / "cutlists"
        names = sorted(p.name for p in cutlists.glob("*.json")) if cutlists.is_dir() else []
        expected = 0
        if str(book) in self.books:
            lo, hi = self.chapter_range(book)
            expected = hi - lo + 1

        found = []
        for name in names:
            path = cutlists / name
            if not path.is_file():
                continue
            try:
                chapters = load_items_file(path, book)
            except (json.JSONDecodeError, OSError, KeyError):
                continue
            if not chapters:
                continue
            huis = sorted(c["hui"] for c in chapters if isinstance(c.get("hui"), int))
            found.append(
                ItemsCandidate(
                    path=path,
                    chapters=len(chapters),
                    hui_lo=huis[0] if huis else 0,
                    hui_hi=huis[-1] if huis else 0,
                    expected=expected,
                    mtime=path.stat().st_mtime,
                )
            )
        found.sort(key=lambda c: (-c.chapters, -c.mtime))
        return found


DEFAULT_ACCESS_FLOOR = 4.0
DEFAULT_ACCESS_LIFT = 0.8


def lift_held(project: "Project", score_a: float, score_b: float) -> bool:
    """Did the fluency pass earn its place?

    A newcomer scores the faithful pass (A) against the final (B). The final has to clear
    an absolute floor AND improve on A by a margin, so a pass that merely reshuffles
    words does not count as a lift. Both numbers are the project's to set:

        "quality_bar": {"access_floor": 4.0, "access_lift": 0.8}
    """
    bar = project.cfg.get("quality_bar", {}) or {}
    floor = float(bar.get("access_floor", DEFAULT_ACCESS_FLOOR))
    lift = float(bar.get("access_lift", DEFAULT_ACCESS_LIFT))
    return score_b >= floor and (score_b - score_a) >= lift


PASS1_REQUIRED_KEYS = (
    ("voice", "target"),
    ("voice", "_pass2_fluency"),
    ("structure", "tier_handling"),
    ("structure", "verbatim_policy"),
)


@dataclass(frozen=True)
class ItemsCandidate:
    path: Path
    chapters: int
    hui_lo: int
    hui_hi: int
    expected: int
    mtime: float

    @property
    def complete(self) -> bool:
        return self.expected > 0 and self.chapters >= self.expected

    def describe(self) -> str:
        coverage = f"{self.chapters}/{self.expected}" if self.expected else str(self.chapters)
        flag = "complete" if self.complete else "PARTIAL"
        return (
            f"{self.path.name}  {coverage} chapters "
            f"(hui {self.hui_lo}-{self.hui_hi})  {flag}"
        )


def load_items_file(path: Path | str, book: int | None = None) -> list[dict]:
    """Load an approved beat-plan file, tolerating both shapes on disk.

    Some generations are a bare list of chapters, others wrap them in
    `{"chapters": [...]}`. Filters to `book` when the entries carry one.
    """
    with open(Path(path).expanduser(), encoding="utf-8") as handle:
        payload = json.load(handle)
    chapters = payload if isinstance(payload, list) else payload.get("chapters", [])
    if book is not None:
        scoped = [c for c in chapters if c.get("book") == book]
        # `beatplan_decisions.json` carries every book; a per-book file may omit
        # the key entirely, in which case take it as-is.
        if scoped or any("book" in c for c in chapters):
            chapters = scoped
    return [c for c in chapters if isinstance(c, dict) and c.get("beats")]
