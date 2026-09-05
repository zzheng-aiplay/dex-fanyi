from __future__ import annotations

import os

import json

import pytest

from fanyi_dex.claude_cli import parse_json_object
from fanyi_dex.flow import _ints, _join, _scrape_uncertain
from fanyi_dex.project import Project
from fanyi_dex.prompts import beatplan_prompt

# A real project's config, for the handful of tests that assert against one. Unset by
# default so a clone skips them instead of failing on a path it does not have.
TK_CONFIG = os.path.expanduser(os.environ.get("FANYI_TK_CONFIG", ""))


# -- PARSE port ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        'prose before {"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'thinking...\n```json\n{"a": 1}\n```\ntrailing',
    ],
)
def test_parse_recovers_object(text: str) -> None:
    assert parse_json_object(text) == {"a": 1}


@pytest.mark.parametrize("text", [None, "", "no braces here", "{not json}", "[1,2]"])
def test_parse_rejects_non_object(text) -> None:
    assert parse_json_object(text) is None


def test_parse_keeps_outermost_braces() -> None:
    assert parse_json_object('{"a": {"b": 2}}') == {"a": {"b": 2}}


# -- list-as-string plumbing --------------------------------------------------


def test_ints_and_join_round_trip() -> None:
    assert _ints("1,2,3") == [1, 2, 3]
    assert _join([1, 2, 3]) == "1,2,3"
    assert _ints("") == []
    assert _join([]) == ""
    assert _ints(" 4 , 5 ") == [4, 5]


# -- harvest stdout scraping --------------------------------------------------


def test_scrape_uncertain_reads_harvest_line() -> None:
    stdout = (
        "harvested 35 chapter plans\n"
        "uncertain tier calls -> /x/cutlists/beatplan_review.json (63 to review)\n"
    )
    assert _scrape_uncertain(stdout) == 63


def test_scrape_uncertain_defaults_to_zero() -> None:
    assert _scrape_uncertain("nothing relevant") == 0


# -- project config handling --------------------------------------------------


def _write_config(tmp_path, **overrides):
    cfg = {
        "project": {"root": str(tmp_path), "source_pattern": "source/hui_{n:03d}.txt"},
        "source_work": "Test Work",
        "books": {"1": {"hui": [1, 3], "title": "T"}},
        "structure": {"tier_handling": "TH", "verbatim_policy": "VP"},
        "cast_tiers": {"floor_rule": "FR", "tier1": ["A"], "tier2": ["B"]},
        "naming": {"courtesy_names": "CN"},
    }
    cfg.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def test_books_ignores_annotation_keys(tmp_path) -> None:
    """A config may carry a "_note" key beside the numbered books."""
    path = _write_config(
        tmp_path, books={"1": {"hui": [1, 2]}, "_note": "a comment"}
    )
    assert sorted(Project(path).books) == ["1"]


def test_missing_books_is_reported_not_raised(tmp_path) -> None:
    """An unabridged project has no volume plan at all."""
    path = _write_config(tmp_path, books={})
    assert "books" in Project(path).missing_keys()


def test_complete_config_reports_nothing_missing(tmp_path) -> None:
    assert Project(_write_config(tmp_path)).missing_keys() == []


def test_missing_tier_config_is_reported(tmp_path) -> None:
    path = _write_config(tmp_path, structure={"verbatim_policy": "VP"})
    assert "structure.tier_handling" in Project(path).missing_keys()


def test_local_index_stays_book_relative_when_filtered(tmp_path) -> None:
    """--only must not renumber chapters; downstream numbering depends on local."""
    project = Project(_write_config(tmp_path))
    assert [(c.hui, c.local) for c in project.chapters(1, only=[3])] == [(3, 3)]


# -- prompt fidelity ----------------------------------------------------------


@pytest.mark.skipif(
    not __import__("pathlib").Path(TK_CONFIG).is_file(),
    reason="set FANYI_TK_CONFIG to run these",
)
def test_prompt_carries_every_config_block() -> None:
    project = Project(TK_CONFIG)
    prompt = beatplan_prompt(project, 12, "測試源文", aggressive=False)
    for marker in (
        "BEAT-PLANNER",
        Project(TK_CONFIG).source_work,
        "*** THE TWO TIERS",
        "*** WHEN A BEAT MUST BE FULL REGARDLESS",
        "*** CAST FLOOR",
        "MAJOR:",
        "NAMING:",
        "=== SOURCE (chapter 12) ===",
        "測試源文",
        "*** OUTPUT FORMAT",
    ):
        assert marker in prompt, marker
    assert "Choose the beat count the chapter needs" in prompt
    assert "COMPRESSION DIRECTIVE" not in prompt


@pytest.mark.skipif(
    not __import__("pathlib").Path(TK_CONFIG).is_file(),
    reason="set FANYI_TK_CONFIG to run these",
)
def test_aggressive_swaps_the_beat_count_clause() -> None:
    project = Project(TK_CONFIG)
    prompt = beatplan_prompt(project, 105, "測試", aggressive=True)
    assert "COMPRESSION DIRECTIVE" in prompt
    assert "COMPRESSION DIRECTIVE" in prompt
    assert "Choose the beat count the chapter needs" not in prompt
