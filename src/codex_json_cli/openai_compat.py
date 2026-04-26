from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional, Tuple

from .runner import CodexResponse


DEFAULT_MODEL = "codex-cli"


def to_chat_completion(
    response: CodexResponse,
    model: Optional[str] = None,
    completion_id: Optional[str] = None,
    created: Optional[int] = None,
) -> Dict[str, object]:
    return {
        "id": completion_id or _completion_id(),
        "object": "chat.completion",
        "created": created if created is not None else int(time.time()),
        "model": model or DEFAULT_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response.answer,
                    "refusal": None,
                    "annotations": [],
                },
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "system_fingerprint": None,
    }


def to_chat_completion_chunk(
    content: str,
    model: Optional[str] = None,
    completion_id: Optional[str] = None,
    created: Optional[int] = None,
) -> Tuple[str, int, List[Dict[str, object]]]:
    chunk_id = completion_id or _completion_id()
    chunk_created = created if created is not None else int(time.time())
    chunk_model = model or DEFAULT_MODEL
    return (
        chunk_id,
        chunk_created,
        [
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": chunk_created,
                "model": chunk_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "logprobs": None,
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": chunk_created,
                "model": chunk_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content},
                        "logprobs": None,
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": chunk_created,
                "model": chunk_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "logprobs": None,
                        "finish_reason": "stop",
                    }
                ],
            },
        ],
    )


def to_model(model_id: str) -> Dict[str, object]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "codex-json-llm",
    }


def to_model_list(model_id: str) -> Dict[str, object]:
    return {
        "object": "list",
        "data": [to_model(model_id)],
    }


def to_error(
    message: str,
    error_type: str = "server_error",
    code: Optional[str] = None,
    param: Optional[str] = None,
) -> Dict[str, object]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


def codex_failure_to_error(response: CodexResponse) -> Dict[str, object]:
    detail = response.stderr.strip() or response.stdout.strip()
    message = f"Codex CLI failed with exit code {response.exit_code}."
    if detail:
        message = f"{message} {detail}"
    code = "timeout" if response.exit_code == 124 else "codex_cli_error"
    return to_error(message=message, code=code)


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"
