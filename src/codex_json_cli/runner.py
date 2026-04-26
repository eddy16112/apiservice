from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import List, Optional


class CodexRunnerError(RuntimeError):
    """Raised when Codex CLI cannot be started."""


@dataclass(frozen=True)
class CodexRequest:
    question: str
    cwd: Path
    codex_bin: str = "codex"
    model: Optional[str] = None
    profile: Optional[str] = None
    sandbox: Optional[str] = None
    timeout: Optional[float] = None
    extra_config: Optional[List[str]] = None


@dataclass(frozen=True)
class CodexResponse:
    ok: bool
    question: str
    answer: str
    exit_code: int
    duration_seconds: float
    cwd: str
    command: List[str]
    stdout: str
    stderr: str


def ask_codex(request: CodexRequest) -> CodexResponse:
    cwd = request.cwd.expanduser().resolve()
    if not cwd.exists():
        raise CodexRunnerError(f"working directory does not exist: {cwd}")
    if not cwd.is_dir():
        raise CodexRunnerError(f"working directory is not a directory: {cwd}")

    start = time.monotonic()
    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="codex-answer-",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as output_file:
        answer_path = Path(output_file.name)

    command = _build_command(request, cwd, answer_path)
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=request.timeout,
            check=False,
            env=os.environ.copy(),
        )
        answer = _read_answer(answer_path, completed.stdout)
        duration = time.monotonic() - start
        return CodexResponse(
            ok=completed.returncode == 0,
            question=request.question,
            answer=answer,
            exit_code=completed.returncode,
            duration_seconds=duration,
            cwd=str(cwd),
            command=command,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except FileNotFoundError as exc:
        raise CodexRunnerError(
            f"could not find Codex CLI executable: {request.codex_bin}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr)
        return CodexResponse(
            ok=False,
            question=request.question,
            answer=_read_answer(answer_path, stdout),
            exit_code=124,
            duration_seconds=duration,
            cwd=str(cwd),
            command=command,
            stdout=stdout,
            stderr=stderr + f"\nTimed out after {request.timeout} seconds.",
        )
    finally:
        answer_path.unlink(missing_ok=True)


def _build_command(request: CodexRequest, cwd: Path, answer_path: Path) -> List[str]:
    command = [
        request.codex_bin,
        "exec",
        "--cd",
        str(cwd),
        "--output-last-message",
        str(answer_path),
        "--color",
        "never",
    ]

    if request.model:
        command.extend(["--model", request.model])
    if request.profile:
        command.extend(["--profile", request.profile])
    if request.sandbox:
        command.extend(["--sandbox", request.sandbox])
    for config in request.extra_config or []:
        command.extend(["--config", config])

    command.append(request.question)
    return command


def _read_answer(answer_path: Path, fallback_stdout: str) -> str:
    try:
        answer = answer_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        answer = ""
    return answer or fallback_stdout.strip()


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
