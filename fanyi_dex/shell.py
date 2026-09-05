"""Runs the translation project's own pipeline scripts unmodified."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from fanyi_dex.config import Config


class ScriptFailed(RuntimeError):
    def __init__(self, script: str, code: int, tail: str) -> None:
        super().__init__(f"{script} exited {code}\n{tail}")
        self.script = script
        self.code = code
        self.tail = tail


@dataclass(frozen=True)
class ScriptResult:
    code: int
    stdout: str
    stderr: str

    @property
    def tail(self) -> str:
        merged = (self.stdout + "\n" + self.stderr).strip().splitlines()
        return "\n".join(merged[-25:])


def _inside_this_venv(interpreter: str) -> bool:
    prefix = os.environ.get("VIRTUAL_ENV") or (
        sys.prefix if sys.prefix != sys.base_prefix else ""
    )
    if not prefix:
        return False
    try:
        return Path(interpreter).resolve().is_relative_to(Path(prefix).resolve())
    except (OSError, ValueError):
        return False


async def run_python(
    config: Config,
    script: Path,
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> ScriptResult:
    """Run a pipeline script with the interpreter that owns its dependencies.

    The pipeline scripts need the *system* environment: `harvest_beatplan.py`
    imports zhconv, `checks/print.py` imports pypdf. Neither is in this project's
    venv, and under `uv run` a bare `python3` resolves to the venv — which is how a
    verification run reached the print stage and died on `No module named 'pypdf'`.
    So the resolved interpreter is checked, and a venv one is refused by name rather
    than failing three stages later.
    """
    interpreter = os.environ.get("FANYI_PYTHON", "python3")
    resolved = shutil.which(interpreter) or interpreter
    if _inside_this_venv(resolved):
        raise ScriptFailed(
            script.name,
            126,
            f"refusing to run {script.name} with {resolved}: that is this project's "
            f"venv, which lacks the pipeline scripts' dependencies (zhconv, pypdf). "
            f"Set FANYI_PYTHON to a system interpreter.",
        )
    command = [interpreter, str(script), *args]
    printable = " ".join(command)

    if config.dry_run:
        print(f"[dry-run] {printable}", flush=True)
        return ScriptResult(0, f"[dry-run] {printable}", "")

    if not script.is_file():
        raise ScriptFailed(script.name, 127, f"script not found: {script}")

    print(f"[run] {printable}", flush=True)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    out, err = await process.communicate()
    result = ScriptResult(
        process.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )
    if result.stdout.strip():
        print(result.stdout.strip()[-4000:], flush=True)
    if check and result.code != 0:
        raise ScriptFailed(script.name, result.code, result.tail)
    return result
