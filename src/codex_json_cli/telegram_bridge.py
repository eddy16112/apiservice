from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import time
from typing import Callable, Dict, Iterable, List, Optional, Union
from urllib import request
from urllib.error import HTTPError, URLError


DEFAULT_LLM_BASE_URL = "http://127.0.0.1:8081/v1"
DEFAULT_LLM_MODEL = "codex-cli"
DEFAULT_POLL_TIMEOUT = 30
DEFAULT_UPLOAD_DIR = Path("uploads") / "telegram"
MAX_TELEGRAM_TEXT_LENGTH = 4096
START_REPLY = "已连接。直接发送文字或图片给我就可以开始。发送 /reset 可以清空当前会话。"


@dataclass(frozen=True)
class TelegramBridgeConfig:
    bot_token: Optional[str] = None
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_model: str = DEFAULT_LLM_MODEL
    llm_api_key: Optional[str] = None
    allowed_users: List[int] = field(default_factory=list)
    allowed_chats: List[int] = field(default_factory=list)
    poll_timeout: int = DEFAULT_POLL_TIMEOUT
    poll_interval: float = 1.0
    drop_pending_updates: bool = False
    session_prefix: str = "telegram"
    upload_dir: Path = DEFAULT_UPLOAD_DIR


@dataclass(frozen=True)
class TelegramTextMessage:
    update_id: int
    message_id: int
    chat_id: int
    from_user_id: Optional[int]
    text: str
    chat_type: str
    username: Optional[str] = None
    first_name: Optional[str] = None


@dataclass(frozen=True)
class TelegramPhotoMessage:
    update_id: int
    message_id: int
    chat_id: int
    from_user_id: Optional[int]
    file_id: str
    file_unique_id: Optional[str]
    caption: str
    chat_type: str
    username: Optional[str] = None
    first_name: Optional[str] = None


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = config_from_args(args)
    if not config.bot_token:
        parser.error("--bot-token or TELEGRAM_BOT_TOKEN is required")
    poll_forever(config)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-telegram",
        description="Bridge Telegram Bot API messages to llm serve.",
    )
    add_bridge_arguments(parser)
    return parser


def add_bridge_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bot-token",
        default=os.environ.get("TELEGRAM_BOT_TOKEN"),
        help="Telegram bot token from BotFather. Defaults to TELEGRAM_BOT_TOKEN.",
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
        "--allowed-users",
        default=os.environ.get("TELEGRAM_ALLOWED_USERS", ""),
        help="Comma-separated Telegram user ids allowed to use the bridge.",
    )
    parser.add_argument(
        "--allowed-chats",
        default=os.environ.get("TELEGRAM_ALLOWED_CHATS", ""),
        help="Comma-separated Telegram chat ids allowed to use the bridge.",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=int(os.environ.get("TELEGRAM_POLL_TIMEOUT", DEFAULT_POLL_TIMEOUT)),
        help="Long polling timeout in seconds.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("TELEGRAM_POLL_INTERVAL", "1.0")),
        help="Sleep time after polling errors.",
    )
    parser.add_argument(
        "--drop-pending-updates",
        action="store_true",
        help="Ask Telegram to drop old queued updates on startup.",
    )
    parser.add_argument(
        "--session-prefix",
        default=os.environ.get("TELEGRAM_SESSION_PREFIX", "telegram"),
        help="Prefix for per-user session keys.",
    )
    parser.add_argument(
        "--upload-dir",
        default=os.environ.get("TELEGRAM_UPLOAD_DIR", str(DEFAULT_UPLOAD_DIR)),
        help="Directory for Telegram image downloads.",
    )


def config_from_args(args: argparse.Namespace) -> TelegramBridgeConfig:
    return TelegramBridgeConfig(
        bot_token=args.bot_token,
        llm_base_url=args.llm_base_url.rstrip("/"),
        llm_model=args.llm_model,
        llm_api_key=args.llm_api_key,
        allowed_users=parse_int_list(args.allowed_users),
        allowed_chats=parse_int_list(args.allowed_chats),
        poll_timeout=args.poll_timeout,
        poll_interval=args.poll_interval,
        drop_pending_updates=args.drop_pending_updates,
        session_prefix=args.session_prefix,
        upload_dir=Path(args.upload_dir),
    )


def poll_forever(config: TelegramBridgeConfig) -> None:
    if not config.bot_token:
        raise RuntimeError("missing Telegram bot token")
    print("Serving Telegram bridge with long polling", flush=True)
    print(f"Forwarding messages to {config.llm_base_url}/chat/completions", flush=True)
    if config.allowed_users:
        print(
            "Allowed Telegram users: "
            + ", ".join(str(user_id) for user_id in config.allowed_users),
            flush=True,
        )
    if config.allowed_chats:
        print(
            "Allowed Telegram chats: "
            + ", ".join(str(chat_id) for chat_id in config.allowed_chats),
            flush=True,
        )

    offset: Optional[int] = None
    webhook_deleted = False
    error_count = 0
    while True:
        try:
            if not webhook_deleted:
                delete_webhook(config)
                webhook_deleted = True
            updates = get_updates(config, offset=offset)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                message = extract_text_message(update)
                if message:
                    handle_text_message(message, config)
                    continue
                photo_message = extract_photo_message(update)
                if photo_message:
                    handle_photo_message(photo_message, config)
            error_count = 0
        except KeyboardInterrupt:
            print("\nShutting down.", flush=True)
            return
        except RuntimeError as exc:
            error_count += 1
            delay = min(max(config.poll_interval, 1.0) * (2 ** min(error_count - 1, 5)), 60)
            print(
                f"Telegram polling error ({error_count}); retrying in {delay:g}s: {exc}",
                flush=True,
            )
            time.sleep(delay)


def delete_webhook(config: TelegramBridgeConfig) -> Dict[str, object]:
    return call_telegram_api(
        config,
        "deleteWebhook",
        {"drop_pending_updates": config.drop_pending_updates},
    )


def get_updates(
    config: TelegramBridgeConfig,
    offset: Optional[int] = None,
) -> List[Dict[str, object]]:
    payload: Dict[str, object] = {
        "timeout": config.poll_timeout,
        "allowed_updates": ["message"],
    }
    if offset is not None:
        payload["offset"] = offset
    response = call_telegram_api(config, "getUpdates", payload)
    result = response.get("result")
    if not isinstance(result, list):
        raise RuntimeError(f"unexpected getUpdates response: {response}")
    return [item for item in result if isinstance(item, dict)]


def handle_text_message(
    message: TelegramTextMessage,
    config: TelegramBridgeConfig,
    llm_func: Optional[Callable[[str, TelegramBridgeConfig, str], str]] = None,
    send_func: Optional[
        Callable[[int, str, TelegramBridgeConfig], Dict[str, object]]
    ] = None,
) -> None:
    llm_func = llm_func or call_llm
    send_func = send_func or send_telegram_message
    if not is_allowed(message, config):
        print(
            f"Ignoring Telegram message from user {message.from_user_id} chat {message.chat_id}",
            flush=True,
        )
        return

    command = telegram_command(message.text)
    if command == "/start":
        send_func(message.chat_id, START_REPLY, config)
        return

    session_id = telegram_session_id(message, config)
    if command == "/reset":
        reset_session(session_id)
        send_func(message.chat_id, "Session reset.", config)
        return

    print(
        f"Telegram inbound chat {message.chat_id} user {message.from_user_id}: "
        f"{message.text[:120]}",
        flush=True,
    )
    try:
        reply = llm_func(message.text, config, session_id).strip()
    except RuntimeError as exc:
        reply = f"LLM request failed: {exc}"
    if not reply:
        reply = "I did not get a response from the LLM."
    for chunk in chunk_text(reply, MAX_TELEGRAM_TEXT_LENGTH):
        try:
            send_func(message.chat_id, chunk, config)
        except RuntimeError as exc:
            print(f"Failed to send Telegram reply to {message.chat_id}: {exc}", flush=True)
            return


def handle_photo_message(
    message: TelegramPhotoMessage,
    config: TelegramBridgeConfig,
    llm_func: Optional[Callable[[str, TelegramBridgeConfig, str], str]] = None,
    send_func: Optional[
        Callable[[int, str, TelegramBridgeConfig], Dict[str, object]]
    ] = None,
    download_func: Optional[
        Callable[[TelegramPhotoMessage, TelegramBridgeConfig], Path]
    ] = None,
) -> None:
    llm_func = llm_func or call_llm
    send_func = send_func or send_telegram_message
    download_func = download_func or download_telegram_photo
    if not is_allowed(message, config):
        print(
            f"Ignoring Telegram photo from user {message.from_user_id} chat {message.chat_id}",
            flush=True,
        )
        return

    session_id = telegram_session_id(message, config)
    print(
        f"Telegram inbound photo chat {message.chat_id} user {message.from_user_id}: "
        f"{message.file_id[:24]}",
        flush=True,
    )
    try:
        image_path = download_func(message, config)
        reply = llm_func(build_photo_prompt(image_path, message.caption), config, session_id).strip()
    except RuntimeError as exc:
        reply = f"Image request failed: {exc}"
    if not reply:
        reply = "I did not get a response from the LLM."
    for chunk in chunk_text(reply, MAX_TELEGRAM_TEXT_LENGTH):
        try:
            send_func(message.chat_id, chunk, config)
        except RuntimeError as exc:
            print(f"Failed to send Telegram reply to {message.chat_id}: {exc}", flush=True)
            return


def call_llm(prompt: str, config: TelegramBridgeConfig, session_id: str) -> str:
    payload = {
        "model": config.llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "metadata": {"session_id": session_id},
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


def send_telegram_message(
    chat_id: int,
    text: str,
    config: TelegramBridgeConfig,
) -> Dict[str, object]:
    return call_telegram_api(
        config,
        "sendMessage",
        build_send_message_payload(chat_id, text),
    )


def download_telegram_photo(
    message: TelegramPhotoMessage,
    config: TelegramBridgeConfig,
) -> Path:
    result = get_telegram_file(config, message.file_id)
    file_path = result.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        raise RuntimeError(f"Telegram getFile returned no file_path: {result}")
    data = get_bytes(f"https://api.telegram.org/file/bot{config.bot_token}/{file_path}")
    target = telegram_photo_target_path(message, file_path, config)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target.resolve()


def get_telegram_file(
    config: TelegramBridgeConfig,
    file_id: str,
) -> Dict[str, object]:
    response = call_telegram_api(config, "getFile", {"file_id": file_id})
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected getFile response: {response}")
    return result


def call_telegram_api(
    config: TelegramBridgeConfig,
    method: str,
    payload: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    if not config.bot_token:
        raise RuntimeError("missing Telegram bot token")
    response = post_json(
        f"https://api.telegram.org/bot{config.bot_token}/{method}",
        payload or {},
        headers={"Content-Type": "application/json"},
    )
    if response.get("ok") is not True:
        raise RuntimeError(f"Telegram API {method} failed: {response}")
    return response


def get_bytes(url: str) -> bytes:
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=120) as response:
            return response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GET {redact_url(url)} failed with HTTP {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"GET {redact_url(url)} failed: {exc}") from exc


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
        raise RuntimeError(
            f"POST {redact_url(url)} failed with HTTP {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"POST {redact_url(url)} failed: {exc}") from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"POST {redact_url(url)} returned non-JSON response"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"POST {redact_url(url)} returned non-object JSON")
    return parsed


def redact_url(url: str) -> str:
    for marker in ("api.telegram.org/file/bot", "api.telegram.org/bot"):
        index = url.find(marker)
        if index == -1:
            continue
        token_start = index + len(marker)
        slash = url.find("/", token_start)
        if slash == -1:
            return url[:token_start] + "<redacted>"
        return url[:token_start] + "<redacted>" + url[slash:]
    return url


def extract_text_message(update: object) -> Optional[TelegramTextMessage]:
    if not isinstance(update, dict):
        return None
    update_id = update.get("update_id")
    message = update.get("message")
    if not isinstance(update_id, int) or not isinstance(message, dict):
        return None

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    sender = message.get("from") or {}
    if isinstance(sender, dict) and sender.get("is_bot") is True:
        return None
    chat = message.get("chat") or {}
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return None

    from_user_id = sender.get("id") if isinstance(sender, dict) else None
    username = sender.get("username") if isinstance(sender, dict) else None
    first_name = sender.get("first_name") if isinstance(sender, dict) else None
    chat_type = chat.get("type")
    return TelegramTextMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        from_user_id=from_user_id if isinstance(from_user_id, int) else None,
        text=text.strip(),
        chat_type=chat_type if isinstance(chat_type, str) else "unknown",
        username=username if isinstance(username, str) else None,
        first_name=first_name if isinstance(first_name, str) else None,
    )


def extract_photo_message(update: object) -> Optional[TelegramPhotoMessage]:
    if not isinstance(update, dict):
        return None
    update_id = update.get("update_id")
    message = update.get("message")
    if not isinstance(update_id, int) or not isinstance(message, dict):
        return None

    sender = message.get("from") or {}
    if isinstance(sender, dict) and sender.get("is_bot") is True:
        return None
    chat = message.get("chat") or {}
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return None

    photo = choose_best_photo(message.get("photo"))
    if photo is None:
        return None
    file_id = photo.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None

    from_user_id = sender.get("id") if isinstance(sender, dict) else None
    username = sender.get("username") if isinstance(sender, dict) else None
    first_name = sender.get("first_name") if isinstance(sender, dict) else None
    chat_type = chat.get("type")
    file_unique_id = photo.get("file_unique_id")
    caption = message.get("caption")
    return TelegramPhotoMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        from_user_id=from_user_id if isinstance(from_user_id, int) else None,
        file_id=file_id,
        file_unique_id=file_unique_id if isinstance(file_unique_id, str) else None,
        caption=caption.strip() if isinstance(caption, str) else "",
        chat_type=chat_type if isinstance(chat_type, str) else "unknown",
        username=username if isinstance(username, str) else None,
        first_name=first_name if isinstance(first_name, str) else None,
    )


def choose_best_photo(photo_sizes: object) -> Optional[Dict[str, object]]:
    if not isinstance(photo_sizes, list):
        return None
    photos = [item for item in photo_sizes if isinstance(item, dict)]
    if not photos:
        return None
    return max(photos, key=photo_score)


def photo_score(photo: Dict[str, object]) -> int:
    file_size = photo.get("file_size")
    if isinstance(file_size, int):
        return file_size
    width = photo.get("width")
    height = photo.get("height")
    if isinstance(width, int) and isinstance(height, int):
        return width * height
    return 0


def is_allowed(
    message: Union[TelegramTextMessage, TelegramPhotoMessage],
    config: TelegramBridgeConfig,
) -> bool:
    if not config.allowed_users and not config.allowed_chats:
        return True
    if message.from_user_id in config.allowed_users:
        return True
    if message.chat_id in config.allowed_chats:
        return True
    return False


def telegram_session_id(
    message: Union[TelegramTextMessage, TelegramPhotoMessage],
    config: TelegramBridgeConfig,
) -> str:
    if message.from_user_id is not None:
        return f"{config.session_prefix}:user:{message.from_user_id}"
    return f"{config.session_prefix}:chat:{message.chat_id}"


def reset_session(session_id: str) -> None:
    from .session_store import SessionStore

    SessionStore().clear(session_id)


def build_send_message_payload(chat_id: int, text: str) -> Dict[str, object]:
    return {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }


def build_photo_prompt(image_path: Path, caption: str) -> str:
    lines = [
        "Telegram 用户上传了一张图片。",
        f"图片本地路径: {image_path}",
    ]
    if caption.strip():
        lines.extend(["用户说明:", caption.strip()])
    else:
        lines.append("用户没有附加文字说明。")
    lines.append("请根据这个图片文件和用户说明处理请求。")
    return "\n".join(lines)


def telegram_photo_target_path(
    message: TelegramPhotoMessage,
    telegram_file_path: str,
    config: TelegramBridgeConfig,
) -> Path:
    if message.from_user_id is not None:
        owner = f"user-{message.from_user_id}"
    else:
        owner = f"chat-{message.chat_id}"
    suffix = Path(telegram_file_path).suffix.lower()
    if not suffix or len(suffix) > 10:
        suffix = ".jpg"
    stem_source = message.file_unique_id or message.file_id
    stem = safe_path_component(stem_source)
    return config.upload_dir.expanduser() / owner / f"{message.message_id}-{stem}{suffix}"


def safe_path_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    normalized = normalized.strip(".-")
    return normalized[:80] or "file"


def chunk_text(text: str, max_length: int) -> Iterable[str]:
    if max_length <= 0:
        raise ValueError("max_length must be positive")
    for start in range(0, len(text), max_length):
        yield text[start : start + max_length]


def parse_int_list(value: str) -> List[int]:
    parsed: List[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        parsed.append(int(item))
    return parsed


def telegram_command(text: str) -> str:
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return first.split("@", 1)[0].lower()


if __name__ == "__main__":
    raise SystemExit(main())
