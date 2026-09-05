"""Calls headless Claude Code and parses one JSON object out of the reply.

The `PARSE` helper and the 3-attempt retry-with-nudge are ports of the same
logic inside the generated Workflow scripts, so a chapter that would have been
retried there is retried the same way here.

The prompt goes in on stdin rather than argv: a chapter prompt is tens of
kilobytes and argv has a size ceiling.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from fanyi_dex.config import Config
from fanyi_dex.fake_agent import reply_for


class AgentCallFailed(RuntimeError):
    """The CLI itself failed — non-zero exit, unparseable envelope, or is_error."""


@dataclass
class AgentReply:
    text: str
    cost_usd: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""


def parse_json_object(text: str | None) -> dict[str, Any] | None:
    """Port of the JS PARSE: strip a fenced block, then take the outermost braces."""
    if text is None:
        return None
    body = str(text)
    fence_start = body.find("```")
    if fence_start >= 0:
        after = body[fence_start + 3 :]
        if after[:4].lower() == "json":
            after = after[4:]
        fence_end = after.find("```")
        if fence_end >= 0:
            body = after[:fence_end]
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def build_argv(config: Config, effort: str | None = None) -> list[str]:
    argv = [
        config.claude_bin,
        "-p",
        "--output-format",
        "json",
        # Nobody is here to approve a tool call, and this stage needs none: the
        # prompt carries the source text and the reply is pure JSON. Dex does
        # every file write.
        "--allowedTools",
        "",
        "--max-turns",
        str(config.max_turns),
        "--model",
        config.model,
        "--effort",
        effort or config.effort,
    ]
    if config.bare:
        argv.append("--bare")
    return argv


async def call_agent(
    config: Config, prompt: str, *, label: str, effort: str | None = None
) -> AgentReply:
    """One headless invocation. Raises AgentCallFailed on transport-level trouble."""
    if config.dry_run or config.fake_agent:
        print(f"[fake-agent] claude -p <{len(prompt)} chars> ({label})", flush=True)
        return AgentReply(text=reply_for(prompt))

    argv = build_argv(config, effort)
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate(prompt.encode("utf-8"))
    stdout = out.decode("utf-8", "replace")
    stderr = err.decode("utf-8", "replace")

    if process.returncode != 0:
        raise AgentCallFailed(
            f"{label}: claude exited {process.returncode}\n{stderr.strip()[-2000:]}"
        )
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AgentCallFailed(
            f"{label}: could not parse the CLI envelope\n{stdout.strip()[-2000:]}"
        ) from exc
    if envelope.get("is_error"):
        raise AgentCallFailed(
            f"{label}: {envelope.get('subtype') or 'error'} — "
            f"{str(envelope.get('result'))[:800]}"
        )
    return AgentReply(
        text=str(envelope.get("result") or ""),
        cost_usd=float(envelope.get("total_cost_usd") or 0.0),
        usage=envelope.get("usage") or {},
        session_id=str(envelope.get("session_id") or ""),
    )


async def call_agent_for_json(
    config: Config,
    prompt: str,
    *,
    label: str,
    nudge: str,
    is_valid,
    effort: str | None = None,
) -> tuple[dict[str, Any], float]:
    """Call until the reply parses AND passes `is_valid`, up to parse_attempts.

    Returns the parsed object and the total cost. Raises AgentCallFailed when
    every attempt is used up, which lets the Step's failure route take over.
    """
    total_cost = 0.0
    problems: list[str] = []
    for attempt in range(1, config.parse_attempts + 1):
        suffix = nudge if attempt > 1 else ""
        tag = label if attempt == 1 else f"{label} r{attempt}"
        reply = await call_agent(config, prompt + suffix, label=tag, effort=effort)
        total_cost += reply.cost_usd
        parsed = parse_json_object(reply.text)
        if parsed is None:
            problems.append(f"attempt {attempt}: no JSON parsed")
        elif not is_valid(parsed):
            problems.append(f"attempt {attempt}: parsed but failed validation")
        else:
            return parsed, total_cost
        print(f"  {label} {problems[-1]}", flush=True)
    raise AgentCallFailed(f"{label}: {config.parse_attempts} attempts failed — " + "; ".join(problems))
