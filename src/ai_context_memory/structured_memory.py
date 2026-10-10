"""構造化した長期記憶。ユーザーについての属性を subject / key / value で持ち、現在値と変更履歴を分けて保存する。

subject + key が意味上の住所で、1つの住所には現在値が1つだけある。
どの key に分類するかの判断はAIに任せ、ここではAIが提案した候補の検証と、
追加・置き換え・変更なしの判定、状態の書き換えだけを行う。
現在値（long_term_memory_state.json）とは別に、変更履歴を JSONL へ追記する。
従来の長期記憶（memories.jsonl）とは別のレイヤーで、そちらは変更しない。
"""

import json
import re
from datetime import datetime

from .memory import strip_code_fence
from .search import normalize

# 今回扱う subject はユーザー自身だけ
SUBJECT_USER = "user"
ALLOWED_SUBJECTS = (SUBJECT_USER,)

# key の形式。意味はAIが決め、ここでは形だけを確かめる
KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]*")
MAX_KEY_LENGTH = 64

OP_ADD = "add"
OP_REPLACE = "replace"


class StructuredMemoryError(Exception):
    """構造化した長期記憶の抽出結果の解析・ファイルの読み書きの失敗。メッセージはそのままユーザーに表示できる。"""


def timestamp_now(now=None):
    """現在値の updated_at と履歴の timestamp に使う時刻。作業記憶の変更履歴と同じ形式。"""
    return (now or datetime.now().astimezone()).isoformat(timespec="seconds")


def _address(item):
    return item["subject"], item["key"]


def load_memory_state(path):
    """保存済みの現在値を読み込む。ファイルが無い・空の場合は空のリストを返す。"""
    try:
        # utf-8-sigはエディタで保存し直したときに付くBOMを取り除くため
        with open(path, encoding="utf-8-sig") as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        raise StructuredMemoryError(f"長期状態ファイルを読み込めませんでした: {path} ({e})") from e
    if not raw.strip():
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StructuredMemoryError(
            f"長期状態ファイルがJSONとして読めません: {path} ({e.lineno} 行目: {e.msg})"
        ) from e
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not all(
        isinstance(item, dict)
        and all(isinstance(item.get(k), str) for k in ("subject", "key", "value", "evidence"))
        and isinstance(item.get("updated_at", ""), str)
        for item in items
    ):
        raise StructuredMemoryError(
            f"長期状態ファイルの形式が不正です: {path} "
            '（{"items": [{"subject": "...", "key": "...", "value": "...", "evidence": "..."}]} が必要です）'
        )
    seen = set()
    for item in items:
        if _address(item) in seen:
            raise StructuredMemoryError(
                f"長期状態ファイルに同じ subject + key が複数あります: {path} "
                f"({item['subject']}.{item['key']})"
            )
        seen.add(_address(item))
    keys = ("subject", "key", "value", "evidence", "updated_at")
    return [{k: item[k] for k in keys if k in item} for item in items]


def save_memory_state(path, items):
    """現在値の全項目をファイルへ書き出す。"""
    text = json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise StructuredMemoryError(f"長期状態を保存できませんでした: {path} ({e})") from e


def parse_candidates(raw):
    """抽出AIの出力を候補のリストとして解析する。JSON配列でなければ失敗とする。"""
    try:
        candidates = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as e:
        raise StructuredMemoryError(f"抽出結果がJSONとして読めません ({e.msg})") from e
    if not isinstance(candidates, list):
        raise StructuredMemoryError("抽出結果がJSON配列ではありません")
    return candidates


def rejection_reason(candidate, user_text):
    """候補を採用してよければ None、採用できなければその理由を返す。value の意味が正しいかは判断しない。"""
    if not isinstance(candidate, dict):
        return "形式が不正です"
    subject = candidate.get("subject")
    key = candidate.get("key")
    value = candidate.get("value")
    evidence = candidate.get("evidence")
    if subject not in ALLOWED_SUBJECTS:
        return f"subject が不正です: {subject}"
    if not isinstance(key, str) or not key.strip():
        return "key がありません"
    if len(key.strip()) > MAX_KEY_LENGTH or not KEY_PATTERN.fullmatch(key.strip()):
        return f"key の形式が不正です: {key}"
    if not isinstance(value, str) or not value.strip():
        return "value がありません"
    if not isinstance(evidence, str) or not evidence.strip():
        return "evidence がありません"
    # AIが根拠を作り出していないことを、元の発言との照合で確かめる
    if evidence not in user_text:
        return f"evidence が元の発言に含まれていません: {evidence}"
    return None


def _same_value(a, b):
    return normalize(" ".join(a.split())) == normalize(" ".join(b.split()))


def apply_candidates(items, candidates, user_text, timestamp):
    """候補を検証して現在値へ反映し、(新しい現在値, 変更のリスト, 警告メッセージ) を返す。

    元の items は書き換えない。追加か置き換えかはAIに決めさせず、ここで subject + key を見て決める。
    現在値が無ければ add、あって value が同じなら何もしない、value が違えば replace。
    変更は履歴に残すイベントと同じ形で、operation / subject / key / value / evidence と、
    replace のときだけ old_value を持つ。timestamp は変更した項目の updated_at になる。
    """
    updated = list(items)
    changes = []
    warnings = []
    proposed = set()
    for candidate in candidates:
        reason = rejection_reason(candidate, user_text)
        if reason:
            warnings.append(f"長期状態の候補を保存しませんでした: {reason}")
            continue
        subject = candidate["subject"]
        key = candidate["key"].strip()
        value = candidate["value"].strip()
        if (subject, key) in proposed:
            # 1つの発言の中で、同じ項目を自分自身で置き換えない
            warnings.append(
                f"長期状態の候補を保存しませんでした: 同じ発言に {subject}.{key} の候補が複数あります"
            )
            continue
        proposed.add((subject, key))
        item = {
            "subject": subject,
            "key": key,
            "value": value,
            "evidence": candidate["evidence"],
            "updated_at": timestamp,
        }
        index = next((i for i, c in enumerate(updated) if _address(c) == (subject, key)), None)
        change = {"subject": subject, "key": key, "value": value, "evidence": candidate["evidence"]}
        if index is None:
            updated.append(item)
            changes.append({"operation": OP_ADD, **change})
        elif not _same_value(updated[index]["value"], value):
            old_value = updated[index]["value"]
            updated[index] = item
            changes.append(
                {
                    "operation": OP_REPLACE,
                    "subject": subject,
                    "key": key,
                    "old_value": old_value,
                    "value": value,
                    "evidence": candidate["evidence"],
                }
            )
    return updated, changes, warnings


def append_memory_history(path, changes, timestamp):
    """現在値の変更を、1行1イベントで履歴ファイルへ追記する。置き換えた古い値を残すため。"""
    lines = "".join(
        json.dumps({"timestamp": timestamp, **change}, ensure_ascii=False) + "\n"
        for change in changes
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(lines)
    except OSError as e:
        raise StructuredMemoryError(f"長期状態の変更履歴を保存できませんでした: {path} ({e})") from e


def load_memory_history(path):
    """履歴ファイルを全イベント読み込む。ファイルが無ければ空のリストを返す。

    回答用のLLMへは、ここから検索でヒットしたイベントだけを渡す。
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        raise StructuredMemoryError(f"長期状態の変更履歴を読み込めませんでした: {path} ({e})") from e

    events = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            raise StructuredMemoryError(
                f"長期状態の変更履歴がJSONとして読めません: {path} ({number} 行目: {e.msg})"
            ) from e
        if not isinstance(event, dict):
            raise StructuredMemoryError(
                f"長期状態の変更履歴の形式が不正です: {path} ({number} 行目: JSONオブジェクトが必要です)"
            )
        events.append(event)
    return events
