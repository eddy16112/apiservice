from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import json
import sqlite3
import subprocess
import tempfile
import time
from typing import List, Optional

from .session_store import DEFAULT_SESSION_STORE, SessionStore


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
    session_key: Optional[str] = None
    session_store_path: Optional[Path] = None


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
    session_key: Optional[str] = None
    session_id: Optional[str] = None


def ask_codex(request: CodexRequest) -> CodexResponse:
    cwd = request.cwd.expanduser().resolve()
    if not cwd.exists():
        raise CodexRunnerError(f"working directory does not exist: {cwd}")
    if not cwd.is_dir():
        raise CodexRunnerError(f"working directory is not a directory: {cwd}")

    start = time.monotonic()
    started_at_ms = int(time.time() * 1000)
    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="codex-answer-",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as output_file:
        answer_path = Path(output_file.name)

    store = SessionStore(request.session_store_path or cwd / DEFAULT_SESSION_STORE)
    stored_info = store.get(request.session_key) if request.session_key else None
    stored_session = stored_info.codex_session_id if stored_info else None
    command = _build_command(request, cwd, answer_path, stored_session)
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
        session_id = _extract_session_id(completed.stdout) or stored_session
        if request.session_key and not session_id:
            session_id = _discover_codex_session_id(
                cwd=cwd,
                question=request.question,
                started_at_ms=started_at_ms,
            )
        if request.session_key and session_id:
            store.set_codex_session(request.session_key, session_id)
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
            session_key=request.session_key,
            session_id=session_id,
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
            session_key=request.session_key,
            session_id=stored_session,
        )
    finally:
        answer_path.unlink(missing_ok=True)


def _build_command(
    request: CodexRequest,
    cwd: Path,
    answer_path: Path,
    stored_session: Optional[str],
) -> List[str]:
    if stored_session:
        command = [
            request.codex_bin,
            "exec",
            "resume",
            "--output-last-message",
            str(answer_path),
            "--json",
        ]
        if request.model:
            command.extend(["--model", request.model])
        for config in request.extra_config or []:
            command.extend(["--config", config])
        command.extend([stored_session, request.question])
        return command

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

    if request.session_key:
        command.append("--json")
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


def _extract_session_id(stdout: str) -> Optional[str]:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        direct = event.get("session_id") or event.get("id")
        if isinstance(direct, str) and _looks_like_session_id(direct):
            return direct
        payload = event.get("payload")
        if isinstance(payload, dict):
            value = payload.get("id") or payload.get("session_id")
            if isinstance(value, str) and _looks_like_session_id(value):
                return value
    return None


def _looks_like_session_id(value: str) -> bool:
    return bool(value.strip()) and len(value.strip()) >= 8


def _discover_codex_session_id(
    cwd: Path,
    question: str,
    started_at_ms: int,
) -> Optional[str]:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return _discover_codex_session_from_state(codex_home, cwd, question, started_at_ms)


def _discover_codex_session_from_state(
    codex_home: Path,
    cwd: Path,
    question: str,
    started_at_ms: int,
) -> Optional[str]:
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        return None
    threshold_ms = max(0, started_at_ms - 5_000)
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(
            """
            SELECT id, first_user_message
            FROM threads
            WHERE cwd = ?
              AND COALESCE(created_at_ms, created_at * 1000) >= ?
            ORDER BY COALESCE(created_at_ms, created_at * 1000) DESC
            LIMIT 10
            """,
            (str(cwd), threshold_ms),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()

    for session_id, first_user_message in rows:
        if (
            isinstance(session_id, str)
            and first_user_message == question
            and _looks_like_session_id(session_id)
        ):
            return session_id
    if len(rows) == 1:
        session_id = rows[0][0]
        if isinstance(session_id, str) and _looks_like_session_id(session_id):
            return session_id
    return None
