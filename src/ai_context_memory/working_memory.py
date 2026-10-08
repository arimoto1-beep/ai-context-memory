"""作業記憶（Working Memory）。現在進行中の作業で忘れてはいけない情報を、1つのJSONファイルに保存する。

長期記憶と違って検索せず、回答のたびに全項目をLLMへ渡す。
何を入れるか・何を消すかの判断はAIに任せ、ここではAIが提案した候補の検証と状態の書き換えだけを行う。
現在状態（working_memory.json）とは別に、変更履歴を JSONL へ追記する。
履歴は毎回は渡さず、AIが必要と判断したときだけ検索して、ヒットしたイベントをLLMへ渡す（検索は search.py）。
"""

import json
from datetime import datetime

from .memory import strip_code_fence
from .search import normalize

# フィールドと、LLMへ渡すときの見出し
FIELDS = {
    "mission": "Mission",
    "scope": "Scope",
    "acceptance_criteria": "Acceptance Criteria",
}

# 作業記憶に対する状態変更
OP_ADD = "add"
OP_REPLACE = "replace"
OP_REMOVE = "remove"
OPERATIONS = (OP_ADD, OP_REPLACE, OP_REMOVE)


class WorkingMemoryError(Exception):
    """作業記憶の抽出結果の解析・ファイルの読み書きの失敗。メッセージはそのままユーザーに表示できる。"""


def empty_working_memory():
    return {field: [] for field in FIELDS}


def count_items(working_memory):
    return sum(len(items) for items in working_memory.values())


def load_working_memory(path):
    """保存済みの作業記憶を読み込む。ファイルが無い・空の場合は空の作業記憶を返す。"""
    try:
        # utf-8-sigはエディタで保存し直したときに付くBOMを取り除くため
        with open(path, encoding="utf-8-sig") as f:
            raw = f.read()
    except FileNotFoundError:
        return empty_working_memory()
    except (OSError, UnicodeDecodeError) as e:
        raise WorkingMemoryError(f"作業記憶ファイルを読み込めませんでした: {path} ({e})") from e
    if not raw.strip():
        return empty_working_memory()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise WorkingMemoryError(
            f"作業記憶ファイルがJSONとして読めません: {path} ({e.lineno} 行目: {e.msg})"
        ) from e
    if not isinstance(data, dict) or not set(data) <= set(FIELDS):
        raise WorkingMemoryError(
            f"作業記憶ファイルの形式が不正です: {path} "
            f"（キーは {' / '.join(FIELDS)} だけを持つオブジェクトが必要です）"
        )
    working_memory = empty_working_memory()
    for field, items in data.items():
        if not isinstance(items, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and isinstance(item.get("evidence"), str)
            for item in items
        ):
            raise WorkingMemoryError(
                f"作業記憶ファイルの {field} の形式が不正です: {path} "
                '（{"text": "...", "evidence": "..."} の配列が必要です）'
            )
        working_memory[field] = [
            {"text": item["text"], "evidence": item["evidence"]} for item in items
        ]
    return working_memory


def save_working_memory(path, working_memory):
    """作業記憶全体をファイルへ書き出す。"""
    text = json.dumps(working_memory, ensure_ascii=False, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise WorkingMemoryError(f"作業記憶を保存できませんでした: {path} ({e})") from e


def parse_candidates(raw):
    """抽出AIの出力を作業記憶の候補のリストとして解析する。JSON配列でなければ失敗とする。"""
    try:
        candidates = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as e:
        raise WorkingMemoryError(f"抽出結果がJSONとして読めません ({e.msg})") from e
    if not isinstance(candidates, list):
        raise WorkingMemoryError("抽出結果がJSON配列ではありません")
    return candidates


def _same(a, b):
    return normalize(" ".join(a.split())) == normalize(" ".join(b.split()))


def normalize_candidate(candidate):
    """候補を operation 付きの形にそろえる。

    MVP5 形式（operation なし）は、replaces があれば replace（replaces を target とする）、なければ add とみなす。
    """
    if not isinstance(candidate, dict) or "operation" in candidate:
        return candidate
    if candidate.get("replaces") is None:
        return {**candidate, "operation": OP_ADD}
    normalized = {k: v for k, v in candidate.items() if k != "replaces"}
    return {**normalized, "operation": OP_REPLACE, "target": candidate["replaces"]}


def _matching_indexes(items, target):
    return [i for i, item in enumerate(items) if _same(item["text"], target)]


def rejection_reason(candidate, user_text, working_memory):
    """候補を採用してよければ None、採用できなければその理由を返す。candidate は normalize_candidate 済みとする。"""
    if not isinstance(candidate, dict):
        return "形式が不正です"
    operation = candidate.get("operation")
    field = candidate.get("field")
    text = candidate.get("text")
    target = candidate.get("target")
    evidence = candidate.get("evidence")
    if operation not in OPERATIONS:
        return f"operation が不正です: {operation}"
    if field not in FIELDS:
        return f"field が不正です: {field}"
    if operation != OP_REMOVE and (not isinstance(text, str) or not text.strip()):
        return "text がありません"
    if not isinstance(evidence, str) or not evidence.strip():
        return "evidence がありません"
    # AIが根拠を作り出していないことを、元の発言との照合で確かめる
    if evidence not in user_text:
        return f"evidence が元の発言に含まれていません: {evidence}"
    if operation != OP_ADD:
        if not isinstance(target, str) or not target.strip():
            return "target がありません"
        # 部分一致や似た項目では変更しない。AIが書いた target は既存の文面と一致したときだけ使う
        matches = _matching_indexes(working_memory[field], target)
        if not matches:
            return f"target が {field} の既存の項目と一致しません: {target}"
        if len(matches) > 1:
            return f"target が {field} の複数の項目と一致するため特定できません: {target}"
    return None


def apply_candidates(working_memory, candidates, user_text):
    """候補を検証して作業記憶へ反映し、(新しい作業記憶, 変更のリスト, 警告メッセージ) を返す。

    元の working_memory は書き換えない。
    変更は履歴に残すイベントと同じ形で、operation / field / text / evidence と、replace のときだけ target を持つ。
    text は add なら追加した項目、replace なら置き換え後の項目、remove なら削除した項目の文面。
    """
    updated = {field: list(items) for field, items in working_memory.items()}
    changes = []
    warnings = []
    for candidate in candidates:
        candidate = normalize_candidate(candidate)
        reason = rejection_reason(candidate, user_text, updated)
        if reason:
            warnings.append(f"作業記憶の候補を保存しませんでした: {reason}")
            continue
        operation = candidate["operation"]
        field = candidate["field"]
        evidence = candidate["evidence"]
        items = updated[field]
        if operation == OP_REMOVE:
            [index] = _matching_indexes(items, candidate["target"])
            removed = items.pop(index)
            changes.append(
                {"operation": operation, "field": field, "text": removed["text"], "evidence": evidence}
            )
            continue
        text = candidate["text"].strip()
        if any(_same(item["text"], text) for item in items):
            continue
        item = {"text": text, "evidence": evidence}
        if operation == OP_ADD:
            items.append(item)
            changes.append({"operation": operation, "field": field, "text": text, "evidence": evidence})
        else:
            [index] = _matching_indexes(items, candidate["target"])
            changes.append(
                {
                    "operation": operation,
                    "field": field,
                    "target": items[index]["text"],
                    "text": text,
                    "evidence": evidence,
                }
            )
            items[index] = item
    return updated, changes, warnings


def append_working_memory_history(path, changes, now=None):
    """作業記憶の変更を、1行1イベントで履歴ファイルへ追記する。現在状態とは別に、消した項目も残すため。"""
    timestamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    lines = "".join(
        json.dumps({"timestamp": timestamp, **change}, ensure_ascii=False) + "\n"
        for change in changes
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(lines)
    except OSError as e:
        raise WorkingMemoryError(f"作業記憶の変更履歴を保存できませんでした: {path} ({e})") from e


def load_working_memory_history(path):
    """履歴ファイルを全イベント読み込む。ファイルが無ければ空のリストを返す。

    回答用のLLMへは、ここから検索でヒットしたイベントだけを渡す。
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        raise WorkingMemoryError(f"作業記憶の変更履歴を読み込めませんでした: {path} ({e})") from e

    events = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            raise WorkingMemoryError(
                f"作業記憶の変更履歴がJSONとして読めません: {path} ({number} 行目: {e.msg})"
            ) from e
        if not isinstance(event, dict):
            raise WorkingMemoryError(
                f"作業記憶の変更履歴の形式が不正です: {path} ({number} 行目: JSONオブジェクトが必要です)"
            )
        events.append(event)
    return events
