"""会話履歴のファイル保存。JSONL形式で、1行が1メッセージ。"""

import json

from .structured_memory import PartialWriteError, append_text

ROLES = ("user", "assistant")


class HistoryError(Exception):
    """履歴ファイルの読み書きの失敗。メッセージはそのままユーザーに表示できる。

    partial が真なら、追記の途中で失敗し、途中まで書かれた行を取り除けなかった（ファイルの末尾が壊れている可能性がある）。
    """

    def __init__(self, message, partial=False):
        super().__init__(message)
        self.partial = partial


def load_history(path):
    """保存済みの履歴を読み込む。ファイルが無い・空の場合は空のリストを返す。"""
    try:
        # utf-8-sigはエディタで保存し直したときに付くBOMを取り除くため
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        raise HistoryError(f"会話履歴ファイルを読み込めませんでした: {path} ({e})") from e

    messages = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            raise HistoryError(
                f"会話履歴ファイルの {number} 行目がJSONとして読めません: {path} ({e.msg})"
            ) from e
        if (
            not isinstance(message, dict)
            or message.get("role") not in ROLES
            or not isinstance(message.get("content"), str)
        ):
            raise HistoryError(
                f"会話履歴ファイルの {number} 行目の形式が不正です: {path} "
                '（{"role": "user" または "assistant", "content": "..."} が必要です）'
            )
        messages.append({"role": message["role"], "content": message["content"]})
    return messages


def append_history(path, messages):
    """メッセージを履歴ファイルの末尾へ追記する。途中で失敗したら追記する前の長さに戻す。"""
    text = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
    try:
        append_text(path, text)
    except PartialWriteError as e:
        raise HistoryError(f"会話履歴を保存できませんでした: {e}", partial=True) from e
    except OSError as e:
        raise HistoryError(f"会話履歴を保存できませんでした: {path} ({e})") from e
