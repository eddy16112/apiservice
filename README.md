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

## 设计

这个项目分成两层：

- CLI 层：`src/codex_json_cli/cli.py` 负责解析 `llm ask` 参数、读取 stdin、打印 JSON。
- Runner 层：`src/codex_json_cli/runner.py` 负责拼出 `codex exec` 命令、执行子进程、读取 Codex 最后一条回复。
- OpenAI 兼容层：`src/codex_json_cli/openai_compat.py` 负责把 Codex 回复转换成 Chat Completion object。

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
- `setup.cfg` / `setup.py`：包元数据和 `llm` console script 入口。
- `tests/test_cli.py`：不真实调用 Codex 的单元测试。
