"""作業記憶（Working Memory）。現在進行中の作業で忘れてはいけない情報を、1つのJSONファイルに保存する。

長期記憶と違って検索せず、回答のたびに全項目をLLMへ渡す。
何を入れるかの判断はAIに任せ、ここではAIが提案した候補の検証と状態の書き換えだけを行う。
"""

import json

from .memory import strip_code_fence
from .search import normalize

# フィールドと、LLMへ渡すときの見出し
FIELDS = {
    "mission": "Mission",
    "scope": "Scope",
    "acceptance_criteria": "Acceptance Criteria",
}


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


def rejection_reason(candidate, user_text, working_memory):
    """候補を採用してよければ None、採用できなければその理由を返す。"""
    if not isinstance(candidate, dict):
        return "形式が不正です"
    field = candidate.get("field")
    text = candidate.get("text")
    evidence = candidate.get("evidence")
    replaces = candidate.get("replaces")
    if field not in FIELDS:
        return f"field が不正です: {field}"
    if not isinstance(text, str) or not text.strip():
        return "text がありません"
    if not isinstance(evidence, str) or not evidence.strip():
        return "evidence がありません"
    # AIが根拠を作り出していないことを、元の発言との照合で確かめる
    if evidence not in user_text:
        return f"evidence が元の発言に含まれていません: {evidence}"
    if replaces is not None:
        if not isinstance(replaces, str) or not any(
            _same(item["text"], replaces) for item in working_memory[field]
        ):
            return f"replaces が {field} の既存の項目と一致しません: {replaces}"
    return None


def apply_candidates(working_memory, candidates, user_text):
    """候補を検証して作業記憶へ反映し、(新しい作業記憶, 変更のリスト, 警告メッセージ) を返す。

    元の working_memory は書き換えない。
    変更は {"field", "text", "replaced"} で、replaced は置き換えた項目の文面（追加なら None）。
    """
    updated = {field: list(items) for field, items in working_memory.items()}
    changes = []
    warnings = []
    for candidate in candidates:
        reason = rejection_reason(candidate, user_text, updated)
        if reason:
            warnings.append(f"作業記憶の候補を保存しませんでした: {reason}")
            continue
        field = candidate["field"]
        text = candidate["text"].strip()
        items = updated[field]
        if any(_same(item["text"], text) for item in items):
            continue
        item = {"text": text, "evidence": candidate["evidence"]}
        replaces = candidate.get("replaces")
        if replaces is None:
            items.append(item)
            changes.append({"field": field, "text": text, "replaced": None})
        else:
            index = next(i for i, old in enumerate(items) if _same(old["text"], replaces))
            changes.append({"field": field, "text": text, "replaced": items[index]["text"]})
            items[index] = item
    return updated, changes, warnings
