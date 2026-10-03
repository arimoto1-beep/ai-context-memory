"""LLM呼び出し。Claude Code CLI (`claude -p`) をサブプロセスとして実行する。"""

import json
import os
import shutil
import subprocess

TIMEOUT_SECONDS = 180

SYSTEM_PROMPT = (
    "あなたはチャットアシスタントです。"
    "入力は [user] と [assistant] の見出しで区切られたこれまでの会話です。"
    "最後の [user] の発言に対する応答の本文だけを返してください。見出しは付けないでください。"
)

# これらが設定されているとCLIがサブスクリプションではなくAPI課金で動くため、子プロセスには渡さない
API_BILLING_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class LLMError(Exception):
    """LLM呼び出しの失敗。メッセージはそのままユーザーに表示できる。"""


def format_prompt(messages):
    return "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)


class ClaudeCLI:
    def __init__(self, model=None):
        # 未指定ならClaude Code側の既定モデルを使う
        self.model = model or os.environ.get("ACM_MODEL")

    def complete(self, messages):
        """会話履歴全体を渡し、アシスタントの応答テキストを返す。"""
        executable = shutil.which("claude")
        if executable is None:
            raise LLMError(
                "claude コマンドが見つかりません。Claude Code をインストールし、PATHを確認してください。"
            )

        command = [
            executable,
            "-p",
            "--output-format", "json",
            "--system-prompt", SYSTEM_PROMPT,
            "--tools", "",  # 素のチャットにするためツールをすべて無効化
            "--safe-mode",  # CLAUDE.md・MCP・フックなど利用者固有の設定を読み込まない
            "--no-session-persistence",
        ]
        if self.model:
            command += ["--model", self.model]

        env = {k: v for k, v in os.environ.items() if k not in API_BILLING_ENV_VARS}

        try:
            completed = subprocess.run(
                command,
                input=format_prompt(messages),
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(
                f"claude コマンドが {TIMEOUT_SECONDS} 秒以内に応答しませんでした。"
            ) from e
        except OSError as e:
            raise LLMError(f"claude コマンドを起動できませんでした: {e}") from e

        # 失敗時もstdoutにJSONが出ることがある（未ログイン時など）
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            result = None
        if not isinstance(result, dict):
            result = {}
        text = result.get("result") or ""

        if completed.returncode != 0 or result.get("is_error"):
            detail = text or completed.stderr.strip() or completed.stdout.strip() or "詳細不明"
            if "login" in detail.lower() or "logged in" in detail.lower():
                raise LLMError(
                    "Claude Code にログインしていません。"
                    "ターミナルで `claude auth login` を実行してください。"
                    f"（{detail}）"
                )
            raise LLMError(
                f"claude コマンドが異常終了しました (終了コード {completed.returncode}): {detail}"
            )

        if not text:
            raise LLMError("claude コマンドから応答テキストを取得できませんでした。")
        return text
