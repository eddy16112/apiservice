# Codex JSON LLM

一个很薄的 Python CLI 包装层：把用户问题转发给本机 `codex` CLI，再把 Codex 的最终回复包装成 OpenAI Chat Completions 兼容 JSON 输出。

## 安装

在当前仓库里做 editable 安装：

```bash
python3 -m pip install -e .
```

安装后可以直接使用：

```bash
llm ask "用一句话解释这个仓库的作用"
```

输出示例：

```json
{
  "id": "chatcmpl-9f6f8f6b0f89475dadf0c0db2bd7a0e3",
  "object": "chat.completion",
  "created": 1777180000,
  "model": "codex-cli",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "这是一个把问题转发给 Codex CLI 并输出 OpenAI 兼容 JSON 的 Python 命令行工具。",
        "refusal": null,
        "annotations": []
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  },
  "system_fingerprint": null
}
```

## 快速运行

如果用 conda 测试，可以这样从零启动：

```bash
cd /Users/weiwu/apiservice
conda create -n codex-json-llm python=3.11 -y
conda activate codex-json-llm
python -m pip install -e .
```

启动给 OpenClaw 使用的 API service：

```bash
llm serve \
  --host 127.0.0.1 \
  --port 8081 \
  --cwd /Users/weiwu/apiservice
```

如果要启用 API key：

```bash
CODEX_JSON_LLM_API_KEY=secret llm serve \
  --host 127.0.0.1 \
  --port 8081 \
  --cwd /Users/weiwu/apiservice
```

OpenClaw 里使用 vLLM provider：

```text
Provider: vllm
Base URL: http://127.0.0.1:8081/v1
Model: vllm/codex-cli
API Key: secret
```

如果服务没有设置 `CODEX_JSON_LLM_API_KEY`，OpenClaw 里的 API Key 可以填任意非空值。

本地测试接口：

```bash
curl http://127.0.0.1:8081/v1/models
```

如果启用了 API key，聊天请求需要加 Authorization header：

```bash
curl http://127.0.0.1:8081/v1/chat/completions \
  -H "Authorization: Bearer secret" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "codex-cli",
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'
```

查看请求日志：

```bash
tail -f logs/requests.jsonl
```

## Telegram Bridge

多人从手机接入时，Telegram Bot 是最简单的方案：不需要公网 HTTPS，不需要 WhatsApp Business verification，也不需要 OpenClaw。它使用 Telegram Bot API 的 long polling。

流程：

```text
多人 Telegram
  ↓
Telegram Bot API getUpdates
  ↓
llm telegram
  ↓
llm serve 监听 localhost:8081
  ↓
Codex CLI
```

### 1. 创建 Telegram Bot

在 Telegram 里找 `@BotFather`：

1. 发送 `/newbot`
2. 按提示取名字和 username
3. 复制 BotFather 给你的 token

设置环境变量：

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
```

不要把 token 发给别人，也不要 commit。

### 2. 启动 LLM API

```bash
cd /Users/weiwu/apiservice
conda activate codex-json-llm
python -m pip install -e .

llm serve \
  --host 127.0.0.1 \
  --port 8081 \
  --cwd /Users/weiwu/apiservice
```

### 3. 启动 Telegram Bridge

另开一个终端：

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_ALLOWED_USERS="123456789,987654321"

llm telegram
```

也可以使用独立入口：

```bash
llm-telegram
```

默认会调用：

```text
http://127.0.0.1:8081/v1/chat/completions
```

如果 `llm serve` 要求 API key：

```bash
export CODEX_JSON_LLM_API_KEY="secret"
llm telegram
```

参数说明：

- `TELEGRAM_BOT_TOKEN`：BotFather 给你的 bot token。
- `TELEGRAM_ALLOWED_USERS`：允许使用 bot 的 Telegram user id，逗号分隔。
- `TELEGRAM_ALLOWED_CHATS`：允许使用 bot 的 Telegram chat id，逗号分隔；群聊通常用负数 chat id。
- `TELEGRAM_UPLOAD_DIR` / `--upload-dir`：Telegram 图片下载目录，默认是 `uploads/telegram`。
- `--drop-pending-updates`：启动时丢弃 Telegram 队列里的旧消息，避免第一次启动回复历史消息。
- `/start`：本地回复连接提示，不会转发给 Codex。
- `/reset`：用户给 bot 发送 `/reset` 会清空自己的 Codex session 映射，下次消息会创建新 session。

如果不设置 `TELEGRAM_ALLOWED_USERS` 和 `TELEGRAM_ALLOWED_CHATS`，bridge 会接受所有能给 bot 发消息的人。多人测试时可以先开放，长期使用建议加白名单。

Telegram bridge 会按 Telegram user id 自动隔离 session：

```text
telegram:user:123
telegram:user:456
```

也就是说，不同用户不会共用同一个 Codex CLI session；同一个用户的后续消息会用 `codex exec resume <session-id>` 接着聊。

Telegram bridge 也支持用户发送图片。图片会先下载到当前项目目录下：

```text
uploads/telegram/user-123/20-photo-id.jpg
```

然后 bridge 会把图片本地路径和用户 caption 一起发给 `llm serve`。例如用户发送图片并写 caption `这是什么？`，Codex 收到的内容大致是：

```text
Telegram 用户上传了一张图片。
图片本地路径: /Users/weiwu/apiservice/uploads/telegram/user-123/20-photo-id.jpg
用户说明:
这是什么？
请根据这个图片文件和用户说明处理请求。
```

`uploads/` 已经加入 `.gitignore`，不会把用户上传的图片提交到 git。

### 4. 如何找到 Telegram User ID

最简单的方法：

1. 先不设置 `TELEGRAM_ALLOWED_USERS`。
2. 启动 `llm telegram`。
3. 用你的 Telegram 给 bot 发一条消息。
4. 终端会打印类似：

```text
Telegram inbound chat 456 user 123: Hello
```

这里的 `123` 就是你的 user id。拿到以后重启：

```bash
export TELEGRAM_ALLOWED_USERS="123"
llm telegram --drop-pending-updates
```

## 用法

```bash
llm ask "问题"
llm ask --pretty "问题"
llm ask --cwd /path/to/repo "问题"
llm ask --model gpt-5.4 "问题"
llm ask --timeout 60 "问题"
llm ask --session alice "继续上一次对话"
echo "问题" | llm ask -
```

默认会把当前目录作为 Codex 的工作目录传给 `codex exec --cd`。如果要指定另一个 Codex 可执行文件：

```bash
llm ask --codex-bin /opt/homebrew/bin/codex "问题"
```

如果使用 `--session`，第一次请求会创建新的 Codex CLI session，并把真实 session id 写到 `session/sessions.json`；后续相同 session key 会用 `codex exec resume <session-id>`。

## API Service

这个项目也可以作为一个本地 OpenAI-compatible HTTP API 给 OpenClaw 或其他兼容 OpenAI API 的客户端使用。

启动服务：

```bash
llm serve --host 127.0.0.1 --port 8081
```

也可以使用独立入口：

```bash
llm-api --host 127.0.0.1 --port 8081
```

OpenClaw 里配置 OpenAI-compatible provider 时，通常把 base URL 指到：

```text
http://127.0.0.1:8081/v1
```

模型名使用：

```text
codex-cli
```

服务支持的接口：

- `GET /health`
- `GET /v1/models`
- `GET /v1/models/{id}`
- `POST /v1/chat/completions`

测试模型列表：

```bash
curl http://127.0.0.1:8081/v1/models
```

测试 Chat Completions：

```bash
curl http://127.0.0.1:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "codex-cli",
    "metadata": {"session_id": "alice"},
    "messages": [
      {"role": "user", "content": "用一句话解释这个项目"}
    ]
  }'
```

`metadata.session_id` 是可选字段。设置后，API service 会为这个 id 维护独立 Codex CLI session；不设置时，请求保持无状态。

如果客户端发送 `"stream": true`，服务会返回 SSE 格式的 `chat.completion.chunk`。当前实现会等 Codex CLI 完成后再一次性发送内容块，所以是兼容流式格式，不是真正逐 token 流式。

### API Key

默认不要求认证。如果要让服务要求 Bearer token：

```bash
CODEX_JSON_LLM_API_KEY=secret llm serve --host 127.0.0.1 --port 8081
```

请求时带上：

```bash
curl http://127.0.0.1:8081/v1/chat/completions \
  -H "Authorization: Bearer secret" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "codex-cli",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### 服务参数

常用参数：

```bash
llm serve \
  --host 127.0.0.1 \
  --port 8081 \
  --cwd /Users/weiwu/apiservice \
  --served-model codex-cli \
  --codex-bin /opt/homebrew/bin/codex \
  --timeout 120
```

- `--served-model`：对 OpenAI-compatible 客户端暴露的模型名。
- `--codex-model`：传给 `codex exec --model` 的真实 Codex 模型名；不设置时使用 Codex CLI 默认配置。
- `--cwd`：传给 Codex CLI 的工作目录。
- `--api-key`：要求客户端使用 `Authorization: Bearer <token>`。

### Request Log

Request log 默认开启，写到：

```text
logs/requests.jsonl
```

默认启动即可记录请求：

```bash
llm serve \
  --host 127.0.0.1 \
  --port 8081 \
  --cwd /Users/weiwu/apiservice
```

然后查看：

```bash
tail -f logs/requests.jsonl
```

日志是 JSONL 格式，一行一个请求，包含时间、method/path、client、状态码、耗时、headers 和 JSON body。`Authorization` / `Proxy-Authorization` 不会写入日志。每次重新启动 `llm serve` 时，旧的 request log 会先被删除，这样 `logs/requests.jsonl` 只保留本轮服务启动后的请求。

如果想改日志路径：

```bash
CODEX_JSON_LLM_REQUEST_LOG=logs/requests.jsonl llm serve
```

如果临时不想写 request log：

```bash
llm serve --no-request-log
```

## WhatsApp Bridge

如果想用自己的手机 WhatsApp 给本地 `llm serve` 发消息，需要三个进程：

```text
手机 WhatsApp
  ↓
Meta WhatsApp Cloud API
  ↓
Cloudflare Tunnel HTTPS URL
  ↓
llm whatsapp 监听 localhost:8080
  ↓
llm serve 监听 localhost:8081
  ↓
Codex CLI
```

### 1. 启动 LLM API

先启动内部 OpenAI-compatible API：

```bash
cd /Users/weiwu/apiservice
conda activate codex-json-llm
python -m pip install -e .

llm serve \
  --host 127.0.0.1 \
  --port 8081 \
  --cwd /Users/weiwu/apiservice
```

### 2. 启动 WhatsApp Bridge

另开一个终端，启动 webhook bridge。下面这些值需要从 Meta Developer / WhatsApp Cloud API 页面拿：

```bash
export WHATSAPP_VERIFY_TOKEN="choose-a-secret"
export WHATSAPP_ACCESS_TOKEN="EAAG..."
export WHATSAPP_PHONE_NUMBER_ID="123456789012345"
export WHATSAPP_ALLOWED_FROM="15551234567"

llm whatsapp \
  --host 127.0.0.1 \
  --port 8080 \
  --llm-base-url http://127.0.0.1:8081/v1
```

也可以使用独立入口：

```bash
llm-whatsapp --host 127.0.0.1 --port 8080
```

参数说明：

- `WHATSAPP_VERIFY_TOKEN`：你自己设置的 webhook 验证 token；Meta Dashboard 里也填同一个。
- `WHATSAPP_ACCESS_TOKEN`：Meta WhatsApp Cloud API access token。
- `WHATSAPP_PHONE_NUMBER_ID`：WhatsApp Business 的 phone number id，不是你的手机号。
- `WHATSAPP_ALLOWED_FROM`：允许使用这个 bridge 的手机号，格式是不带 `+` 的国际号码；建议只填你自己的手机。
- `--llm-base-url`：指向本地 `llm serve` 的 `/v1` base URL，默认是 `http://127.0.0.1:8081/v1`。

如果你配置了 Meta App Secret，也可以开启 webhook 签名校验：

```bash
export WHATSAPP_APP_SECRET="your-meta-app-secret"
```

### 3. 暴露公网 HTTPS

你的 Cloudflare Quick Tunnel 已经是这个形式：

```bash
cloudflared tunnel --url http://localhost:8080
```

它会生成类似：

```text
https://festival-arrange-export-characteristic.trycloudflare.com
```

Meta Dashboard 里的 Callback URL 填：

```text
https://festival-arrange-export-characteristic.trycloudflare.com/webhooks/whatsapp
```

Verify Token 填：

```text
choose-a-secret
```

也就是和 `WHATSAPP_VERIFY_TOKEN` 一样的值。验证通过后，订阅 `messages` webhook 字段。

### 4. 本地验证 Webhook

可以先手动测试 Meta webhook 验证逻辑：

```bash
curl "https://festival-arrange-export-characteristic.trycloudflare.com/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=choose-a-secret&hub.challenge=123"
```

如果输出：

```text
123
```

说明 Cloudflare Tunnel -> WhatsApp bridge 已经通了。

### 5. 收发消息

用 `WHATSAPP_ALLOWED_FROM` 里的手机号码给你的 WhatsApp Cloud API 测试号发消息。bridge 会：

1. 收到 Meta webhook。
2. 提取 text message。
3. 调用 `llm serve /v1/chat/completions`。
4. 调用 Meta Graph API 发回 WhatsApp 文本回复。

注意：WhatsApp Cloud API 的普通文本回复通常需要在用户先发消息后的 customer service window 内发送；首次主动联系用户通常需要 template message。

## 设计

这个项目分成两层：

- CLI 层：`src/codex_json_cli/cli.py` 负责解析 `llm ask` 参数、读取 stdin、打印 JSON。
- Runner 层：`src/codex_json_cli/runner.py` 负责拼出 `codex exec` 命令、执行子进程、读取 Codex 最后一条回复。
- Session 层：`src/codex_json_cli/session_store.py` 负责保存 session key 到 Codex CLI session id 的映射。
- OpenAI 兼容层：`src/codex_json_cli/openai_compat.py` 负责把 Codex 回复转换成 Chat Completion object。
- API 层：`src/codex_json_cli/server.py` 负责提供 `/v1/chat/completions` 和 `/v1/models`。
- Telegram Bridge 层：`src/codex_json_cli/telegram_bridge.py` 负责 long polling Telegram Bot API、调用 API 层、发送 Telegram 回复。
- WhatsApp Bridge 层：`src/codex_json_cli/whatsapp_bridge.py` 负责接收 WhatsApp webhook、调用 API 层、发送 WhatsApp 回复。

核心调用顺序：

```text
llm ask "问题"
  ↓
setup.cfg 里的 console_scripts
  ↓
codex_json_cli.cli:main()
  ↓
cli.py 解析参数并组装 CodexRequest
  ↓
runner.py::ask_codex()
  ↓
subprocess.run(["codex", "exec", ...])
  ↓
如果设置 --session，runner.py 保存/读取 Codex session id
  ↓
runner.py 读取 Codex 最后一条回复
  ↓
openai_compat.py::to_chat_completion()
  ↓
cli.py 打印 OpenAI-compatible JSON
```

API service 的调用顺序：

```text
OpenClaw / OpenAI-compatible client
  ↓
POST /v1/chat/completions
  ↓
server.py 解析 messages 并组装 CodexRequest
  ↓
runner.py::ask_codex()
  ↓
如果 metadata.session_id 存在，runner.py 使用独立 Codex session
  ↓
subprocess.run(["codex", "exec", ...])
  ↓
openai_compat.py::to_chat_completion()
  ↓
server.py 返回 OpenAI-compatible JSON
```

Telegram bridge 的调用顺序：

```text
Telegram user
  ↓
telegram_bridge.py getUpdates long polling
  ↓
telegram_bridge.py 提取 text message
  ↓
POST llm serve /v1/chat/completions，metadata.session_id=telegram:user:<id>
  ↓
POST Telegram Bot API sendMessage
  ↓
Telegram user 收到回复
```

WhatsApp bridge 的调用顺序：

```text
手机 WhatsApp
  ↓
Meta webhook POST /webhooks/whatsapp
  ↓
whatsapp_bridge.py 提取 text message
  ↓
POST llm serve /v1/chat/completions
  ↓
POST Meta Graph API /{phone-number-id}/messages
  ↓
手机 WhatsApp 收到回复
```

无 session 时实际调用形式大致是：

```bash
codex exec --cd "$PWD" --output-last-message /tmp/codex-answer.txt --color never "问题"
```

有 session 时，第一次调用会在 `codex exec` 里加 `--json` 以提取真实 session id；后续调用形式大致是：

```bash
codex exec resume --output-last-message /tmp/codex-answer.txt --json <session-id> "问题"
```

成功时输出 OpenAI Chat Completion object，方便上层服务按下面的路径取回复：

```python
content = payload["choices"][0]["message"]["content"]
```

失败时输出 OpenAI 风格的 error object：

```json
{
  "error": {
    "message": "Codex CLI failed with exit code 1. ...",
    "type": "server_error",
    "param": null,
    "code": "codex_cli_error"
  }
}
```

Codex CLI 当前不会稳定暴露 token usage，所以 `usage` 里的 token 数会填 `0`，只用于兼容 OpenAI API 客户端字段结构。

## Session

Session 的映射文件默认放在当前 repo 的：

```text
session/sessions.json
```

这个目录已经加入 `.gitignore`。它只保存本项目的 session key 到 Codex CLI session id 的映射，例如：

```json
{
  "sessions": {
    "telegram:user:123": {
      "codex_session_id": "019dc8e7-65fe-7c81-a6c0-72ad97569c5c"
    }
  }
}
```

支持 session 的入口：

- CLI：`llm ask --session alice "问题"`
- API：请求体里加 `"metadata": {"session_id": "alice"}`
- Telegram：自动使用 `telegram:user:<telegram_user_id>`

清空 Telegram 当前用户 session：

```text
/reset
```

## 开发

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

核心文件：

- `src/codex_json_cli/cli.py`：命令行参数和 JSON 输出。
- `src/codex_json_cli/runner.py`：调用 `codex exec` 并读取最终答案。
- `src/codex_json_cli/session_store.py`：本地 session key 到 Codex CLI session id 的映射。
- `src/codex_json_cli/openai_compat.py`：OpenAI Chat Completions 兼容响应格式。
- `src/codex_json_cli/server.py`：OpenAI-compatible HTTP API service。
- `src/codex_json_cli/telegram_bridge.py`：Telegram Bot API long-polling bridge。
- `src/codex_json_cli/whatsapp_bridge.py`：WhatsApp Cloud API webhook bridge。
- `setup.cfg` / `setup.py`：包元数据和 `llm` console script 入口。
- `tests/test_cli.py`：不真实调用 Codex 的单元测试。
