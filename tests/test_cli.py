import argparse
import json
import hmac
import hashlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_json_cli.cli import main
from codex_json_cli.openai_compat import to_chat_completion, to_chat_completion_chunk, to_model_list
from codex_json_cli.runner import CodexRequest, CodexResponse, ask_codex
from codex_json_cli.session_store import SessionStore
from codex_json_cli.server import (
    ServerConfig,
    add_server_arguments,
    build_request_log_record,
    config_from_args,
    extract_session_key,
    handle_chat_completion,
    is_authorized,
    messages_to_prompt,
    reset_request_log,
    write_request_log,
)
from codex_json_cli.telegram_bridge import (
    TelegramBridgeConfig,
    TelegramPhotoMessage,
    TelegramTextMessage,
    build_photo_prompt,
    build_send_message_payload,
    chunk_text as telegram_chunk_text,
    extract_photo_message as extract_telegram_photo_message,
    extract_text_message as extract_telegram_text_message,
    handle_photo_message as handle_telegram_photo_message,
    handle_text_message as handle_telegram_text_message,
    is_allowed as is_telegram_allowed,
    parse_int_list,
    redact_url,
    telegram_session_id,
    telegram_photo_target_path,
)
from codex_json_cli.whatsapp_bridge import (
    WhatsAppBridgeConfig,
    WhatsAppTextMessage,
    build_whatsapp_text_payload,
    chunk_text,
    extract_text_messages,
    handle_text_message,
    is_valid_meta_signature,
    parse_allowed_numbers,
    verify_meta_challenge,
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

    def test_ask_codex_stores_session_id_from_json_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            store_path = cwd / "sessions.json"

            def fake_run(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text("session answer\n", encoding="utf-8")

                class Completed:
                    returncode = 0
                    stdout = (
                        '{"type":"session_meta","payload":{"id":"session-123"}}\n'
                    )
                    stderr = ""

                return Completed()

            request = CodexRequest(
                question="hello",
                cwd=cwd,
                codex_bin="codex",
                session_key="telegram:user:123",
                session_store_path=store_path,
            )
            with patch("codex_json_cli.runner.subprocess.run", side_effect=fake_run):
                response = ask_codex(request)

            self.assertTrue(response.ok)
            self.assertEqual(response.session_id, "session-123")
            self.assertIn("--json", response.command)
            stored = SessionStore(store_path).get("telegram:user:123")
            self.assertEqual(stored.codex_session_id, "session-123")

    def test_ask_codex_resumes_stored_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            store_path = cwd / "sessions.json"
            SessionStore(store_path).set_codex_session("telegram:user:123", "session-123")

            def fake_run(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text("resumed answer\n", encoding="utf-8")

                class Completed:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return Completed()

            request = CodexRequest(
                question="hello again",
                cwd=cwd,
                codex_bin="codex",
                session_key="telegram:user:123",
                session_store_path=store_path,
            )
            with patch("codex_json_cli.runner.subprocess.run", side_effect=fake_run):
                response = ask_codex(request)

            self.assertTrue(response.ok)
            self.assertEqual(response.session_id, "session-123")
            self.assertEqual(response.command[:4], ["codex", "exec", "resume", "--output-last-message"])
            self.assertIn("session-123", response.command)
            self.assertNotIn("--cd", response.command)

    def test_ask_codex_discovers_session_id_from_codex_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "repo"
            cwd.mkdir()
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            store_path = cwd / "sessions.json"

            def fake_run(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text("state answer\n", encoding="utf-8")
                db_path = codex_home / "state_5.sqlite"
                connection = sqlite3.connect(db_path)
                connection.execute(
                    """
                    CREATE TABLE threads (
                        id TEXT,
                        cwd TEXT,
                        first_user_message TEXT,
                        created_at INTEGER,
                        created_at_ms INTEGER
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                    (
                        "session-from-state",
                        str(cwd.resolve()),
                        "hello from telegram",
                        int(time.time()),
                        int(time.time() * 1000),
                    ),
                )
                connection.commit()
                connection.close()

                class Completed:
                    returncode = 0
                    stdout = '{"type":"agent_message","payload":{"text":"ok"}}\n'
                    stderr = ""

                return Completed()

            request = CodexRequest(
                question="hello from telegram",
                cwd=cwd,
                codex_bin="codex",
                session_key="telegram:user:123",
                session_store_path=store_path,
            )
            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}):
                with patch("codex_json_cli.runner.subprocess.run", side_effect=fake_run):
                    response = ask_codex(request)

            self.assertEqual(response.session_id, "session-from-state")
            stored = SessionStore(store_path).get("telegram:user:123")
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.codex_session_id, "session-from-state")

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

    def test_ask_passes_session_key_to_runner(self):
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

        with patch("codex_json_cli.cli.ask_codex", return_value=response) as ask_mock:
            with patch("builtins.print"):
                exit_code = main(["ask", "--session", "alice", "hello"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(ask_mock.call_args.args[0].session_key, "alice")

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

    def test_extract_session_key_from_request_metadata(self):
        self.assertEqual(
            extract_session_key(
                {"metadata": {"session_id": "telegram:user:123"}},
                prefix="api",
            ),
            "telegram:user:123",
        )
        self.assertEqual(
            extract_session_key({"metadata": {"session_id": "bob"}}, prefix="api"),
            "api:bob",
        )

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

    def test_request_log_defaults_to_logs_requests_jsonl(self):
        with patch.dict("os.environ", {}, clear=True):
            parser = argparse.ArgumentParser()
            add_server_arguments(parser)
            config = config_from_args(parser.parse_args([]))

        self.assertEqual(config.request_log, Path("logs") / "requests.jsonl")

    def test_request_log_can_be_disabled(self):
        parser = argparse.ArgumentParser()
        add_server_arguments(parser)
        config = config_from_args(parser.parse_args(["--no-request-log"]))

        self.assertIsNone(config.request_log)

    def test_reset_request_log_deletes_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "requests.jsonl"
            log_path.write_text("old\n", encoding="utf-8")

            reset_request_log(log_path)

            self.assertFalse(log_path.exists())


class WhatsAppBridgeTests(unittest.TestCase):
    def test_verify_meta_challenge_returns_challenge_for_matching_token(self):
        challenge = verify_meta_challenge(
            {
                "hub.mode": ["subscribe"],
                "hub.verify_token": ["secret"],
                "hub.challenge": ["12345"],
            },
            expected_token="secret",
        )

        self.assertEqual(challenge, "12345")

    def test_verify_meta_challenge_rejects_wrong_token(self):
        challenge = verify_meta_challenge(
            {
                "hub.mode": ["subscribe"],
                "hub.verify_token": ["wrong"],
                "hub.challenge": ["12345"],
            },
            expected_token="secret",
        )

        self.assertEqual(challenge, None)

    def test_meta_signature_validation(self):
        raw = b'{"hello":"world"}'
        digest = hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()

        self.assertTrue(
            is_valid_meta_signature(raw, f"sha256={digest}", "app-secret")
        )
        self.assertFalse(
            is_valid_meta_signature(raw, "sha256=wrong", "app-secret")
        )

    def test_extract_text_messages_from_webhook_payload(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "phone-id"},
                                "messages": [
                                    {
                                        "id": "wamid.1",
                                        "from": "15551234567",
                                        "type": "text",
                                        "text": {"body": "Hello"},
                                    },
                                    {
                                        "id": "wamid.2",
                                        "from": "15551234567",
                                        "type": "image",
                                    },
                                ],
                            }
                        }
                    ]
                }
            ]
        }

        messages = extract_text_messages(payload)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, "wamid.1")
        self.assertEqual(messages[0].from_number, "15551234567")
        self.assertEqual(messages[0].text, "Hello")
        self.assertEqual(messages[0].phone_number_id, "phone-id")

    def test_build_whatsapp_text_payload(self):
        payload = build_whatsapp_text_payload("15551234567", "Hello")

        self.assertEqual(payload["messaging_product"], "whatsapp")
        self.assertEqual(payload["to"], "15551234567")
        self.assertEqual(payload["type"], "text")
        self.assertEqual(payload["text"]["body"], "Hello")
        self.assertFalse(payload["text"]["preview_url"])

    def test_handle_text_message_calls_llm_and_sends_reply(self):
        sent = []
        config = WhatsAppBridgeConfig(allowed_from=["15551234567"])
        message = WhatsAppTextMessage(
            message_id="wamid.1",
            from_number="15551234567",
            text="Hello",
        )

        def fake_llm(prompt, _config):
            self.assertEqual(prompt, "Hello")
            return "Hi back"

        def fake_send(to_number, text, _config):
            sent.append((to_number, text))
            return {"messages": [{"id": "wamid.reply"}]}

        with patch("builtins.print"):
            handle_text_message(
                message,
                config,
                llm_func=fake_llm,
                send_func=fake_send,
            )

        self.assertEqual(sent, [("15551234567", "Hi back")])

    def test_handle_text_message_ignores_disallowed_sender(self):
        sent = []
        config = WhatsAppBridgeConfig(allowed_from=["15550000000"])
        message = WhatsAppTextMessage(
            message_id="wamid.1",
            from_number="15551234567",
            text="Hello",
        )

        with patch("builtins.print"):
            handle_text_message(
                message,
                config,
                llm_func=lambda prompt, _config: "Hi",
                send_func=lambda to_number, text, _config: sent.append((to_number, text)),
            )

        self.assertEqual(sent, [])

    def test_parse_allowed_numbers_and_chunk_text(self):
        self.assertEqual(
            parse_allowed_numbers("15551234567, 15557654321"),
            ["15551234567", "15557654321"],
        )
        self.assertEqual(list(chunk_text("abcdef", 2)), ["ab", "cd", "ef"])


class TelegramBridgeTests(unittest.TestCase):
    def test_extract_text_message_from_update(self):
        update = {
            "update_id": 10,
            "message": {
                "message_id": 20,
                "from": {
                    "id": 123,
                    "is_bot": False,
                    "username": "alice",
                    "first_name": "Alice",
                },
                "chat": {"id": 456, "type": "private"},
                "text": "Hello",
            },
        }

        message = extract_telegram_text_message(update)

        self.assertEqual(message.update_id, 10)
        self.assertEqual(message.message_id, 20)
        self.assertEqual(message.from_user_id, 123)
        self.assertEqual(message.chat_id, 456)
        self.assertEqual(message.text, "Hello")
        self.assertEqual(message.username, "alice")

    def test_extract_text_message_ignores_bot_messages(self):
        update = {
            "update_id": 10,
            "message": {
                "message_id": 20,
                "from": {"id": 123, "is_bot": True},
                "chat": {"id": 456, "type": "private"},
                "text": "Hello",
            },
        }

        self.assertIsNone(extract_telegram_text_message(update))

    def test_extract_photo_message_uses_largest_photo(self):
        update = {
            "update_id": 10,
            "message": {
                "message_id": 20,
                "from": {"id": 123, "is_bot": False},
                "chat": {"id": 456, "type": "private"},
                "caption": "这是什么？",
                "photo": [
                    {
                        "file_id": "small-file",
                        "file_unique_id": "small",
                        "width": 90,
                        "height": 90,
                        "file_size": 1000,
                    },
                    {
                        "file_id": "large-file",
                        "file_unique_id": "large",
                        "width": 1280,
                        "height": 960,
                        "file_size": 5000,
                    },
                ],
            },
        }

        message = extract_telegram_photo_message(update)

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.file_id, "large-file")
        self.assertEqual(message.file_unique_id, "large")
        self.assertEqual(message.caption, "这是什么？")

    def test_is_allowed_accepts_matching_user_or_chat(self):
        message = TelegramTextMessage(
            update_id=1,
            message_id=2,
            chat_id=456,
            from_user_id=123,
            text="Hello",
            chat_type="private",
        )

        self.assertTrue(is_telegram_allowed(message, TelegramBridgeConfig()))
        self.assertTrue(
            is_telegram_allowed(message, TelegramBridgeConfig(allowed_users=[123]))
        )
        self.assertTrue(
            is_telegram_allowed(message, TelegramBridgeConfig(allowed_chats=[456]))
        )
        self.assertFalse(
            is_telegram_allowed(message, TelegramBridgeConfig(allowed_users=[999]))
        )

    def test_telegram_session_id_uses_user_when_available(self):
        message = TelegramTextMessage(
            update_id=1,
            message_id=2,
            chat_id=456,
            from_user_id=123,
            text="Hello",
            chat_type="private",
        )

        self.assertEqual(
            telegram_session_id(message, TelegramBridgeConfig()),
            "telegram:user:123",
        )

    def test_handle_text_message_calls_llm_and_sends_reply(self):
        sent = []
        config = TelegramBridgeConfig(allowed_users=[123])
        message = TelegramTextMessage(
            update_id=1,
            message_id=2,
            chat_id=456,
            from_user_id=123,
            text="Hello",
            chat_type="private",
        )

        def fake_llm(prompt, _config, session_id):
            self.assertEqual(prompt, "Hello")
            self.assertEqual(session_id, "telegram:user:123")
            return "Hi back"

        def fake_send(chat_id, text, _config):
            sent.append((chat_id, text))
            return {"ok": True}

        with patch("builtins.print"):
            handle_telegram_text_message(
                message,
                config,
                llm_func=fake_llm,
                send_func=fake_send,
            )

        self.assertEqual(sent, [(456, "Hi back")])

    def test_handle_start_message_replies_without_calling_llm(self):
        sent = []
        config = TelegramBridgeConfig(allowed_users=[123])
        message = TelegramTextMessage(
            update_id=1,
            message_id=2,
            chat_id=456,
            from_user_id=123,
            text="/start anything",
            chat_type="private",
        )

        def fail_llm(prompt, _config, session_id):
            raise AssertionError("start should not be forwarded to LLM")

        def fake_send(chat_id, text, _config):
            sent.append((chat_id, text))
            return {"ok": True}

        with patch("builtins.print"):
            handle_telegram_text_message(
                message,
                config,
                llm_func=fail_llm,
                send_func=fake_send,
            )

        self.assertEqual(sent[0][0], 456)
        self.assertIn("/reset", sent[0][1])

    def test_handle_photo_message_downloads_and_sends_path_to_llm(self):
        sent = []
        config = TelegramBridgeConfig(allowed_users=[123])
        message = TelegramPhotoMessage(
            update_id=1,
            message_id=2,
            chat_id=456,
            from_user_id=123,
            file_id="photo-file",
            file_unique_id="photo-unique",
            caption="请描述这张图",
            chat_type="private",
        )

        def fake_download(_message, _config):
            return Path("/tmp/telegram-photo.jpg")

        def fake_llm(prompt, _config, session_id):
            self.assertIn("/tmp/telegram-photo.jpg", prompt)
            self.assertIn("请描述这张图", prompt)
            self.assertEqual(session_id, "telegram:user:123")
            return "这是一张图片"

        def fake_send(chat_id, text, _config):
            sent.append((chat_id, text))
            return {"ok": True}

        with patch("builtins.print"):
            handle_telegram_photo_message(
                message,
                config,
                llm_func=fake_llm,
                send_func=fake_send,
                download_func=fake_download,
            )

        self.assertEqual(sent, [(456, "这是一张图片")])

    def test_telegram_photo_target_path_uses_upload_dir_and_safe_name(self):
        message = TelegramPhotoMessage(
            update_id=1,
            message_id=20,
            chat_id=456,
            from_user_id=123,
            file_id="bad/file:id",
            file_unique_id=None,
            caption="",
            chat_type="private",
        )

        path = telegram_photo_target_path(
            message,
            "photos/file_1.JPG",
            TelegramBridgeConfig(upload_dir=Path("custom-uploads")),
        )

        self.assertEqual(
            path,
            Path("custom-uploads") / "user-123" / "20-bad-file-id.jpg",
        )

    def test_build_photo_prompt_includes_caption_or_empty_note(self):
        prompt = build_photo_prompt(Path("/tmp/a.jpg"), "")

        self.assertIn("/tmp/a.jpg", prompt)
        self.assertIn("用户没有附加文字说明。", prompt)

    def test_handle_text_message_ignores_disallowed_sender(self):
        sent = []
        config = TelegramBridgeConfig(allowed_users=[999])
        message = TelegramTextMessage(
            update_id=1,
            message_id=2,
            chat_id=456,
            from_user_id=123,
            text="Hello",
            chat_type="private",
        )

        with patch("builtins.print"):
            handle_telegram_text_message(
                message,
                config,
                llm_func=lambda prompt, _config, session_id: "Hi",
                send_func=lambda chat_id, text, _config: sent.append((chat_id, text)),
            )

        self.assertEqual(sent, [])

    def test_build_send_message_payload(self):
        payload = build_send_message_payload(456, "Hello")

        self.assertEqual(payload["chat_id"], 456)
        self.assertEqual(payload["text"], "Hello")
        self.assertTrue(payload["disable_web_page_preview"])

    def test_parse_int_list_and_chunk_text(self):
        self.assertEqual(parse_int_list("123, 456"), [123, 456])
        self.assertEqual(list(telegram_chunk_text("abcdef", 2)), ["ab", "cd", "ef"])

    def test_redact_url_hides_telegram_bot_token(self):
        url = "https://api.telegram.org/bot123456:SECRET/deleteWebhook"

        redacted = redact_url(url)

        self.assertEqual(
            redacted,
            "https://api.telegram.org/bot<redacted>/deleteWebhook",
        )
        self.assertNotIn("SECRET", redacted)

        file_url = "https://api.telegram.org/file/bot123456:SECRET/photos/file.jpg"
        self.assertEqual(
            redact_url(file_url),
            "https://api.telegram.org/file/bot<redacted>/photos/file.jpg",
        )


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
