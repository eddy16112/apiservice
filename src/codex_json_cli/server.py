from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple, Type

from .openai_compat import (
    DEFAULT_MODEL,
    codex_failure_to_error,
    to_chat_completion,
    to_chat_completion_chunk,
    to_error,
    to_model,
    to_model_list,
)
from .runner import CodexRequest, CodexRunnerError, ask_codex


MAX_BODY_BYTES = 1_000_000
DEFAULT_REQUEST_LOG = Path("logs") / "requests.jsonl"


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8081
    cwd: Path = Path(".")
    codex_bin: str = "codex"
    codex_model: Optional[str] = None
    profile: Optional[str] = None
    sandbox: Optional[str] = None
    timeout: Optional[float] = None
    extra_config: List[str] = field(default_factory=list)
    served_model: str = DEFAULT_MODEL
    api_key: Optional[str] = None
    request_log: Optional[Path] = DEFAULT_REQUEST_LOG
    session_prefix: str = "api"


@dataclass(frozen=True)
class ChatCompletionResult:
    status: HTTPStatus
    payload: Optional[Dict[str, object]] = None
    stream_content: Optional[str] = None
    stream_model: Optional[str] = None


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = config_from_args(args)
    serve_forever(config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-api",
        description="Serve an OpenAI-compatible HTTP API backed by Codex CLI.",
    )
    add_server_arguments(parser)
    return parser


def add_server_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8081, help="Port to bind.")
    parser.add_argument(
        "--cwd",
        default=".",
        help="Repository/workspace passed to Codex with --cd.",
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex CLI executable path. Defaults to 'codex'.",
    )
    parser.add_argument(
        "--codex-model",
        help="Optional model passed through to codex exec --model.",
    )
    parser.add_argument(
        "--served-model",
        default=DEFAULT_MODEL,
        help="Model id exposed via /v1/models and response JSON.",
    )
    parser.add_argument(
        "--profile",
        help="Optional Codex config profile passed through to codex exec.",
    )
    parser.add_argument(
        "--sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Optional Codex sandbox mode.",
    )
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        help="Extra Codex config override. Repeatable.",
    )
    parser.add_argument(
        "-c",
        dest="config",
        action="append",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="Abort Codex after this many seconds.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("CODEX_JSON_LLM_API_KEY"),
        help="Optional bearer token required for requests. Defaults to CODEX_JSON_LLM_API_KEY.",
    )
    parser.add_argument(
        "--request-log",
        default=os.environ.get("CODEX_JSON_LLM_REQUEST_LOG", str(DEFAULT_REQUEST_LOG)),
        help="JSONL file path for request logs. Defaults to logs/requests.jsonl. Use '-' to write to stdout.",
    )
    parser.add_argument(
        "--no-request-log",
        action="store_true",
        help="Disable request logging.",
    )
    parser.add_argument(
        "--session-prefix",
        default=os.environ.get("CODEX_JSON_LLM_SESSION_PREFIX", "api"),
        help="Prefix for session keys accepted from request metadata.",
    )


def config_from_args(args: argparse.Namespace) -> ServerConfig:
    return ServerConfig(
        host=args.host,
        port=args.port,
        cwd=Path(args.cwd),
        codex_bin=args.codex_bin,
        codex_model=args.codex_model,
        profile=args.profile,
        sandbox=args.sandbox,
        timeout=args.timeout,
        extra_config=args.config or [],
        served_model=args.served_model,
        api_key=args.api_key,
        request_log=None
        if getattr(args, "no_request_log", False)
        else Path(args.request_log)
        if args.request_log
        else None,
        session_prefix=args.session_prefix,
    )


def serve_forever(config: ServerConfig) -> None:
    reset_request_log(config.request_log)
    handler = make_handler(config)
    server = ThreadingHTTPServer((config.host, config.port), handler)
    url = f"http://{config.host}:{server.server_port}"
    print(f"Serving OpenAI-compatible Codex API at {url}", flush=True)
    print(f"Chat Completions: {url}/v1/chat/completions", flush=True)
    if config.request_log:
        print(f"Request log: {config.request_log}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
    finally:
        server.server_close()


def reset_request_log(request_log: Optional[Path]) -> None:
    if not request_log or str(request_log) == "-":
        return
    request_log.unlink(missing_ok=True)


def make_handler(
    config: ServerConfig,
    ask_func: Callable[[CodexRequest], object] = ask_codex,
) -> Type[BaseHTTPRequestHandler]:
    request_log_lock = threading.Lock()

    class OpenAICompatHandler(BaseHTTPRequestHandler):
        server_version = "CodexJsonLLM/0.1"

        def do_OPTIONS(self) -> None:
            started_at = time.monotonic()
            self._send_no_content()
            self._log_request(started_at, HTTPStatus.NO_CONTENT)

        def do_GET(self) -> None:
            started_at = time.monotonic()
            if self.path == "/health":
                status = HTTPStatus.OK
                self._send_json(status, {"status": "ok"})
                self._log_request(started_at, status)
                return
            if self.path == "/v1/models":
                status = HTTPStatus.OK
                self._send_json(status, to_model_list(config.served_model))
                self._log_request(started_at, status)
                return
            if self.path == f"/v1/models/{config.served_model}":
                status = HTTPStatus.OK
                self._send_json(status, to_model(config.served_model))
                self._log_request(started_at, status)
                return
            status = HTTPStatus.NOT_FOUND
            self._send_json(
                status,
                to_error("Not found", error_type="invalid_request_error", code="not_found"),
            )
            self._log_request(started_at, status)

        def do_POST(self) -> None:
            started_at = time.monotonic()
            if self.path != "/v1/chat/completions":
                status = HTTPStatus.NOT_FOUND
                self._send_json(
                    status,
                    to_error(
                        "Not found",
                        error_type="invalid_request_error",
                        code="not_found",
                    ),
                )
                self._log_request(started_at, status)
                return
            if not is_authorized(self.headers.get("Authorization"), config.api_key):
                status = HTTPStatus.UNAUTHORIZED
                self._send_json(
                    status,
                    to_error(
                        "Unauthorized",
                        error_type="authentication_error",
                        code="invalid_api_key",
                    ),
                    headers={"WWW-Authenticate": "Bearer"},
                )
                self._log_request(started_at, status)
                return

            body, error = self._read_json_body()
            if error:
                status = HTTPStatus.BAD_REQUEST
                self._send_json(status, error)
                self._log_request(started_at, status, body=body, error=error)
                return

            result = handle_chat_completion(body, config, ask_func=ask_func)
            if result.stream_content is not None:
                status = HTTPStatus.OK
                self._send_chat_completion_stream(
                    result.stream_content,
                    model=result.stream_model or config.served_model,
                )
                self._log_request(started_at, status, body=body)
                return

            self._send_json(result.status, result.payload or {})
            self._log_request(started_at, result.status, body=body)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json_body(self) -> Tuple[Dict[str, object], Optional[Dict[str, object]]]:
            length_header = self.headers.get("Content-Length")
            try:
                length = int(length_header or "0")
            except ValueError:
                return {}, to_error(
                    "Invalid Content-Length",
                    error_type="invalid_request_error",
                    code="invalid_request",
                )
            if length > MAX_BODY_BYTES:
                return {}, to_error(
                    "Request body is too large",
                    error_type="invalid_request_error",
                    code="request_too_large",
                )
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}, to_error(
                    "Request body must be valid JSON",
                    error_type="invalid_request_error",
                    code="invalid_json",
                )
            if not isinstance(body, dict):
                return {}, to_error(
                    "Request body must be a JSON object",
                    error_type="invalid_request_error",
                    code="invalid_request",
                )
            return body, None

        def _send_chat_completion_stream(self, content: str, model: str) -> None:
            completion_id, created, chunks = to_chat_completion_chunk(
                content=content,
                model=model,
            )
            self.send_response(HTTPStatus.OK)
            self._send_common_headers("text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(
                    f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                )
            self.wfile.write(b"data: [DONE]\n\n")

        def _send_json(
            self,
            status: HTTPStatus,
            payload: Dict[str, object],
            headers: Optional[Dict[str, str]] = None,
        ) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._send_common_headers("application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _send_no_content(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_common_headers("application/json")
            self.end_headers()

        def _send_common_headers(self, content_type: str) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Authorization, Content-Type",
            )
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _log_request(
            self,
            started_at: float,
            status: HTTPStatus,
            body: Optional[Dict[str, object]] = None,
            error: Optional[Dict[str, object]] = None,
        ) -> None:
            if not config.request_log:
                return
            record = build_request_log_record(
                method=self.command,
                path=self.path,
                headers=dict(self.headers.items()),
                client=self.client_address[0] if self.client_address else None,
                status=status,
                duration_seconds=time.monotonic() - started_at,
                body=body,
                error=error,
            )
            write_request_log(config.request_log, record, lock=request_log_lock)

    return OpenAICompatHandler


def build_request_log_record(
    method: str,
    path: str,
    headers: Dict[str, str],
    client: Optional[str],
    status: HTTPStatus,
    duration_seconds: float,
    body: Optional[Dict[str, object]] = None,
    error: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    record: Dict[str, object] = {
        "timestamp": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "method": method,
        "path": path,
        "client": client,
        "status": int(status),
        "duration_ms": round(duration_seconds * 1000, 3),
        "headers": redact_headers(headers),
    }
    if body is not None:
        record["body"] = body
    if error is not None:
        record["error"] = error
    return record


def write_request_log(
    request_log: Path,
    record: Dict[str, object],
    lock: Optional[threading.Lock] = None,
) -> None:
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    if str(request_log) == "-":
        print(line, flush=True)
        return

    def append() -> None:
        if request_log.parent != Path("."):
            request_log.parent.mkdir(parents=True, exist_ok=True)
        with request_log.open("a", encoding="utf-8") as file:
            file.write(line + "\n")

    if lock:
        with lock:
            append()
    else:
        append()


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in {"authorization", "proxy-authorization"}
    }


def handle_chat_completion(
    body: Dict[str, object],
    config: ServerConfig,
    ask_func: Callable[[CodexRequest], object] = ask_codex,
) -> ChatCompletionResult:
    try:
        prompt = messages_to_prompt(body.get("messages"))
    except ValueError as exc:
        return ChatCompletionResult(
            status=HTTPStatus.BAD_REQUEST,
            payload=to_error(
                str(exc),
                error_type="invalid_request_error",
                code="invalid_request",
                param="messages",
            ),
        )

    request = CodexRequest(
        question=prompt,
        cwd=config.cwd,
        codex_bin=config.codex_bin,
        model=config.codex_model,
        profile=config.profile,
        sandbox=config.sandbox,
        timeout=config.timeout,
        extra_config=config.extra_config,
        session_key=extract_session_key(body, config.session_prefix),
    )

    try:
        response = ask_func(request)
    except CodexRunnerError as exc:
        return ChatCompletionResult(
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            payload=to_error(str(exc), code="codex_cli_error"),
        )

    if not getattr(response, "ok", False):
        return ChatCompletionResult(
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            payload=codex_failure_to_error(response),
        )

    model = str(body.get("model") or config.served_model)
    if body.get("stream"):
        return ChatCompletionResult(
            status=HTTPStatus.OK,
            stream_content=response.answer,
            stream_model=model,
        )

    return ChatCompletionResult(
        status=HTTPStatus.OK,
        payload=to_chat_completion(response, model=model),
    )


def is_authorized(authorization_header: Optional[str], api_key: Optional[str]) -> bool:
    if not api_key:
        return True
    prefix = "Bearer "
    if not authorization_header or not authorization_header.startswith(prefix):
        return False
    return hmac.compare_digest(authorization_header[len(prefix) :], api_key)


def extract_session_key(body: Dict[str, object], prefix: str) -> Optional[str]:
    metadata = body.get("metadata")
    session_id: Optional[str] = None
    if isinstance(metadata, dict):
        value = metadata.get("session_id") or metadata.get("codex_session")
        if isinstance(value, str):
            session_id = value
    if session_id is None:
        value = body.get("session_id")
        if isinstance(value, str):
            session_id = value
    if not session_id:
        return None
    if ":" in session_id:
        return session_id
    return f"{prefix}:{session_id}"


def messages_to_prompt(messages: object) -> str:
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty array")

    rendered: List[Tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("each message must be an object")
        role = str(message.get("role") or "user")
        content = content_to_text(message.get("content")).strip()
        if content:
            rendered.append((role, content))

    if not rendered:
        raise ValueError("messages must contain text content")
    if len(rendered) == 1 and rendered[0][0] == "user":
        return rendered[0][1]

    lines = ["Conversation:"]
    for role, content in rendered:
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
