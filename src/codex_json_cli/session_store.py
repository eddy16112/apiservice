from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tempfile
from typing import Dict, Optional


DEFAULT_SESSION_STORE = Path("session") / "sessions.json"


@dataclass(frozen=True)
class SessionInfo:
    key: str
    codex_session_id: Optional[str] = None


class SessionStore:
    def __init__(self, path: Path = DEFAULT_SESSION_STORE):
        self.path = path

    def get(self, key: str) -> Optional[SessionInfo]:
        normalized_key = normalize_session_key(key)
        data = self._read()
        sessions = data.setdefault("sessions", {})
        existing = sessions.get(normalized_key)
        if isinstance(existing, dict):
            codex_session_id = existing.get("codex_session_id")
            if not isinstance(codex_session_id, str):
                codex_session_id = existing.get("codex_session")
            if isinstance(codex_session_id, str) and codex_session_id:
                return SessionInfo(
                    key=normalized_key,
                    codex_session_id=codex_session_id,
                )
        return None

    def set_codex_session(self, key: str, codex_session_id: str) -> SessionInfo:
        normalized_key = normalize_session_key(key)
        codex_session_id = codex_session_id.strip()
        if not codex_session_id:
            raise ValueError("codex session id cannot be empty")
        data = self._read()
        sessions = data.setdefault("sessions", {})
        sessions[normalized_key] = {"codex_session_id": codex_session_id}
        self._write(data)
        return SessionInfo(
            key=normalized_key,
            codex_session_id=codex_session_id,
        )

    def get_or_create(self, key: str) -> SessionInfo:
        existing = self.get(key)
        if existing:
            return SessionInfo(
                key=existing.key,
                codex_session_id=existing.codex_session_id,
            )
        return SessionInfo(key=normalize_session_key(key))

    def clear(self, key: str) -> None:
        normalized_key = normalize_session_key(key)
        data = self._read()
        sessions = data.setdefault("sessions", {})
        if normalized_key in sessions:
            del sessions[normalized_key]
            self._write(data)

    def _read(self) -> Dict[str, object]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"sessions": {}}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"sessions": {}}
        if not isinstance(data, dict):
            return {"sessions": {}}
        if not isinstance(data.get("sessions"), dict):
            data["sessions"] = {}
        return data

    def _write(self, data: Dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        ) as file:
            json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            tmp_path = Path(file.name)
        tmp_path.replace(self.path)


def normalize_session_key(key: str) -> str:
    key = key.strip()
    if not key:
        raise ValueError("session key cannot be empty")
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", key)
    normalized = normalized.strip("-")
    if not normalized:
        raise ValueError("session key cannot be empty")
    return normalized[:120]


def resolve_codex_session(
    session_key: Optional[str],
    session_store_path: Optional[Path] = None,
) -> Optional[str]:
    if not session_key:
        return None
    store = SessionStore(session_store_path or DEFAULT_SESSION_STORE)
    info = store.get(session_key)
    return info.codex_session_id if info else None
