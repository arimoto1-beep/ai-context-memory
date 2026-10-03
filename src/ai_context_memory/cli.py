"""CLIの対話ループ。"""

import sys

from .chat import ChatSession
from .llm import ClaudeCLI, LLMError

EXIT_COMMANDS = {"exit", "quit"}


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
    run(ChatSession(ClaudeCLI()))
    return 0
