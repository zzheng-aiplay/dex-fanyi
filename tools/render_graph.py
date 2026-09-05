#!/usr/bin/env python3
"""Render the Flow graphs as Mermaid, from Dex's own Flow Definition Graph.

`dexcli visualize` (0.1.21+) is the source of truth: it resolves branch conditions,
wait conditions, SubFlow edges, and failure routes out of the source, and reports
`valid: false` with diagnostics when a Flow hides control flow behind a helper. This
script only shapes that JSON into a diagram for the docs — it never re-derives the
graph, so the picture cannot drift from the code.

    uv run python tools/render_graph.py                  # Mermaid to stdout
    uv run python tools/render_graph.py --check          # just assert every Flow is valid
    uv run python tools/render_graph.py --write GRAPH.md # regenerate the doc

Interactive rendering is `dexcli visualize <file>` with no flags.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "fanyi_dex/book/book_flow.py",
    "fanyi_dex/book/curate_chapter.py",
    "fanyi_dex/book/produce_chapter.py",
)
# Steps whose whole job is to absorb a failure — drawn to one side.
RECOVERY = {"RecoveryGate", "PlanFailedStep", "ProduceFailedStep"}


def fdg(path: Path, interpreter: str) -> dict:
    result = subprocess.run(
        [
            "dexcli",
            "visualize",
            str(path),
            "--json",
            "--out",
            "-",
            "--python",
            interpreter,
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(f"dexcli visualize {path.name} failed:\n{result.stderr}")
    return json.loads(result.stdout)


def short(step_id: str) -> str:
    return step_id.split(":")[-1]


def condition(text: str) -> str:
    """Reduce the analyser's guard to the branch's own test, readably.

    The FDG records each branch's guard as every preceding branch negated and ANDed with
    its own, so the else-branch of `if not records:` arrives as
    `not (not records)` and the last arm of a gate as
    `not (frozen.auto_approve) and not (context.has_timer_fired()) and
     not (decision.decision != 'approve')`. Printed verbatim that is unreadable, and it
    was: the first web render showed edges labelled `not not records`.
    """
    if not text:
        return ""
    text = text.strip()
    if text == "otherwise":
        return "otherwise"
    own = [p.strip() for p in text.split(" and ")][-1]
    return _simplify(own)[:52]


def _simplify(text: str) -> str:
    text = _unwrap(text)
    if not text.startswith("not "):
        return text
    inner = _simplify(text[4:])
    if inner.startswith("not "):
        return inner[4:]  # not (not x) is x
    # `not (a != b)` reads better as `a == b`, and likewise for the other operators.
    for negated, plain in ((" != ", " == "), (" == ", " != "), (" > ", " <= "), (" < ", " >= ")):
        if negated in inner:
            return inner.replace(negated, plain, 1)
    return f"not {inner}"


def _unwrap(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def mermaid(graphs: list[dict]) -> str:
    lines = ["flowchart TD"]
    for index, graph in enumerate(graphs):
        flow = graph["flow"]
        nodes = {n["id"]: n for n in graph["nodes"]}
        prefix = f"f{index}"
        start = short(flow["startStepId"])

        lines.append(f'  subgraph {prefix}_sub["{flow["name"]}"]')
        lines.append("    direction TB")
        for node in graph["nodes"]:
            if node["kind"] != "step":
                continue
            name = short(node["id"])
            gates = _gate_label(graph, node["id"])
            label = f"{name}<br/><i>{gates}</i>" if gates else name
            if name == start:
                lines.append(f'    {prefix}_{name}(["{label}"]):::start')
            elif name in RECOVERY:
                lines.append(f'    {prefix}_{name}["{label}"]:::recovery')
            elif gates:
                lines.append(f'    {prefix}_{name}{{{{"{label}"}}}}:::gate')
            else:
                lines.append(f'    {prefix}_{name}["{label}"]')

        for node in graph["nodes"]:
            if node["kind"] != "decision":
                continue
            kind = node["decision"]["type"]
            if kind in ("goTo", "rpcResult"):
                continue
            parent = short(node["parentId"])
            terminal = "complete" if kind == "gracefulComplete" else "FAIL"
            node_id = f"{prefix}_{parent}_{kind}"
            lines.append(f'    {node_id}(("{terminal}"))')
            guard = condition(node.get("condition", ""))
            arrow = f'-- "{guard}" -->' if guard and guard != "otherwise" else "-->"
            lines.append(f"    {prefix}_{parent} {arrow} {node_id}")

        for edge in graph["edges"]:
            if edge["kind"] == "transition":
                source = nodes.get(edge["from"], {})
                origin = short(source.get("parentId", edge["from"]))
                target = short(edge["to"])
                guard = condition(source.get("condition", ""))
                if origin == target:
                    lines.append(f'    {prefix}_{origin} -. "{guard or "retry"}" .-> {prefix}_{origin}')
                elif guard:
                    lines.append(f'    {prefix}_{origin} -- "{guard}" --> {prefix}_{target}')
                else:
                    lines.append(f"    {prefix}_{origin} --> {prefix}_{target}")
            elif edge["kind"] == "failure_transition":
                lines.append(
                    f'    {prefix}_{short(edge["from"])} -. "retries exhausted" '
                    f'.-> {prefix}_{short(edge["to"])}'
                )
        lines.append("    end")

    # Parent step -> child Flow, from the SubFlow wait conditions.
    child_by_attr = {"self.curate": "FanyiCurateChapter", "self.produce": "FanyiProduceChapter"}
    index_by_flow = {g["flow"]["name"]: i for i, g in enumerate(graphs)}
    start_by_flow = {g["flow"]["name"]: short(g["flow"]["startStepId"]) for g in graphs}
    for index, graph in enumerate(graphs):
        for node in graph["nodes"]:
            if node["kind"] != "wait":
                continue
            for cond in node.get("wait", {}).get("conditions", []):
                child = child_by_attr.get(cond.get("label", ""))
                if not child or child not in index_by_flow:
                    continue
                parent_step = short(node["parentId"])
                other = index_by_flow[child]
                lines.append(
                    f"  f{index}_{parent_step} ==>|one SubFlow per chapter, "
                    f"bounded batch| f{other}_{start_by_flow[child]}"
                )

    lines += [
        "  classDef start fill:#1f6f3f,stroke:#0d3,color:#fff",
        "  classDef gate fill:#7a4b00,stroke:#fa0,color:#fff",
        "  classDef recovery fill:#6b1220,stroke:#c33,color:#fff",
    ]
    return "\n".join(lines)


def _gate_label(graph: dict, step_id: str) -> str:
    """Summarise what a Step waits on, e.g. 'GATE · Channel or Timer'."""
    labels: list[str] = []
    for node in graph["nodes"]:
        if node["kind"] != "wait" or node.get("parentId") != step_id:
            continue
        wait = node.get("wait", {})
        if wait.get("type") == "skipWaitImmediately":
            labels.append("no wait")
            continue
        kinds = []
        for cond in wait.get("conditions", []):
            kind = cond.get("kind")
            if kind == "subflow":
                kinds.append("SubFlow batch")
            elif kind == "channel":
                kinds.append("Channel")
            elif kind == "timer":
                kinds.append("Timer")
            else:
                kinds.append(kind or "?")
        labels.append(f"{wait.get('type')}: {' + '.join(dict.fromkeys(kinds))}")
    return " / ".join(dict.fromkeys(labels))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default="/usr/local/bin/python3")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write")
    args = parser.parse_args()

    graphs = []
    for name in FILES:
        graph = fdg(ROOT / name, args.python)
        flow = graph["flow"]["name"]
        steps = sum(1 for n in graph["nodes"] if n["kind"] == "step")
        status = "valid" if graph["valid"] else "INVALID"
        print(
            f"{status}: {flow} — {steps} steps, {len(graph['nodes'])} nodes, "
            f"{len(graph['edges'])} edges, {len(graph['diagnostics'])} diagnostics",
            file=sys.stderr,
        )
        for diagnostic in graph["diagnostics"]:
            print(f"  {json.dumps(diagnostic)}", file=sys.stderr)
        if not graph["valid"]:
            return 1
        graphs.append(graph)

    if args.check:
        return 0

    body = mermaid(graphs)
    if args.write:
        Path(args.write).write_text(
            "<!-- generated by tools/render_graph.py from `dexcli visualize --json` -->\n"
            "# Flow graphs\n\n```mermaid\n" + body + "\n```\n",
            encoding="utf-8",
        )
        print(f"wrote {args.write}", file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
