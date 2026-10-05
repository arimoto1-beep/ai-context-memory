"""CLIの対話ループ。"""

import os
import sys
from pathlib import Path

from .chat import ChatSession
from .history import HistoryError
from .llm import ClaudeCLI, LLMError
from .memory import MemoryStoreError

EXIT_COMMANDS = {"exit", "quit"}

# 起動時のカレントディレクトリからの相対パス。data/ はGit管理対象外
DEFAULT_HISTORY_FILE = Path("data") / "conversation.jsonl"
DEFAULT_MEMORY_FILE = Path("data") / "memories.jsonl"


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
            # 検索は会話本体より優先度が低いので、失敗しても警告だけで記憶なしの回答へ進む
            found, search_log, warnings = session.recall(user_text)
            for number, (queries, count) in enumerate(search_log):
                output_fn(f"[{'再検索' if number else '検索'}] {' / '.join(queries)}")
                output_fn(f"[検索結果] {count}件")
            for warning in warnings:
                output_fn(f"[警告] {warning}")
            reply = session.send(user_text, found)
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

        # 記憶抽出は会話本体より優先度が低いので、失敗しても警告だけで続行する
        try:
            saved, warnings = session.remember(user_text)
        except KeyboardInterrupt:
            output_fn("\n[記憶抽出を中断しました]")
            continue
        for memory in saved:
            output_fn(f"[記憶] {memory['text']}")
        for warning in warnings:
            output_fn(f"[警告] {warning}")

    output_fn("終了します。")


def main():
    # Windowsでパイプ/リダイレクト時にcp932へ落ちて日本語が化けるのを防ぐ。
    # utf-8-sigはPowerShellがパイプ入力の先頭に付けるBOMを取り除くため
    sys.stdin.reconfigure(encoding="utf-8-sig")
    sys.stdout.reconfigure(encoding="utf-8")
    history_path = Path(os.environ.get("ACM_HISTORY_FILE") or DEFAULT_HISTORY_FILE)
    memory_path = Path(os.environ.get("ACM_MEMORY_FILE") or DEFAULT_MEMORY_FILE)
    try:
        session = ChatSession(ClaudeCLI(), history_path, memory_path)
    except MemoryStoreError as e:
        print(f"[エラー] {e}")
        return 1
    if session.memories:
        print(f"長期記憶を読み込みました（{len(session.memories)} 件）。")
    run(session)
    return 0
