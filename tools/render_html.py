#!/usr/bin/env python3
"""Render all three Flow graphs into one self-contained web page.

`dexcli visualize <file>` already opens Dex's own interactive Flow Rendering page, and
that is the authority — but it takes one file at a time, so it cannot show a parent Flow
and the SubFlows it fans out to together. This builds a single page that does, and pairs
each diagram with the detail a picture cannot carry: per-Step timeouts, retry budgets,
failure routes, and which durable state each Step reads and writes.

    uv run python tools/render_html.py              # writes graph.html and opens it
    uv run python tools/render_html.py --no-open

The graph comes from `dexcli visualize --json`; the budgets come from the SDK's own
StepOptions objects. Neither is re-derived here, so the page cannot drift from the code.
"""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.render_graph import FILES, ROOT, condition, fdg, short  # noqa: E402

GATE_STEPS = {"DirectorGate", "QaGate", "ProofGate", "RecoveryGate"}
RECOVERY_STEPS = {"RecoveryGate", "PlanFailedStep", "ProduceFailedStep"}


# --------------------------------------------------------------------------
# Step budgets, read off the SDK objects the Worker actually registers
# --------------------------------------------------------------------------


def step_options() -> dict[str, dict[str, str]]:
    from fanyi_dex.book.book_flow import BookFlow
    from fanyi_dex.book.curate_chapter import CurateChapterFlow
    from fanyi_dex.book.produce_chapter import ProduceChapterFlow
    from fanyi_dex.config import Config

    config = Config.from_env()
    curate = CurateChapterFlow(config)
    produce = ProduceChapterFlow(config)
    out: dict[str, dict[str, str]] = {}
    for flow in (BookFlow(config, curate, produce), curate, produce):
        for value in vars(flow).values():
            if not hasattr(value, "get_step_options"):
                continue
            options = value.get_step_options()
            if options is None:
                continue
            name = type(value).__name__
            retry = options.execute_retry
            attempts = getattr(retry, "maximum_attempts", None) if retry else None
            target = getattr(options, "_execute_failure_target", None)
            out[name] = {
                "timeout": _minutes(options.execute_method_timeout),
                "heartbeat": _minutes(options.heartbeat_timeout),
                "attempts": str(attempts) if attempts else "uncapped",
                "failure": target.__name__ if target else "",
                "doc": (value.__doc__ or "").strip().split("\n")[0],
            }
    return out


def _minutes(delta) -> str:
    if delta is None:
        return "server default"
    seconds = int(delta.total_seconds())
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds}s"


# --------------------------------------------------------------------------
# Shape one Flow's graph for the page
# --------------------------------------------------------------------------


def wait_summary(graph: dict, step_id: str) -> list[str]:
    out = []
    for node in graph["nodes"]:
        if node["kind"] != "wait" or node.get("parentId") != step_id:
            continue
        wait = node.get("wait", {})
        guard = condition(node.get("condition", ""))
        if wait.get("type") == "skipWaitImmediately":
            out.append(f"no wait when <code>{html.escape(guard or 'always')}</code>")
            continue
        parts = []
        for cond in wait.get("conditions", []):
            kind = cond.get("kind")
            label = cond.get("label", "")
            if kind == "subflow":
                parts.append(f"SubFlow <b>{html.escape(label)}</b> &times; batch")
            elif kind == "channel":
                parts.append(f"Channel <code>{html.escape(label)}</code>")
            elif kind == "timer":
                parts.append("Timer (reminder)")
            else:
                parts.append(html.escape(str(kind)))
        joiner = " <i>or</i> " if wait.get("type") == "anyOf" else " <i>and</i> "
        out.append(joiner.join(parts))
    return out


def resources(graph: dict, step_name: str) -> tuple[list[str], list[str]]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    reads, writes = set(), set()
    for edge in graph["edges"]:
        source = nodes.get(edge["from"], {})
        target = nodes.get(edge["to"], {})
        if edge["kind"] == "resource_read" and short(edge["to"]) == step_name:
            reads.add(source.get("name", ""))
        elif edge["kind"] == "resource_read" and target.get("name") == step_name:
            reads.add(source.get("name", ""))
        elif edge["kind"] == "resource_write" and short(edge["from"]) == step_name:
            writes.add(target.get("name", ""))
    return sorted(x for x in reads if x), sorted(x for x in writes if x)


def transitions(graph: dict, step_name: str) -> list[tuple[str, str]]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    out = []
    for edge in graph["edges"]:
        if edge["kind"] != "transition":
            continue
        source = nodes.get(edge["from"], {})
        if short(source.get("parentId", "")) != step_name:
            continue
        out.append((condition(source.get("condition", "")) or "always", short(edge["to"])))
    for node in graph["nodes"]:
        if node["kind"] != "decision" or short(node.get("parentId", "")) != step_name:
            continue
        kind = node["decision"]["type"]
        if kind in ("goTo", "rpcResult"):
            continue
        label = "complete" if kind == "gracefulComplete" else "FAIL"
        out.append((condition(node.get("condition", "")) or "always", f"⏹ {label}"))
    return out


def mermaid_for(graph: dict, index: int, happy: bool = False) -> str:
    """`happy=True` drops the failure machinery: recovery Steps, their fan-out back into
    every stage, and the exhausted-retry edges. That is the same editing a hand-drawn
    diagram does silently — which is why the two pictures of one graph look nothing
    alike. Everything else is identical, node for node."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    start = short(graph["flow"]["startStepId"])
    hidden = RECOVERY_STEPS if happy else set()
    lines = ["flowchart TD"]
    for node in graph["nodes"]:
        if node["kind"] != "step":
            continue
        name = short(node["id"])
        if name in hidden:
            continue
        if name == start:
            lines.append(f'  {name}(["{name}"]):::start')
        elif name in RECOVERY_STEPS:
            lines.append(f'  {name}["{name}"]:::recovery')
        elif name in GATE_STEPS or wait_summary(graph, node["id"]):
            lines.append(f'  {name}{{{{"{name}"}}}}:::gate')
        else:
            lines.append(f'  {name}["{name}"]')

    for node in graph["nodes"]:
        if node["kind"] != "decision":
            continue
        kind = node["decision"]["type"]
        if kind in ("goTo", "rpcResult"):
            continue
        parent = short(node["parentId"])
        if parent in hidden:
            continue
        terminal = "complete" if kind == "gracefulComplete" else "FAIL"
        node_id = f"{parent}_{kind}"
        cls = "done" if kind == "gracefulComplete" else "failed"
        lines.append(f'  {node_id}(("{terminal}")):::{cls}')
        guard = _label(condition(node.get("condition", "")))
        arrow = f'-- "{guard}" -->' if guard and guard != "otherwise" else "-->"
        lines.append(f"  {parent} {arrow} {node_id}")

    for edge in graph["edges"]:
        if edge["kind"] == "transition":
            source = nodes.get(edge["from"], {})
            origin = short(source.get("parentId", edge["from"]))
            target = short(edge["to"])
            if origin in hidden or target in hidden:
                continue
            guard = _label(condition(source.get("condition", "")))
            if origin == target:
                lines.append(f'  {origin} -. "{guard or "retry"}" .-> {origin}')
            elif guard:
                lines.append(f'  {origin} -- "{guard}" --> {target}')
            else:
                lines.append(f"  {origin} --> {target}")
        elif edge["kind"] == "failure_transition" and not happy:
            lines.append(
                f'  {short(edge["from"])} -. "retries exhausted" .-> {short(edge["to"])}'
            )

    for node in graph["nodes"]:
        if node["kind"] != "wait":
            continue
        for cond in node.get("wait", {}).get("conditions", []):
            if cond.get("kind") != "subflow":
                continue
            parent = short(node["parentId"])
            if parent in hidden:
                continue
            child = cond.get("label", "").replace("self.", "")
            child_id = f"sub_{parent}"
            lines.append(f'  {child_id}[["{child} SubFlow<br/>one per chapter"]]:::sub')
            lines.append(f"  {parent} ==>|bounded batch| {child_id}")

    lines += [
        "  classDef start fill:#14532d,stroke:#22c55e,color:#e6fbef,stroke-width:2px",
        "  classDef gate fill:#4a2c00,stroke:#f59e0b,color:#fff7ed,stroke-width:2px",
        "  classDef recovery fill:#4c0519,stroke:#f43f5e,color:#fff1f2,stroke-width:2px",
        "  classDef done fill:#0c4a6e,stroke:#38bdf8,color:#f0f9ff",
        "  classDef failed fill:#450a0a,stroke:#ef4444,color:#fef2f2",
        "  classDef sub fill:#1e1b4b,stroke:#818cf8,color:#eef2ff,stroke-dasharray:4 3",
    ]
    return "\n".join(lines)


def _label(text: str) -> str:
    return text.replace('"', "'").replace("<", "&lt;")


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------


def step_table(graph: dict, budgets: dict[str, dict[str, str]]) -> str:
    start = short(graph["flow"]["startStepId"])
    rows = []
    for node in graph["nodes"]:
        if node["kind"] != "step":
            continue
        name = short(node["id"])
        budget = budgets.get(name, {})
        waits = wait_summary(graph, node["id"])
        reads, writes = resources(graph, name)
        moves = transitions(graph, name)
        badge = ""
        if name == start:
            badge = '<span class="badge start">start</span>'
        elif name in RECOVERY_STEPS:
            badge = '<span class="badge recovery">failure</span>'
        elif waits:
            badge = '<span class="badge gate">waits</span>'
        rows.append(
            f"""<tr>
  <td class="step"><b>{html.escape(name)}</b>{badge}
      <div class="doc">{html.escape(budget.get("doc", ""))}</div></td>
  <td>{"<br>".join(waits) or "<span class=dim>&mdash;</span>"}</td>
  <td class="nowrap">{html.escape(budget.get("timeout", "?"))}
      <div class="dim">hb {html.escape(budget.get("heartbeat", "?"))}</div>
      <div class="dim">{html.escape(budget.get("attempts", "?"))} attempts</div></td>
  <td>{"".join(f'<div><code>{html.escape(c)}</code> → <b>{html.escape(t)}</b></div>' for c, t in moves) or "<span class=dim>&mdash;</span>"}
      {f'<div class="fail">retries exhausted → <b>{html.escape(budget["failure"])}</b></div>' if budget.get("failure") else ""}</td>
  <td class="res">{"".join(f'<span class="w">{html.escape(w)}</span>' for w in writes)}
      {"".join(f'<span class="r">{html.escape(r)}</span>' for r in reads)}</td>
</tr>"""
        )
    return "\n".join(rows)


def build(graphs: list[dict], budgets: dict[str, dict[str, str]]) -> str:
    sections = []
    for index, graph in enumerate(graphs):
        flow = graph["flow"]
        steps = sum(1 for n in graph["nodes"] if n["kind"] == "step")
        rpcs = [n["name"] for n in graph["nodes"] if n["kind"] == "rpc"]
        sections.append(
            f"""
<section id="f{index}">
  <h2>{html.escape(flow["name"])}
    <span class="meta">{steps} steps · {len(graph["nodes"])} nodes ·
      {len(graph["edges"])} edges · {len(graph["diagnostics"])} diagnostics ·
      <span class="{'ok' if graph['valid'] else 'bad'}">{'valid' if graph['valid'] else 'INVALID'}</span></span>
  </h2>
  <div class="views">
    <div class="mermaid" data-variant="happy">{html.escape(mermaid_for(graph, index, happy=True))}</div>
    <div class="mermaid hide" data-variant="full">{html.escape(mermaid_for(graph, index))}</div>
  </div>
  <table>
    <thead><tr><th>Step</th><th>Waits on</th><th>Budget</th><th>Goes to</th>
      <th>Durable state <span class="dim">(writes / direct reads)</span></th></tr></thead>
    <tbody>{step_table(graph, budgets)}</tbody>
  </table>
  {f'<p class="rpc">RPC: <code>{", ".join(html.escape(r) for r in rpcs)}</code></p>' if rpcs else ""}
</section>"""
        )

    nav = " ".join(
        f'<a href="#f{i}">{html.escape(g["flow"]["name"])}</a>'
        for i, g in enumerate(graphs)
    )
    return f"""<!doctype html>
<meta charset="utf-8">
<title>dex-fanyi — Flow graphs</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 2rem clamp(1rem, 4vw, 4rem) 6rem;
    background: #0b0d12; color: #d7dce5;
    font: 15px/1.6 ui-sans-serif, -apple-system, "Segoe UI", sans-serif; }}
  header {{ border-bottom: 1px solid #1e2430; padding-bottom: 1.4rem; margin-bottom: 2rem; }}
  h1 {{ margin: 0 0 .3rem; font-size: 1.5rem; letter-spacing: -.01em; }}
  h1 span {{ color: #7d8798; font-weight: 400; font-size: .95rem; }}
  .sub {{ color: #8b95a7; max-width: 78ch; }}
  nav {{ margin-top: 1rem; display: flex; gap: .5rem; flex-wrap: wrap; }}
  nav a {{ color: #93c5fd; text-decoration: none; border: 1px solid #1e3a5f;
    padding: .25rem .7rem; border-radius: 999px; font-size: .85rem; }}
  nav a:hover {{ background: #10233c; }}
  section {{ margin: 3.5rem 0; }}
  h2 {{ font-size: 1.15rem; margin: 0 0 1rem; display: flex; gap: .8rem;
    align-items: baseline; flex-wrap: wrap; }}
  .meta {{ font-weight: 400; font-size: .8rem; color: #7d8798; }}
  .ok {{ color: #4ade80; }} .bad {{ color: #f87171; }}
  .mermaid {{ background: #0f1219; border: 1px solid #1e2430; border-radius: 10px;
    padding: 1.2rem; overflow-x: auto; margin-bottom: 1.4rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .82rem; }}
  th {{ text-align: left; color: #8b95a7; font-weight: 500; padding: .5rem .7rem;
    border-bottom: 1px solid #1e2430; font-size: .78rem;
    text-transform: uppercase; letter-spacing: .05em; }}
  td {{ padding: .7rem; border-bottom: 1px solid #151a24; vertical-align: top; }}
  tr:hover td {{ background: #0f1319; }}
  .step {{ min-width: 15ch; }}
  .doc {{ color: #737d8f; font-size: .78rem; margin-top: .2rem; max-width: 42ch; }}
  .dim {{ color: #6b7482; font-size: .76rem; }}
  .nowrap {{ white-space: nowrap; }}
  .fail {{ color: #fb7185; margin-top: .3rem; font-size: .78rem; }}
  code {{ background: #161b26; padding: .1rem .35rem; border-radius: 4px;
    font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; color: #c4b5fd; }}
  .res span {{ display: inline-block; margin: 0 .25rem .25rem 0; padding: .1rem .4rem;
    border-radius: 4px; font: 11px ui-monospace, Menlo, monospace; }}
  .res .w {{ background: #14532d; color: #bbf7d0; }}
  .res .r {{ background: #1e293b; color: #94a3b8; }}
  .badge {{ font-size: .65rem; text-transform: uppercase; letter-spacing: .06em;
    padding: .1rem .4rem; border-radius: 4px; margin-left: .45rem; vertical-align: 1px; }}
  .badge.start {{ background: #14532d; color: #86efac; }}
  .badge.gate {{ background: #4a2c00; color: #fcd34d; }}
  .badge.recovery {{ background: #4c0519; color: #fda4af; }}
  .legend {{ display: flex; gap: 1.2rem; flex-wrap: wrap; color: #8b95a7;
    font-size: .8rem; margin-top: .8rem; }}
  .legend b {{ color: #d7dce5; font-weight: 500; }}
  .rpc {{ color: #8b95a7; font-size: .82rem; }}
  .caveat {{ margin-top: 1rem; font-size: .82rem; border-left: 2px solid #2b3446;
    padding-left: .9rem; }}
  .hide {{ display: none; }}
  .toggle {{ margin-top: 1.2rem; display: flex; align-items: center; gap: .6rem;
    flex-wrap: wrap; }}
  .toggle button {{ background: #141a24; color: #93a3b8; border: 1px solid #263041;
    padding: .3rem .8rem; border-radius: 6px; font: inherit; font-size: .82rem;
    cursor: pointer; }}
  .toggle button.on {{ background: #1d3557; color: #dbeafe; border-color: #3b6ea5; }}
  .toggle .dim {{ font-size: .78rem; max-width: 60ch; }}
  footer {{ margin-top: 4rem; padding-top: 1.4rem; border-top: 1px solid #1e2430;
    color: #6b7482; font-size: .8rem; }}
</style>

<header>
  <h1>dex-fanyi &mdash; Flow graphs <span>one volume, initiation to final product</span></h1>
  <p class="sub">Generated from <code>dexcli visualize --json</code>; per-Step budgets read
  off the SDK's own <code>StepOptions</code>. Nothing here is hand-drawn, so the picture
  cannot drift from the code. Regenerate with
  <code>uv run python tools/render_html.py</code>.</p>
  <div class="legend">
    <span><b>◇ amber</b> waits on something (a gate, or a batch of SubFlows)</span>
    <span><b>▭ red</b> absorbs a failure</span>
    <span><b>dotted</b> exhausted retries</span>
    <span><b>thick</b> SubFlow fan-out</span>
    <span><b class="w" style="background:#14532d;color:#bbf7d0;padding:0 .3rem;border-radius:3px">green</b> state written</span>
  </div>
  <p class="sub caveat">One limit worth knowing: the analyser attributes a resource read
  to the Step only when the Step reads it <i>directly</i>. Reads routed through a helper
  (<code>_plan(context)</code>, <code>_recount(context)</code>) do not appear, so the
  read column understates. Control flow has no such gap &mdash; a helper that hid a wait,
  a movement, or a recovery target would make the Flow report
  <span class="bad">INVALID</span>, and all three report valid.</p>
  <div class="toggle">
    <button id="btn-happy" class="on">happy path</button><button id="btn-full">every path</button>
    <span class="dim" id="toggle-note">the failure machinery is hidden &mdash; recovery Steps,
      their fan-out back into every stage, and the exhausted-retry edges</span>
  </div>
  <nav>{nav}</nav>
</header>
{"".join(sections)}
<footer>
  <code>dexcli visualize fanyi_dex/book/book_flow.py</code> opens Dex's own interactive
  Flow Rendering page for a single Flow &mdash; that is the authority. This page exists to
  show the parent Flow and both chapter SubFlows together, with the budgets and durable
  state a diagram alone cannot carry.
</footer>

<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{
    startOnLoad: false,
    theme: 'dark',
    themeVariables: {{ darkMode: true, background: '#0f1219', primaryColor: '#1b2130',
      primaryTextColor: '#d7dce5', primaryBorderColor: '#2b3446', lineColor: '#5a6478',
      fontSize: '13px' }},
    flowchart: {{ curve: 'basis', nodeSpacing: 45, rankSpacing: 55, useMaxWidth: false }},
  }});

  const rendered = new WeakSet();
  async function show(variant) {{
    const pending = [...document.querySelectorAll(`.mermaid[data-variant="${{variant}}"]`)]
      .filter(node => !rendered.has(node));
    document.querySelectorAll('.mermaid').forEach(node =>
      node.classList.toggle('hide', node.dataset.variant !== variant));
    if (pending.length) {{
      pending.forEach(node => rendered.add(node));
      await mermaid.run({{ nodes: pending }});
    }}
    document.getElementById('btn-happy').classList.toggle('on', variant === 'happy');
    document.getElementById('btn-full').classList.toggle('on', variant === 'full');
    document.getElementById('toggle-note').innerHTML = variant === 'happy'
      ? 'the failure machinery is hidden &mdash; recovery Steps, their fan-out back into every stage, and the exhausted-retry edges'
      : 'every node and edge the analyser found, including all 11 recovery routes &mdash; this is the graph, unedited';
  }}
  document.getElementById('btn-happy').onclick = () => show('happy');
  document.getElementById('btn-full').onclick = () => show('full');
  show('happy');
</script>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default="/usr/local/bin/python3")
    parser.add_argument("--out", default=str(ROOT / "graph.html"))
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    graphs = []
    for name in FILES:
        graph = fdg(ROOT / name, args.python)
        status = "valid" if graph["valid"] else "INVALID"
        print(
            f"{status}: {graph['flow']['name']} — "
            f"{sum(1 for n in graph['nodes'] if n['kind'] == 'step')} steps, "
            f"{len(graph['nodes'])} nodes, {len(graph['edges'])} edges, "
            f"{len(graph['diagnostics'])} diagnostics",
            file=sys.stderr,
        )
        if not graph["valid"]:
            for diagnostic in graph["diagnostics"]:
                print(f"  {json.dumps(diagnostic)}", file=sys.stderr)
            return 1
        graphs.append(graph)

    out = Path(args.out)
    out.write_text(build(graphs, step_options()), encoding="utf-8")
    print(f"wrote {out}", file=sys.stderr)
    if not args.no_open:
        webbrowser.open(out.as_uri())
        print("opened in your browser", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
