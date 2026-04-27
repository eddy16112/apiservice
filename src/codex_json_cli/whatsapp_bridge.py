from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Type
from urllib import parse, request
from urllib.error import HTTPError, URLError


DEFAULT_LLM_BASE_URL = "http://127.0.0.1:8081/v1"
DEFAULT_LLM_MODEL = "codex-cli"
DEFAULT_GRAPH_API_VERSION = "v24.0"
DEFAULT_WEBHOOK_PATH = "/webhooks/whatsapp"
MAX_WHATSAPP_TEXT_LENGTH = 4096


@dataclass(frozen=True)
class WhatsAppBridgeConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    webhook_path: str = DEFAULT_WEBHOOK_PATH
    verify_token: Optional[str] = None
    app_secret: Optional[str] = None
    access_token: Optional[str] = None
    phone_number_id: Optional[str] = None
    graph_api_version: str = DEFAULT_GRAPH_API_VERSION
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_model: str = DEFAULT_LLM_MODEL
    llm_api_key: Optional[str] = None
    allowed_from: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class WhatsAppTextMessage:
    message_id: str
    from_number: str
    text: str
    phone_number_id: Optional[str] = None


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = config_from_args(args)
    if not config.verify_token:
        parser.error("--verify-token or WHATSAPP_VERIFY_TOKEN is required")
    serve_forever(config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-whatsapp",
        description="Bridge WhatsApp Cloud API webhooks to llm serve.",
    )
    add_bridge_arguments(parser)
    return parser


def add_bridge_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind.")
    parser.add_argument(
        "--webhook-path",
        default=DEFAULT_WEBHOOK_PATH,
        help="Webhook path for Meta callbacks.",
    )
    parser.add_argument(
        "--verify-token",
        default=os.environ.get("WHATSAPP_VERIFY_TOKEN"),
        help="Meta webhook verify token. Defaults to WHATSAPP_VERIFY_TOKEN.",
    )
    parser.add_argument(
        "--app-secret",
        default=os.environ.get("WHATSAPP_APP_SECRET"),
        help="Optional Meta app secret for X-Hub-Signature-256 verification.",
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("WHATSAPP_ACCESS_TOKEN"),
        help="WhatsApp Cloud API access token. Defaults to WHATSAPP_ACCESS_TOKEN.",
    )
    parser.add_argument(
        "--phone-number-id",
        default=os.environ.get("WHATSAPP_PHONE_NUMBER_ID"),
        help="WhatsApp Business phone number id. Defaults to WHATSAPP_PHONE_NUMBER_ID.",
    )
    parser.add_argument(
        "--graph-api-version",
        default=os.environ.get("WHATSAPP_GRAPH_API_VERSION", DEFAULT_GRAPH_API_VERSION),
        help="Meta Graph API version.",
    )
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("CODEX_JSON_LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
        help="OpenAI-compatible base URL for llm serve.",
    )
    parser.add_argument(
        "--llm-model",
        default=os.environ.get("CODEX_JSON_LLM_MODEL", DEFAULT_LLM_MODEL),
        help="Model id sent to llm serve.",
    )
    parser.add_argument(
        "--llm-api-key",
        default=os.environ.get("CODEX_JSON_LLM_API_KEY"),
        help="Optional bearer token for llm serve.",
    )
    parser.add_argument(
        "--allowed-from",
        default=os.environ.get("WHATSAPP_ALLOWED_FROM", ""),
        help="Comma-separated WhatsApp sender numbers allowed to use the bridge.",
    )


def config_from_args(args: argparse.Namespace) -> WhatsAppBridgeConfig:
    return WhatsAppBridgeConfig(
        host=args.host,
        port=args.port,
        webhook_path=normalize_path(args.webhook_path),
        verify_token=args.verify_token,
        app_secret=args.app_secret,
        access_token=args.access_token,
        phone_number_id=args.phone_number_id,
        graph_api_version=args.graph_api_version,
        llm_base_url=args.llm_base_url.rstrip("/"),
        llm_model=args.llm_model,
        llm_api_key=args.llm_api_key,
        allowed_from=parse_allowed_numbers(args.allowed_from),
    )


def serve_forever(config: WhatsAppBridgeConfig) -> None:
    handler = make_handler(config)
    server = ThreadingHTTPServer((config.host, config.port), handler)
    url = f"http://{config.host}:{server.server_port}"
    print(f"Serving WhatsApp bridge at {url}{config.webhook_path}", flush=True)
    print(f"Forwarding messages to {config.llm_base_url}/chat/completions", flush=True)
    if config.allowed_from:
        print(f"Allowed WhatsApp senders: {', '.join(config.allowed_from)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
    finally:
        server.server_close()


def make_handler(config: WhatsAppBridgeConfig) -> Type[BaseHTTPRequestHandler]:
    seen_message_ids = set()

    class WhatsAppBridgeHandler(BaseHTTPRequestHandler):
        server_version = "CodexWhatsAppBridge/0.1"

        def do_GET(self) -> None:
            parsed = parse.urlparse(self.path)
            if parsed.path not in valid_webhook_paths(config.webhook_path):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return

            query = parse.parse_qs(parsed.query)
            challenge = verify_meta_challenge(
                query=query,
                expected_token=config.verify_token or "",
            )
            if challenge is None:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid_verify_token"})
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(challenge.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(challenge.encode("utf-8"))

        def do_POST(self) -> None:
            parsed = parse.urlparse(self.path)
            if parsed.path not in valid_webhook_paths(config.webhook_path):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return

            raw, error = self._read_body()
            if error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": error})
                return

            if config.app_secret and not is_valid_meta_signature(
                raw_body=raw,
                signature_header=self.headers.get("X-Hub-Signature-256"),
                app_secret=config.app_secret,
            ):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid_signature"})
                return

            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return

            messages = [
                message
                for message in extract_text_messages(payload)
                if message.message_id not in seen_message_ids
            ]
            for message in messages:
                seen_message_ids.add(message.message_id)
                self.server_thread(
                    target=handle_text_message,
                    args=(message, config),
                )

            self._send_json(
                HTTPStatus.OK,
                {"status": "accepted", "messages": len(messages)},
            )

        def log_message(self, format: str, *args: object) -> None:
            return

        def server_thread(self, target: Callable[..., None], args: Tuple[object, ...]) -> None:
            import threading

            thread = threading.Thread(target=target, args=args, daemon=True)
            thread.start()

        def _read_body(self) -> Tuple[bytes, Optional[str]]:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                return b"", "invalid_content_length"
            return self.rfile.read(length), None

        def _send_json(self, status: HTTPStatus, payload: Dict[str, object]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return WhatsAppBridgeHandler


def handle_text_message(
    message: WhatsAppTextMessage,
    config: WhatsAppBridgeConfig,
    llm_func: Optional[Callable[[str, WhatsAppBridgeConfig], str]] = None,
    send_func: Optional[
        Callable[[str, str, WhatsAppBridgeConfig], Dict[str, object]]
    ] = None,
) -> None:
    llm_func = llm_func or call_llm
    send_func = send_func or send_whatsapp_text
    if config.allowed_from and message.from_number not in config.allowed_from:
        print(f"Ignoring WhatsApp message from non-allowed sender {message.from_number}", flush=True)
        return
    print(f"WhatsApp inbound from {message.from_number}: {message.text[:120]}", flush=True)
    try:
        reply = llm_func(message.text, config).strip()
    except RuntimeError as exc:
        reply = f"LLM request failed: {exc}"
    if not reply:
        reply = "I did not get a response from the LLM."
    for chunk in chunk_text(reply, MAX_WHATSAPP_TEXT_LENGTH):
        try:
            send_func(message.from_number, chunk, config)
        except RuntimeError as exc:
            print(f"Failed to send WhatsApp reply to {message.from_number}: {exc}", flush=True)
            return


def call_llm(prompt: str, config: WhatsAppBridgeConfig) -> str:
    payload = {
        "model": config.llm_model,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"Content-Type": "application/json"}
    if config.llm_api_key:
        headers["Authorization"] = f"Bearer {config.llm_api_key}"

    response = post_json(
        f"{config.llm_base_url.rstrip('/')}/chat/completions",
        payload,
        headers=headers,
    )
    try:
        return str(response["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected LLM response: {response}") from exc


def send_whatsapp_text(
    to_number: str,
    text: str,
    config: WhatsAppBridgeConfig,
) -> Dict[str, object]:
    if not config.access_token:
        raise RuntimeError("missing WhatsApp access token")
    if not config.phone_number_id:
        raise RuntimeError("missing WhatsApp phone number id")

    url = (
        f"https://graph.facebook.com/{config.graph_api_version}/"
        f"{config.phone_number_id}/messages"
    )
    return post_json(
        url,
        build_whatsapp_text_payload(to_number, text),
        headers={
            "Authorization": f"Bearer {config.access_token}",
            "Content-Type": "application/json",
        },
    )


def post_json(
    url: str,
    payload: Dict[str, object],
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, object]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with request.urlopen(req, timeout=120) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"POST {url} failed: {exc}") from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"POST {url} returned non-JSON response") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"POST {url} returned non-object JSON")
    return parsed


def verify_meta_challenge(
    query: Dict[str, List[str]],
    expected_token: str,
) -> Optional[str]:
    mode = first_query_value(query, "hub.mode")
    token = first_query_value(query, "hub.verify_token")
    challenge = first_query_value(query, "hub.challenge")
    if mode == "subscribe" and token == expected_token and challenge is not None:
        return challenge
    return None


def is_valid_meta_signature(
    raw_body: bytes,
    signature_header: Optional[str],
    app_secret: str,
) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature_header, f"sha256={expected}")


def extract_text_messages(payload: object) -> List[WhatsAppTextMessage]:
    if not isinstance(payload, dict):
        return []
    messages: List[WhatsAppTextMessage] = []
    for entry in payload.get("entry", []):
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []):
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata") or {}
            phone_number_id = (
                metadata.get("phone_number_id") if isinstance(metadata, dict) else None
            )
            for message in value.get("messages", []):
                if not isinstance(message, dict):
                    continue
                if message.get("type") != "text":
                    continue
                text = message.get("text")
                if not isinstance(text, dict) or not isinstance(text.get("body"), str):
                    continue
                from_number = message.get("from")
                message_id = message.get("id")
                if not isinstance(from_number, str) or not isinstance(message_id, str):
                    continue
                messages.append(
                    WhatsAppTextMessage(
                        message_id=message_id,
                        from_number=from_number,
                        text=text["body"],
                        phone_number_id=phone_number_id
                        if isinstance(phone_number_id, str)
                        else None,
                    )
                )
    return messages


def build_whatsapp_text_payload(to_number: str, text: str) -> Dict[str, object]:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text,
        },
    }


def chunk_text(text: str, max_length: int) -> Iterable[str]:
    if max_length <= 0:
        raise ValueError("max_length must be positive")
    for start in range(0, len(text), max_length):
        yield text[start : start + max_length]


def parse_allowed_numbers(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def first_query_value(query: Dict[str, List[str]], key: str) -> Optional[str]:
    value = query.get(key)
    if not value:
        return None
    return value[0]


def valid_webhook_paths(configured_path: str) -> List[str]:
    path = normalize_path(configured_path)
    paths = {path, "/webhook", "/webhooks/whatsapp", "/whatsapp"}
    return sorted(paths)


def normalize_path(path: str) -> str:
    if not path:
        return DEFAULT_WEBHOOK_PATH
    return path if path.startswith("/") else f"/{path}"


if __name__ == "__main__":
    raise SystemExit(main())
