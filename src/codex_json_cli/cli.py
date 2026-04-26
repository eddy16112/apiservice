from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import List, Optional

from . import __version__
from .openai_compat import codex_failure_to_error, to_chat_completion, to_error
from .runner import CodexRequest, CodexRunnerError, ask_codex
from .server import add_server_arguments, config_from_args, serve_forever


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    if args.command == "ask":
        return _handle_ask(args)
    if args.command == "serve":
        return _handle_serve(args)

    parser.print_help(sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm",
        description="Forward questions to Codex CLI and print OpenAI-compatible JSON.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print this wrapper version and exit.",
    )

    subparsers = parser.add_subparsers(dest="command")
    ask = subparsers.add_parser(
        "ask",
        help="Send a question to Codex CLI.",
        description="Send a question to Codex CLI and wrap the final answer as OpenAI-compatible JSON.",
    )
    ask.add_argument(
        "question",
        nargs="*",
        help='Question to send. Use "-" to read the question from stdin.',
    )
    ask.add_argument(
        "--cwd",
        default=".",
        help="Repository/workspace passed to Codex with --cd. Defaults to the current directory.",
    )
    ask.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex CLI executable path. Defaults to 'codex'.",
    )
    ask.add_argument(
        "--model",
        help="Optional Codex model name passed through to codex exec.",
    )
    ask.add_argument(
        "--profile",
        help="Optional Codex config profile passed through to codex exec.",
    )
    ask.add_argument(
        "--sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Optional Codex sandbox mode.",
    )
    ask.add_argument(
        "--config",
        action="append",
        default=[],
        help="Extra Codex config override, e.g. -c key=value. Repeatable.",
    )
    ask.add_argument(
        "-c",
        dest="config",
        action="append",
        help=argparse.SUPPRESS,
    )
    ask.add_argument(
        "--timeout",
        type=float,
        help="Abort Codex after this many seconds.",
    )
    ask.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    serve = subparsers.add_parser(
        "serve",
        help="Run an OpenAI-compatible HTTP API backed by Codex CLI.",
        description="Run an OpenAI-compatible HTTP API backed by Codex CLI.",
    )
    add_server_arguments(serve)
    return parser


def _handle_ask(args: argparse.Namespace) -> int:
    question = ""
    try:
        question = _read_question(args.question)
        request = CodexRequest(
            question=question,
            cwd=Path(args.cwd),
            codex_bin=args.codex_bin,
            model=args.model,
            profile=args.profile,
            sandbox=args.sandbox,
            timeout=args.timeout,
            extra_config=args.config or [],
        )
        response = ask_codex(request)
        if response.ok:
            payload = to_chat_completion(response, model=args.model)
            exit_code = 0
        else:
            payload = codex_failure_to_error(response)
            exit_code = 1
        _print_json(payload, pretty=args.pretty)
        return exit_code
    except (CodexRunnerError, ValueError) as exc:
        _print_json(
            to_error(
                message=str(exc),
                error_type="invalid_request_error",
                code="invalid_request",
                param="question" if not question else None,
            ),
            pretty=args.pretty,
        )
        return 2


def _read_question(parts: List[str]) -> str:
    if parts == ["-"]:
        question = sys.stdin.read()
    else:
        question = " ".join(parts)

    question = question.strip()
    if not question:
        raise ValueError('missing question; try: llm ask "your question"')
    return question


def _print_json(payload: object, pretty: bool) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if pretty else None,
        )
    )


def _handle_serve(args: argparse.Namespace) -> int:
    serve_forever(config_from_args(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
