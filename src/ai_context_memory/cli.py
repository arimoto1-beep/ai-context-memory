"""CLIの対話ループ。"""

import os
import sys
from pathlib import Path

from .chat import ChatSession
from .history import HistoryError
from .llm import ClaudeCLI, LLMError

EXIT_COMMANDS = {"exit", "quit"}

# 起動時のカレントディレクトリからの相対パス。data/ はGit管理対象外
DEFAULT_HISTORY_FILE = Path("data") / "conversation.jsonl"


def run(session, input_fn=input, output_fn=print):
    output_fn("チャットを開始します。終了するには exit または quit と入力してください。")
    while True:
        try:
            user_text = input_fn("\nあなた> ").strip()
        except (EOFError, KeyboardInterrupt):
            output_fn("")
            break

        if not user_text:
            continue
        if user_text.lower() in EXIT_COMMANDS:
            break

        try:
            reply = session.send(user_text)
        except LLMError as e:
            output_fn(f"[エラー] {e}")
            continue
        except HistoryError as e:
            # 応答は得られているので表示し、保存できなかったことを伝える
            output_fn(f"\nAI> {e.reply}")
            output_fn(f"[エラー] {e}")
            continue
        except KeyboardInterrupt:
            output_fn("\n[中断しました]")
            continue
        output_fn(f"\nAI> {reply}")

    output_fn("終了します。")


def main():
    # Windowsでパイプ/リダイレクト時にcp932へ落ちて日本語が化けるのを防ぐ。
    # utf-8-sigはPowerShellがパイプ入力の先頭に付けるBOMを取り除くため
    sys.stdin.reconfigure(encoding="utf-8-sig")
    sys.stdout.reconfigure(encoding="utf-8")
    history_path = Path(os.environ.get("ACM_HISTORY_FILE") or DEFAULT_HISTORY_FILE)
    try:
        session = ChatSession(ClaudeCLI(), history_path)
    except HistoryError as e:
        print(f"[エラー] {e}")
        return 1
    if session.messages:
        print(f"前回までの会話を読み込みました（{len(session.messages)} 件）。")
    run(session)
    return 0
