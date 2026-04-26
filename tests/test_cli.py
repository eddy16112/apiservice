import json
import sys
import tempfile
import unittest
from pathlib import Path
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_json_cli.cli import main
from codex_json_cli.openai_compat import to_chat_completion, to_chat_completion_chunk, to_model_list
from codex_json_cli.runner import CodexRequest, CodexResponse, ask_codex
from codex_json_cli.server import (
    ServerConfig,
    build_request_log_record,
    handle_chat_completion,
    is_authorized,
    messages_to_prompt,
    write_request_log,
)


class RunnerTests(unittest.TestCase):
    def test_ask_codex_uses_output_file_for_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)

            def fake_run(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text("final answer\n", encoding="utf-8")

                class Completed:
                    returncode = 0
                    stdout = "event stream"
                    stderr = ""

                return Completed()

            request = CodexRequest(question="hello", cwd=cwd, codex_bin="codex")
            with patch("codex_json_cli.runner.subprocess.run", side_effect=fake_run):
                response = ask_codex(request)

            self.assertTrue(response.ok)
            self.assertEqual(response.answer, "final answer")
            self.assertEqual(response.command[:2], ["codex", "exec"])
            self.assertIn("--cd", response.command)
            self.assertIn(str(cwd.resolve()), response.command)

    def test_ask_codex_returns_jsonable_timeout_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = CodexRequest(
                question="slow",
                cwd=Path(tmp),
                codex_bin="codex",
                timeout=1,
            )
            timeout = subprocess.TimeoutExpired(
                cmd=["codex"],
                timeout=1,
                output=b"partial stdout",
                stderr=b"partial stderr",
            )

            with patch("codex_json_cli.runner.subprocess.run", side_effect=timeout):
                response = ask_codex(request)

            self.assertFalse(response.ok)
            self.assertEqual(response.exit_code, 124)
            self.assertEqual(response.answer, "partial stdout")
            self.assertIn("Timed out after 1 seconds", response.stderr)


class CliTests(unittest.TestCase):
    def test_ask_prints_openai_chat_completion_json_and_returns_zero(self):
        response = CodexResponse(
            ok=True,
            question="hello",
            answer="world",
            exit_code=0,
            duration_seconds=1.0,
            cwd="/tmp",
            command=["codex"],
            stdout="",
            stderr="",
        )

        with patch("codex_json_cli.cli.ask_codex", return_value=response):
            with patch("builtins.print") as print_mock:
                exit_code = main(["ask", "hello"])

        self.assertEqual(exit_code, 0)
        printed = print_mock.call_args.args[0]
        payload = json.loads(printed)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["role"], "assistant")
        self.assertEqual(payload["choices"][0]["message"]["content"], "world")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertEqual(payload["usage"]["total_tokens"], 0)
        self.assertNotIn("answer", payload)

    def test_missing_question_returns_usage_error_json(self):
        with patch("builtins.print") as print_mock:
            exit_code = main(["ask"])

        self.assertEqual(exit_code, 2)
        payload = json.loads(print_mock.call_args.args[0])
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertEqual(payload["error"]["param"], "question")
        self.assertIn("missing question", payload["error"]["message"])

    def test_codex_failure_returns_openai_error_json(self):
        response = CodexResponse(
            ok=False,
            question="hello",
            answer="",
            exit_code=1,
            duration_seconds=1.0,
            cwd="/tmp",
            command=["codex"],
            stdout="",
            stderr="not logged in",
        )

        with patch("codex_json_cli.cli.ask_codex", return_value=response):
            with patch("builtins.print") as print_mock:
                exit_code = main(["ask", "hello"])

        self.assertEqual(exit_code, 1)
        payload = json.loads(print_mock.call_args.args[0])
        self.assertEqual(payload["error"]["type"], "server_error")
        self.assertEqual(payload["error"]["code"], "codex_cli_error")
        self.assertIn("not logged in", payload["error"]["message"])


class OpenAICompatTests(unittest.TestCase):
    def test_to_chat_completion_matches_expected_shape(self):
        response = CodexResponse(
            ok=True,
            question="hello",
            answer="world",
            exit_code=0,
            duration_seconds=1.0,
            cwd="/tmp",
            command=["codex"],
            stdout="",
            stderr="",
        )

        payload = to_chat_completion(
            response,
            model="gpt-test",
            completion_id="chatcmpl-test",
            created=123,
        )

        self.assertEqual(payload["id"], "chatcmpl-test")
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["created"], 123)
        self.assertEqual(payload["model"], "gpt-test")
        self.assertEqual(payload["choices"][0]["message"]["content"], "world")
        self.assertEqual(payload["choices"][0]["message"]["refusal"], None)
        self.assertEqual(payload["choices"][0]["message"]["annotations"], [])


class ServerTests(unittest.TestCase):
    def test_chat_completion_handler_returns_openai_json(self):
        seen_prompts = []

        def fake_ask(request):
            seen_prompts.append(request.question)
            return _response(answer="api answer")

        config = ServerConfig(served_model="codex-test")
        result = handle_chat_completion(
            {
                "model": "codex-test",
                "messages": [
                    {"role": "system", "content": "Be concise."},
                    {"role": "user", "content": "Hello"},
                ],
            },
            config,
            ask_func=fake_ask,
        )

        self.assertEqual(result.status, 200)
        self.assertEqual(result.payload["object"], "chat.completion")
        self.assertEqual(result.payload["model"], "codex-test")
        self.assertEqual(result.payload["choices"][0]["message"]["content"], "api answer")
        self.assertEqual(seen_prompts[0], "Conversation:\nsystem: Be concise.\nuser: Hello")

    def test_model_list_matches_openai_shape(self):
        body = to_model_list("codex-test")

        self.assertEqual(body["object"], "list")
        self.assertEqual(body["data"][0]["id"], "codex-test")

    def test_auth_checks_bearer_token(self):
        self.assertTrue(is_authorized(None, None))
        self.assertFalse(is_authorized(None, "secret"))
        self.assertFalse(is_authorized("Bearer wrong", "secret"))
        self.assertTrue(is_authorized("Bearer secret", "secret"))

    def test_streaming_result_can_be_encoded_as_sse_chunks(self):
        config = ServerConfig()
        result = handle_chat_completion(
            {
                "stream": True,
                "messages": [{"role": "user", "content": "Hello"}],
            },
            config,
            ask_func=lambda request: _response(answer="stream answer"),
        )
        _chunk_id, _created, chunks = to_chat_completion_chunk(
            result.stream_content,
            result.stream_model,
            completion_id="chatcmpl-test",
            created=123,
        )

        self.assertEqual(result.status, 200)
        self.assertEqual(chunks[0]["object"], "chat.completion.chunk")
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "stream answer")
        self.assertEqual(chunks[2]["choices"][0]["finish_reason"], "stop")

    def test_messages_to_prompt_accepts_content_parts(self):
        prompt = messages_to_prompt(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "text", "text": "second"},
                    ],
                }
            ]
        )

        self.assertEqual(prompt, "first\nsecond")

    def test_request_log_redacts_auth_and_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "requests.jsonl"
            record = build_request_log_record(
                method="POST",
                path="/v1/chat/completions",
                headers={
                    "Authorization": "Bearer secret",
                    "Content-Type": "application/json",
                },
                client="127.0.0.1",
                status=200,
                duration_seconds=0.1234,
                body={"model": "codex-cli"},
            )
            write_request_log(log_path, record)

            lines = log_path.read_text(encoding="utf-8").splitlines()
            parsed = json.loads(lines[0])

        self.assertEqual(parsed["method"], "POST")
        self.assertEqual(parsed["path"], "/v1/chat/completions")
        self.assertEqual(parsed["status"], 200)
        self.assertEqual(parsed["body"]["model"], "codex-cli")
        self.assertIn("Content-Type", parsed["headers"])
        self.assertNotIn("Authorization", parsed["headers"])


def _response(answer="ok"):
    return CodexResponse(
        ok=True,
        question="hello",
        answer=answer,
        exit_code=0,
        duration_seconds=1.0,
        cwd="/tmp",
        command=["codex"],
        stdout="",
        stderr="",
    )


if __name__ == "__main__":
    unittest.main()
