"""構造化した長期記憶。ユーザーについての属性を subject / key / value で持ち、現在値と変更履歴を分けて保存する。

subject + key が意味上の住所で、1つの住所には現在値が1つだけある。
どの key に分類するか、新しい発言が既存の値とどういう関係にあるか（replace / coexist / pending / no_change）は
AIが判断する。ここではAIの判断を検証し、許された状態遷移だけを実行する。
AIが replace と判断しても、既存の値の終了・訂正を発言の原文で確かめられなければ置き換えず、保留（pending）にする。
現在値（long_term_memory_state.json）とは別に、変更履歴を JSONL へ追記し、保留を別のファイルへ保存する。
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

# AIが判断する、新しい発言と既存の値の関係
DECISION_REPLACE = "replace"
DECISION_COEXIST = "coexist"
DECISION_PENDING = "pending"
DECISION_NO_CHANGE = "no_change"
DECISIONS = (DECISION_REPLACE, DECISION_COEXIST, DECISION_PENDING, DECISION_NO_CHANGE)

# Pythonが実行した結果。add / replace は現在値の変更で履歴に残る。pending は保留で、現在値も履歴も変えない
OP_ADD = "add"
OP_REPLACE = "replace"
OP_PENDING = "pending"

# replace を自動で実行してよいかの判断に使う、AIの自己申告の値。許可の根拠にはせず、止める方向にだけ使う
OLD_VALUE_ENDED_OR_WRONG = ("ended", "wrong")
TEMPORAL_SCOPE_CURRENT = "current"

# 既存の値の終了・変更・訂正を述べるときに現れる表現。意味の判定には使わない。
# replace を自動で実行するための必要条件で、これが無ければ置き換えずに保留にするだけ
ENDING_EXPRESSIONS = (
    "引き払",
    "引っ越",
    "転居",
    "退去",
    "離れ",
    "辞め",
    "やめ",
    "退職",
    "転職",
    "終わ",
    "変わ",
    "変え",
    "間違",
    "誤り",
    "訂正",
)

PENDING_STATUS_UNRESOLVED = "unresolved"
# 保留の1項目が持つキー（保存する順）。id / subject / evidence / created_at は必須、それ以外は文字列か null
PENDING_KEYS = (
    "id",
    "subject",
    "target_key",
    "target_value",
    "proposed_key",
    "proposed_value",
    "evidence",
    "decision",
    "hold_reason",
    "reason",
    "confirmation_question",
    "status",
    "created_at",
)
PENDING_REQUIRED_KEYS = ("id", "subject", "evidence", "created_at")


class StructuredMemoryError(Exception):
    """構造化した長期記憶の抽出結果の解析・ファイルの読み書きの失敗。メッセージはそのままユーザーに表示できる。"""


def timestamp_now(now=None):
    """現在値の updated_at、履歴の timestamp、保留の created_at に使う時刻。作業記憶の変更履歴と同じ形式。"""
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


def _recover_json(text):
    """JSON以外の前置きが付いた出力から、末尾にある唯一のJSONの値を取り出す。取り出せなければ失敗とする。

    出力の中で `{` か `[` から始まり、それ自体で完結するJSONの値を左から探す。
    見つかったのがちょうど1つで、その後ろに空白しか無いときだけ使う。
    複数あればどれも選ばず、途中を切り貼りして直すこともしない。
    """
    decoder = json.JSONDecoder()
    found = []
    index = 0
    while index < len(text):
        if text[index] not in "[{":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        found.append((value, end))
        index = end
    if not found:
        raise StructuredMemoryError("抽出結果がJSONとして読めません (JSONが見つかりません)")
    if len(found) > 1:
        raise StructuredMemoryError("抽出結果がJSONとして読めません (JSONの候補が複数あります)")
    value, end = found[0]
    if text[end:].strip():
        raise StructuredMemoryError("抽出結果がJSONとして読めません (JSONの後ろに別の出力があります)")
    return value


def parse_extraction(raw):
    """抽出AIの出力を (候補のリスト, JSON以外の前置きを除いて取り出したか) として解析する。

    JSON配列でなければ失敗とする。通常の解析に失敗したときだけ、末尾にある唯一のJSONを取り出す。
    """
    text = strip_code_fence(raw)
    recovered = False
    try:
        candidates = json.loads(text)
    except json.JSONDecodeError:
        candidates = _recover_json(text)
        recovered = True
    if not isinstance(candidates, list):
        raise StructuredMemoryError("抽出結果がJSON配列ではありません")
    return candidates, recovered


def parse_candidates(raw):
    """抽出AIの出力を候補のリストとして解析する。JSON配列でなければ失敗とする。"""
    return parse_extraction(raw)[0]


def _text(candidate, name):
    """候補の文字列フィールドを前後の空白を除いて返す。空・文字列でない・無い場合は None。"""
    value = candidate.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _key_problem(candidate, name, required):
    """key のフィールドの形式に問題があればその理由を、無ければ None を返す。"""
    key = candidate.get(name)
    if key is None or (isinstance(key, str) and not key.strip()):
        return f"{name} がありません" if required else None
    if not isinstance(key, str) or len(key.strip()) > MAX_KEY_LENGTH or not KEY_PATTERN.fullmatch(key.strip()):
        return f"{name} の形式が不正です: {key}"
    return None


def rejection_reason(candidate, user_text):
    """候補を検証してよければ None、反映できなければその理由を返す。値の意味が正しいかは判断しない。"""
    if not isinstance(candidate, dict):
        return "形式が不正です"
    decision = candidate.get("decision")
    if decision not in DECISIONS:
        return f"decision が不正です: {decision}"
    subject = candidate.get("subject")
    if subject not in ALLOWED_SUBJECTS:
        return f"subject が不正です: {subject}"
    evidence = candidate.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return "evidence がありません"
    # AIが根拠を作り出していないことを、元の発言との照合で確かめる
    if evidence not in user_text:
        return f"evidence が元の発言に含まれていません: {evidence}"
    if decision == DECISION_NO_CHANGE:
        return None
    problem = _key_problem(candidate, "target_key", required=False) or _key_problem(
        candidate, "proposed_key", required=decision == DECISION_COEXIST
    )
    if problem:
        return problem
    if decision == DECISION_COEXIST and not _text(candidate, "proposed_value"):
        return "proposed_value がありません"
    value = candidate.get("proposed_value")
    if value is not None and not isinstance(value, str):
        return f"proposed_value の形式が不正です: {value}"
    return None


def _contains(haystack, needle):
    return normalize(needle) in normalize(haystack)


def replace_hold_reason(candidate, current, user_text):
    """replace を自動で実行してよければ None、置き換えずに保留にするならその理由を返す。

    current は target_key の現在値の項目（無ければ None）。AIの old_value_status と temporal_scope は
    自己申告なので、条件を満たさないときに止めるためだけに使い、満たしていても許可の根拠にはしない。
    許可の根拠は、新しい値が発言の原文に現れること、既存の値の終了・訂正を述べた部分（old_value_evidence）が
    発言の原文にあり、その中に既存の値と、終了・変更・訂正を表す表現が現れることを、ここで確かめられることだけである。
    これは意味の検証ではない。否定や、別のものの終了を述べた発言は、AIが誤れば通ってしまう。
    """
    if current is None:
        if not _text(candidate, "target_key"):
            return "置き換える既存の項目（target_key）がありません"
        return f"target_key の {candidate['subject']}.{candidate['target_key'].strip()} が現在の長期状態にありません"
    value = _text(candidate, "proposed_value")
    if not value:
        return "置き換え後の値（proposed_value）がありません"
    # 新しい値もユーザー自身が述べたものに限る（AIが作った値で現在値を消さないため）
    if not _contains(user_text, value):
        return f"置き換え後の値「{value}」が元の発言に含まれていません"
    status = candidate.get("old_value_status")
    if status not in OLD_VALUE_ENDED_OR_WRONG:
        return f"AIが既存の値の終了・訂正を示していません（old_value_status: {status}）"
    scope = candidate.get("temporal_scope")
    if scope != TEMPORAL_SCOPE_CURRENT:
        return f"現在の状態についての発言と判断されていません（temporal_scope: {scope}）"
    old_evidence = candidate.get("old_value_evidence")
    if not isinstance(old_evidence, str) or not old_evidence.strip():
        return "既存の値の終了・訂正を述べた部分（old_value_evidence）がありません"
    if old_evidence not in user_text:
        return f"old_value_evidence が元の発言に含まれていません: {old_evidence}"
    if not _contains(old_evidence, current["value"]):
        return f"old_value_evidence に既存の値「{current['value']}」が含まれていません"
    if not any(expression in old_evidence for expression in ENDING_EXPRESSIONS):
        return "old_value_evidence に終了・変更・訂正を表す表現がありません"
    return None


def _same_value(a, b):
    return normalize(" ".join(a.split())) == normalize(" ".join(b.split()))


def _held(candidate, target_key, target, hold_reason):
    """現在値を変えずに保留にする提案を作る。id と created_at は保存するときに付ける。"""
    question = _text(candidate, "confirmation_question")
    if question is None and target is not None:
        # 格下げした replace には質問が無いことがある。推測を含まない、既存の値を確かめる質問にする
        question = f"以前の「{target['value']}」は今も有効ですか？"
    return {
        "operation": OP_PENDING,
        "subject": candidate["subject"],
        "target_key": target_key,
        "target_value": target["value"] if target else None,
        "proposed_key": _text(candidate, "proposed_key"),
        "proposed_value": _text(candidate, "proposed_value"),
        "evidence": candidate["evidence"],
        "decision": candidate["decision"],
        "hold_reason": hold_reason,
        "reason": _text(candidate, "reason"),
        "confirmation_question": question,
    }


def apply_candidates(items, candidates, user_text, timestamp):
    """AIの判断を検証して現在値へ反映し、(新しい現在値, 変更のリスト, 警告メッセージ) を返す。

    元の items は書き換えない。AIの判断（decision）ごとに、ここで許された状態遷移だけを実行する。
    - replace: replace_hold_reason の条件をすべて満たすときだけ置き換える。満たさなければ保留
    - coexist: Current に無い key への追加だけ。既存の key と衝突したら上書きせず、同じ値なら何もしない、違えば保留
    - pending: 現在値を変えずに保留
    - no_change: 何もしない
    変更は operation が add / replace のものが現在値の変更で、履歴に残すイベントと同じ形
    （operation / subject / key / value / evidence と、replace のときだけ old_value）。
    operation が pending のものは保留の提案で、現在値も履歴も変えない。timestamp は変更した項目の updated_at になる。
    """
    updated = list(items)
    changes = []
    warnings = []
    touched = set()

    def index_of(subject, key):
        return next((i for i, c in enumerate(updated) if _address(c) == (subject, key)), None)

    for candidate in candidates:
        reason = rejection_reason(candidate, user_text)
        if reason:
            warnings.append(f"長期状態の候補を保存しませんでした: {reason}")
            continue
        decision = candidate["decision"]
        if decision == DECISION_NO_CHANGE:
            continue
        subject = candidate["subject"]
        target_key = _text(candidate, "target_key")
        target_index = index_of(subject, target_key) if target_key else None
        target = updated[target_index] if target_index is not None else None

        if decision == DECISION_PENDING:
            changes.append(_held(candidate, target_key, target, None))
            continue

        key = target_key if decision == DECISION_REPLACE else _text(candidate, "proposed_key")
        if key and (subject, key) in touched:
            # 1つの発言の中で、同じ項目を自分自身で置き換えない
            warnings.append(
                f"長期状態の候補を保存しませんでした: 同じ発言に {subject}.{key} の候補が複数あります"
            )
            continue

        if decision == DECISION_REPLACE:
            proposed_value = _text(candidate, "proposed_value")
            if target is not None and proposed_value and _same_value(target["value"], proposed_value):
                # 現在値と同じ値への置き換えは、条件を確かめるまでもなく何もしない
                touched.add((subject, key))
                continue
            hold_reason = replace_hold_reason(candidate, target, user_text)
            if hold_reason:
                changes.append(_held(candidate, target_key, target, hold_reason))
                continue
            value = candidate["proposed_value"].strip()
            touched.add((subject, key))
            updated[target_index] = {
                "subject": subject,
                "key": key,
                "value": value,
                "evidence": candidate["evidence"],
                "updated_at": timestamp,
            }
            changes.append(
                {
                    "operation": OP_REPLACE,
                    "subject": subject,
                    "key": key,
                    "old_value": target["value"],
                    "value": value,
                    "evidence": candidate["evidence"],
                }
            )
            continue

        # coexist
        value = candidate["proposed_value"].strip()
        touched.add((subject, key))
        existing_index = index_of(subject, key)
        if existing_index is not None:
            existing = updated[existing_index]
            if not _same_value(existing["value"], value):
                # 既存の値を上書きしない。AIが別の属性と判断した値を、同じ key の値として扱わないため保留にする
                changes.append(
                    _held(
                        candidate,
                        key,
                        existing,
                        f"proposed_key の {subject}.{key} は既に現在の長期状態にあり、上書きしません",
                    )
                )
            continue
        updated.append(
            {"subject": subject, "key": key, "value": value, "evidence": candidate["evidence"], "updated_at": timestamp}
        )
        changes.append(
            {"operation": OP_ADD, "subject": subject, "key": key, "value": value, "evidence": candidate["evidence"]}
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


def load_memory_pending(path):
    """保存済みの保留を読み込む。ファイルが無い・空の場合は空のリストを返す。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        raise StructuredMemoryError(f"長期保留ファイルを読み込めませんでした: {path} ({e})") from e
    if not raw.strip():
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StructuredMemoryError(
            f"長期保留ファイルがJSONとして読めません: {path} ({e.lineno} 行目: {e.msg})"
        ) from e
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not all(
        isinstance(item, dict)
        and all(isinstance(item.get(k), str) for k in PENDING_REQUIRED_KEYS)
        and all(item.get(k) is None or isinstance(item.get(k), str) for k in PENDING_KEYS)
        for item in items
    ):
        raise StructuredMemoryError(
            f"長期保留ファイルの形式が不正です: {path} "
            '（{"items": [{"id": "...", "subject": "...", "evidence": "...", "created_at": "..."}]} が必要です）'
        )
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        raise StructuredMemoryError(f"長期保留ファイルに同じ id が複数あります: {path}")
    return [
        {**{k: item.get(k) for k in PENDING_KEYS}, "status": item.get("status") or PENDING_STATUS_UNRESOLVED}
        for item in items
    ]


def save_memory_pending(path, items):
    """保留の全項目をファイルへ書き出す。"""
    text = json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise StructuredMemoryError(f"長期保留を保存できませんでした: {path} ({e})") from e


def _pending_identity(item):
    """完全一致相当の重複を見分けるための値。AIの理由や質問の言い回しの違いは見ない。"""
    return tuple(
        normalize(" ".join((item.get(k) or "").split()))
        for k in ("subject", "target_key", "proposed_key", "proposed_value", "evidence")
    )


def _next_pending_number(items):
    numbers = [int(item["id"][1:]) for item in items if re.fullmatch(r"p\d+", item["id"])]
    return max(numbers, default=0) + 1


def add_pending(items, held, timestamp):
    """保留の提案に id と created_at を付けて追加し、(新しい保留の全項目, 追加した項目) を返す。

    元の items は書き換えない。既にある保留（または同じ提案の中）と完全一致相当のものは追加しない。
    """
    updated = list(items)
    added = []
    seen = {_pending_identity(item) for item in items}
    number = _next_pending_number(items)
    for proposal in held:
        identity = _pending_identity(proposal)
        if identity in seen:
            continue
        seen.add(identity)
        item = {
            "id": f"p{number:03d}",
            **{k: v for k, v in proposal.items() if k != "operation"},
            "status": PENDING_STATUS_UNRESOLVED,
            "created_at": timestamp,
        }
        number += 1
        updated.append(item)
        added.append(item)
    return updated, added
