"""CLIの対話ループ。"""

import os
import sys
from pathlib import Path

from .chat import ChatSession
from .history import HistoryError
from .llm import ClaudeCLI, LLMError
from .memory import MemoryStoreError
from .structured_memory import OP_REPLACE, StructuredMemoryError
from .working_memory import OP_ADD, OP_REMOVE, WorkingMemoryError, count_items

EXIT_COMMANDS = {"exit", "quit"}

# 起動時のカレントディレクトリからの相対パス。data/ はGit管理対象外
DEFAULT_HISTORY_FILE = Path("data") / "conversation.jsonl"
DEFAULT_MEMORY_FILE = Path("data") / "memories.jsonl"
DEFAULT_WORKING_MEMORY_FILE = Path("data") / "working_memory.json"
DEFAULT_WORKING_MEMORY_HISTORY_FILE = Path("data") / "working_memory_history.jsonl"
DEFAULT_MEMORY_STATE_FILE = Path("data") / "long_term_memory_state.json"
DEFAULT_MEMORY_HISTORY_FILE = Path("data") / "long_term_memory_history.jsonl"


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
            retrieval = session.retrieve(user_text)
            for queries, count in retrieval.history_log:
                output_fn(f"[履歴検索] {' / '.join(queries)}")
                output_fn(f"[履歴検索結果] {count}件")
            for queries, count in retrieval.memory_history_log:
                output_fn(f"[長期履歴検索] {' / '.join(queries)}")
                output_fn(f"[長期履歴検索結果] {count}件")
            for number, (queries, count) in enumerate(retrieval.search_log):
                output_fn(f"[{'再検索' if number else '検索'}] {' / '.join(queries)}")
                output_fn(f"[検索結果] {count}件")
            for warning in retrieval.warnings:
                output_fn(f"[警告] {warning}")
            reply = session.send(
                user_text,
                retrieval.memories,
                retrieval.history_events,
                retrieval.memory_state,
                retrieval.memory_history_events,
            )
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

        # 構造化した長期記憶の抽出も同様に、失敗しても警告だけで続行する
        try:
            changes, warnings = session.update_memory_state(user_text)
        except KeyboardInterrupt:
            output_fn("\n[長期状態の抽出を中断しました]")
            continue
        for line in format_memory_state_changes(changes):
            output_fn(f"[長期状態] {line}")
        for warning in warnings:
            output_fn(f"[警告] {warning}")

        # 作業記憶の抽出も同様に、失敗しても警告だけで続行する
        try:
            changes, warnings = session.update_working_memory(user_text)
        except KeyboardInterrupt:
            output_fn("\n[作業記憶の抽出を中断しました]")
            continue
        for line in format_working_memory_changes(changes):
            output_fn(f"[作業記憶] {line}")
        for warning in warnings:
            output_fn(f"[警告] {warning}")

    output_fn("終了します。")


def format_working_memory_changes(changes):
    """作業記憶の変更を表示用の行にする。追加はフィールドごとに1行、置き換え・削除は1件ごとに1行。"""
    added = {}
    lines = []
    for change in changes:
        if change["operation"] == OP_ADD:
            added.setdefault(change["field"], []).append(change["text"])
        elif change["operation"] == OP_REMOVE:
            lines.append(f"{change['field']}: {change['text']} → 削除")
        else:
            lines.append(f"{change['field']}: {change['target']} → {change['text']}")
    return [f"{field}: {' / '.join(texts)}" for field, texts in added.items()] + lines


def format_memory_state_changes(changes):
    """構造化した長期記憶の変更を表示用の行にする。追加は新しい値、置き換えは「古い値 → 新しい値」。"""
    lines = []
    for change in changes:
        value = change["value"]
        if change["operation"] == OP_REPLACE:
            value = f"{change['old_value']} → {value}"
        lines.append(f"{change['subject']}.{change['key']}: {value}")
    return lines


def main():
    # Windowsでパイプ/リダイレクト時にcp932へ落ちて日本語が化けるのを防ぐ。
    # utf-8-sigはPowerShellがパイプ入力の先頭に付けるBOMを取り除くため
    sys.stdin.reconfigure(encoding="utf-8-sig")
    sys.stdout.reconfigure(encoding="utf-8")
    history_path = Path(os.environ.get("ACM_HISTORY_FILE") or DEFAULT_HISTORY_FILE)
    memory_path = Path(os.environ.get("ACM_MEMORY_FILE") or DEFAULT_MEMORY_FILE)
    working_memory_path = Path(
        os.environ.get("ACM_WORKING_MEMORY_FILE") or DEFAULT_WORKING_MEMORY_FILE
    )
    working_memory_history_path = Path(
        os.environ.get("ACM_WORKING_MEMORY_HISTORY_FILE") or DEFAULT_WORKING_MEMORY_HISTORY_FILE
    )
    memory_state_path = Path(
        os.environ.get("ACM_LONG_TERM_MEMORY_STATE_FILE") or DEFAULT_MEMORY_STATE_FILE
    )
    memory_history_path = Path(
        os.environ.get("ACM_LONG_TERM_MEMORY_HISTORY_FILE") or DEFAULT_MEMORY_HISTORY_FILE
    )
    try:
        session = ChatSession(
            ClaudeCLI(),
            history_path,
            memory_path,
            working_memory_path,
            working_memory_history_path,
            memory_state_path,
            memory_history_path,
        )
    except (MemoryStoreError, WorkingMemoryError, StructuredMemoryError) as e:
        print(f"[エラー] {e}")
        return 1
    if session.memories:
        print(f"長期記憶を読み込みました（{len(session.memories)} 件）。")
    if session.memory_state:
        print(f"長期状態を読み込みました（{len(session.memory_state)}項目）。")
    if count_items(session.working_memory):
        print(f"作業記憶を読み込みました（{count_items(session.working_memory)}項目）。")
    run(session)
    return 0
