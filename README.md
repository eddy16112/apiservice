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
  --port 8000 \
  --cwd /Users/weiwu/apiservice \
  --request-log logs/requests.jsonl
```

如果要启用 API key：

```bash
CODEX_JSON_LLM_API_KEY=secret llm serve \
  --host 127.0.0.1 \
  --port 8000 \
  --cwd /Users/weiwu/apiservice \
  --request-log logs/requests.jsonl
```

OpenClaw 里使用 vLLM provider：

```text
Provider: vllm
Base URL: http://127.0.0.1:8000/v1
Model: vllm/codex-cli
API Key: secret
```

如果服务没有设置 `CODEX_JSON_LLM_API_KEY`，OpenClaw 里的 API Key 可以填任意非空值。

本地测试接口：

```bash
curl http://127.0.0.1:8000/v1/models
```

如果启用了 API key，聊天请求需要加 Authorization header：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
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

## 用法

```bash
llm ask "问题"
llm ask --pretty "问题"
llm ask --cwd /path/to/repo "问题"
llm ask --model gpt-5.4 "问题"
llm ask --timeout 60 "问题"
echo "问题" | llm ask -
```

默认会把当前目录作为 Codex 的工作目录传给 `codex exec --cd`。如果要指定另一个 Codex 可执行文件：

```bash
llm ask --codex-bin /opt/homebrew/bin/codex "问题"
```

## API Service

这个项目也可以作为一个本地 OpenAI-compatible HTTP API 给 OpenClaw 或其他兼容 OpenAI API 的客户端使用。

启动服务：

```bash
llm serve --host 127.0.0.1 --port 8000
```

也可以使用独立入口：

```bash
llm-api --host 127.0.0.1 --port 8000
```

OpenClaw 里配置 OpenAI-compatible provider 时，通常把 base URL 指到：

```text
http://127.0.0.1:8000/v1
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
curl http://127.0.0.1:8000/v1/models
```

测试 Chat Completions：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "codex-cli",
    "messages": [
      {"role": "user", "content": "用一句话解释这个项目"}
    ]
  }'
```

如果客户端发送 `"stream": true`，服务会返回 SSE 格式的 `chat.completion.chunk`。当前实现会等 Codex CLI 完成后再一次性发送内容块，所以是兼容流式格式，不是真正逐 token 流式。

### API Key

默认不要求认证。如果要让服务要求 Bearer token：

```bash
CODEX_JSON_LLM_API_KEY=secret llm serve --host 127.0.0.1 --port 8000
```

请求时带上：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
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
  --port 8000 \
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

如果想记录 OpenClaw 发来的请求，可以启动时加 `--request-log`：

```bash
llm serve \
  --host 127.0.0.1 \
  --port 8000 \
  --cwd /Users/weiwu/apiservice \
  --request-log logs/requests.jsonl
```

然后查看：

```bash
tail -f logs/requests.jsonl
```

日志是 JSONL 格式，一行一个请求，包含时间、method/path、client、状态码、耗时、headers 和 JSON body。`Authorization` / `Proxy-Authorization` 不会写入日志。

也可以通过环境变量开启：

```bash
CODEX_JSON_LLM_REQUEST_LOG=logs/requests.jsonl llm serve
```

## 设计

这个项目分成两层：

- CLI 层：`src/codex_json_cli/cli.py` 负责解析 `llm ask` 参数、读取 stdin、打印 JSON。
- Runner 层：`src/codex_json_cli/runner.py` 负责拼出 `codex exec` 命令、执行子进程、读取 Codex 最后一条回复。
- OpenAI 兼容层：`src/codex_json_cli/openai_compat.py` 负责把 Codex 回复转换成 Chat Completion object。
- API 层：`src/codex_json_cli/server.py` 负责提供 `/v1/chat/completions` 和 `/v1/models`。

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
subprocess.run(["codex", "exec", ...])
  ↓
openai_compat.py::to_chat_completion()
  ↓
server.py 返回 OpenAI-compatible JSON
```

实际调用形式大致是：

```bash
codex exec --cd "$PWD" --output-last-message /tmp/codex-answer.txt --color never "问题"
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

## 开发

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

核心文件：

- `src/codex_json_cli/cli.py`：命令行参数和 JSON 输出。
- `src/codex_json_cli/runner.py`：调用 `codex exec` 并读取最终答案。
- `src/codex_json_cli/openai_compat.py`：OpenAI Chat Completions 兼容响应格式。
- `src/codex_json_cli/server.py`：OpenAI-compatible HTTP API service。
- `setup.cfg` / `setup.py`：包元数据和 `llm` console script 入口。
- `tests/test_cli.py`：不真实调用 Codex 的单元测试。
