import json
import sys
import tempfile
import unittest
from pathlib import Path
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_json_cli.cli import main
from codex_json_cli.openai_compat import to_chat_completion
from codex_json_cli.runner import CodexRequest, CodexResponse, ask_codex


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


if __name__ == "__main__":
    unittest.main()
