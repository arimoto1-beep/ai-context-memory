"""長期状態の保留（Pending）の確認。

利用者が /confirm で確認を始めると、保存済みの確認質問を表示し、直後の入力を回答として扱う。
回答が質問にどう答えているか（coexist / replace / unresolved / not_an_answer）はAIが判断する。
ここではAIの判断を検証し、許された状態遷移だけを、変更計画を確認ログへ先に書いてから実行する。
確認ログ（long_term_memory_confirmations.jsonl）が、確認の根拠の正本である。
変更履歴には confirmation_id だけを持たせ、質問と回答の原文は確認ログから引く（原文を二重に持たないため）。
"""

import json
import re
from datetime import datetime

from .memory import strip_code_fence
from .search import normalize
from .structured_memory import (
    KEY_PATTERN,
    MAX_KEY_LENGTH,
    OP_ADD,
    OP_REPLACE,
    PENDING_STATUS_RESOLVED,
    PENDING_STATUS_REVERTED,
    PENDING_STATUS_UNRESOLVED,
    SUBJECT_USER,
    StructuredMemoryError,
    _recover_json,
    _same_value,
    append_memory_history,
    append_text,
    find_item,
    load_memory_history,
    neutral_question,
    next_numbered_key,
    same_attribute_items,
    save_memory_pending,
    save_memory_state,
)

# 確認ログのイベント。同じ confirmation_id のイベントの並びで、1回の確認を表す
EVENT_PREPARED = "prepared"  # 表示する質問を決めた（表示する前に書く）。表示したことは意味しない
# 質問の出力処理が正常に終わった（出力した後に書く）。ユーザーが画面を見た・読んだ・理解したことは意味しない。
# prepared が無い確認の presented は、表示する前に書いていた旧形式で、表示の成功を必ずしも示さない
EVENT_PRESENTED = "presented"
EVENT_JUDGED = "judged"  # 回答を受け取り、AIが判断し、Pythonが検証した
EVENT_APPROVAL = "approval"  # replace の実行前の y/N
EVENT_COMMIT = "commit"  # これから実行する変更計画。状態を変える前に書く
EVENT_APPLIED = "applied"  # 変更計画をすべて実行した
EVENT_FAILED = "failed"  # 変更計画を実行しなかった（Current を変える前に止まった）
EVENT_CANCELLED = "cancelled"  # 回答の前、または承認の前に取り消した
EVENT_EXPIRED = "expired"  # 再起動を挟んだため、続きを行わない
EVENTS = (
    EVENT_PREPARED,
    EVENT_PRESENTED,
    EVENT_JUDGED,
    EVENT_APPROVAL,
    EVENT_COMMIT,
    EVENT_APPLIED,
    EVENT_FAILED,
    EVENT_CANCELLED,
    EVENT_EXPIRED,
)
TERMINAL_EVENTS = (EVENT_APPLIED, EVENT_FAILED, EVENT_CANCELLED, EVENT_EXPIRED)

# AIが判断する、回答と保留の関係
OUTCOME_COEXIST = "coexist"
OUTCOME_REPLACE = "replace"
OUTCOME_UNRESOLVED = "unresolved"
OUTCOME_NOT_AN_ANSWER = "not_an_answer"
OUTCOMES = (OUTCOME_COEXIST, OUTCOME_REPLACE, OUTCOME_UNRESOLVED, OUTCOME_NOT_AN_ANSWER)
OLD_VALUE_STATUSES = ("continues", "ended", "wrong", "unknown")
NEW_STATEMENT_STATUSES = ("stands", "withdrawn", "unclear")
NEW_STATEMENT_WITHDRAWN = "withdrawn"

# 判断結果の組み合わせ。AIの自己申告どうしの食い違いを見つけるためだけのもので、判断の正しさは確かめない
CONSISTENT_STATUSES = {
    OUTCOME_COEXIST: (("continues",), ("stands",)),
    OUTCOME_REPLACE: (("ended", "wrong"), ("stands",)),
}

# 新しい発言の撤回・訂正を述べるときに現れる表現。意味の判定には使わない。
# 回答に新しい値とこの表現が両方あれば、共存・置換を実行せずに保留のままにする（撤回は MVP10 では扱わない）
CORRECTION_EXPRESSIONS = ("間違", "誤り", "訂正")

QUESTION_SOURCE_PENDING = "pending"  # 保留を作ったときに関係判断AIが生成した質問
QUESTION_SOURCE_TEMPLATE = "template"  # Pythonの汎用の質問

# replace の実行前の確認で、実行してよいとみなす入力。これ以外はすべて「実行しない」
APPROVAL_INPUTS = ("y", "Y")

CONFIRMATION_ID_PATTERN = re.compile(r"c\d+")
PENDING_ID_PATTERN = re.compile(r"p\d+")


class ConfirmationError(StructuredMemoryError):
    """保留の確認の失敗。メッセージはそのままユーザーに表示できる。"""


def load_confirmations(path):
    """確認ログを全イベント読み込む。ファイルが無ければ空のリストを返す。

    途中で途切れた行（追記の途中で失敗した可能性がある）を含め、読めない行があれば失敗にする。
    どの確認がどこまで進んだかが分からないまま、状態を変えないため。
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        raise ConfirmationError(f"確認ログを読み込めませんでした: {path} ({e})") from e

    lines = raw.split("\n")
    events = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            truncated = number == len(lines) and not raw.endswith("\n")
            detail = "最後の行が途中で途切れています。追記の途中で失敗した可能性があります" if truncated else e.msg
            raise ConfirmationError(f"確認ログがJSONとして読めません: {path} ({number} 行目: {detail})") from e
        if (
            not isinstance(event, dict)
            or event.get("event") not in EVENTS
            or not isinstance(event.get("confirmation_id"), str)
            or not CONFIRMATION_ID_PATTERN.fullmatch(event["confirmation_id"])
            or not isinstance(event.get("pending_id"), str)
            or not isinstance(event.get("timestamp"), str)
        ):
            raise ConfirmationError(
                f"確認ログの形式が不正です: {path} ({number} 行目: event / confirmation_id / pending_id / timestamp が必要です)"
            )
        events.append(event)
    return events


def append_confirmation(path, event):
    """確認ログへイベントを1行追記する。途中で失敗したら追記する前の長さに戻す（戻せなければ PartialWriteError）。"""
    try:
        append_text(path, json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as e:
        raise ConfirmationError(f"確認ログを保存できませんでした: {path} ({e})") from e


def next_confirmation_id(events):
    numbers = [int(e["confirmation_id"][1:]) for e in events]
    return f"c{max(numbers, default=0) + 1:03d}"


def events_of(events, confirmation_id):
    return [e for e in events if e["confirmation_id"] == confirmation_id]


def confirmation_ids(events):
    return list(dict.fromkeys(e["confirmation_id"] for e in events))


def presentation(confirmation_events):
    """1回の確認で、質問の出力処理が正常に終わったことが記録から分かるかを返す。

    presented: prepared の後に presented がある（出力処理が正常に終わった）。
    not_presented: prepared だけで presented が無い（表示していない、または表示できなかった）。
    legacy_presented: prepared の無い旧形式の presented の後に、回答・取り消しがある。
    旧形式でも、回答・取り消しは表示の処理が戻った後にしか書かれないので、出力処理は終わっている。
    legacy_unverified: 旧形式の presented だけで、表示の成功が記録からは分からない。
    """
    names = [e["event"] for e in confirmation_events]
    if EVENT_PREPARED in names:
        return "presented" if EVENT_PRESENTED in names else "not_presented"
    if EVENT_JUDGED in names or EVENT_CANCELLED in names:
        return "legacy_presented"
    return "legacy_unverified"


def progress(confirmation_events):
    """1回の確認がどこまで進んだかを返す。

    closed: 終わっている。committed: 変更計画を書いたが、実行の結果が書かれていない（復旧が必要）。
    open: 回答・承認・計画の途中（再起動を挟んだら続きを行わない）。
    """
    names = [e["event"] for e in confirmation_events]
    if any(name in TERMINAL_EVENTS for name in names):
        return "closed"
    if EVENT_COMMIT in names:
        return "committed"
    approvals = [e for e in confirmation_events if e["event"] == EVENT_APPROVAL]
    if approvals and not approvals[-1].get("approved"):
        return "closed"
    judged = [e for e in confirmation_events if e["event"] == EVENT_JUDGED]
    if judged and not (judged[-1].get("accepted") and judged[-1].get("outcome") in CONSISTENT_STATUSES):
        # 状態を変えない判断（unresolved / not_an_answer / 検証失敗 / 呼び出し失敗）は、それで終わり
        return "closed"
    return "open"


def _parse_time(text):
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def resolution_problem(pending, items):
    """保留を確認して解決できれば None、できなければその理由を返す。"""
    if pending.get("status") == PENDING_STATUS_REVERTED:
        return f"{pending['id']} は、解決した確認を取り消した保留です（同じ発言は、取り消しで作った新しい保留で確認できます）"
    if pending.get("status") != PENDING_STATUS_UNRESOLVED:
        return f"{pending['id']} は既に解決済みです"
    if pending.get("subject") != SUBJECT_USER:
        return f"subject が user ではない保留は確認できません: {pending.get('subject')}"
    target_key = pending.get("target_key")
    target_value = pending.get("target_value")
    proposed_value = pending.get("proposed_value")
    if not target_key or target_value is None:
        return "既存の項目（target_key）に関係しない保留は、MVP10 では確認できません"
    if not proposed_value:
        return "新しい値（proposed_value）が分からない保留は、MVP10 では確認できません"
    current = find_item(items, pending["subject"], target_key)
    if current is None:
        return f"{pending['subject']}.{target_key} が現在の長期状態にありません"
    if not _same_value(current["value"], target_value):
        return (
            f"保留した後に {pending['subject']}.{target_key} の値が"
            f"「{target_value}」から「{current['value']}」に変わっています"
        )
    if pending.get("target_updated_at"):
        if current.get("updated_at") != pending["target_updated_at"]:
            return f"保留した後に {pending['subject']}.{target_key} が更新されています"
    elif current.get("updated_at"):
        # MVP9 の保留には保留した時点の updated_at が無いので、保留した時刻と比べる
        updated, created = _parse_time(current["updated_at"]), _parse_time(pending.get("created_at"))
        if updated is None or created is None:
            return "保留した時刻と現在の値の更新時刻を比べられません"
        if updated > created:
            return f"保留した後に {pending['subject']}.{target_key} が更新されています"
    if normalize(proposed_value) not in normalize(pending["evidence"]):
        # 関係判断AIが作った値（MVP9 の R2b で止めたもの）で現在値を変えないため
        return f"新しい値「{proposed_value}」が保留の発言にありません"
    if _same_value(proposed_value, target_value):
        return "新しい値が既存の値と同じです"
    return None


def choose_question(pending):
    """表示する質問と、その出どころを返す。

    保存済みの質問は、既存の値がそのまま現れるときだけ使う（何について尋ねたかを原文で確かめられるようにするため）。
    """
    question = (pending.get("confirmation_question") or "").strip()
    if question and normalize(pending["target_value"]) in normalize(question):
        return question, QUESTION_SOURCE_PENDING
    return neutral_question(pending["target_value"]), QUESTION_SOURCE_TEMPLATE


def parse_judgment(raw):
    """確認の判断AIの出力を (判断, JSON以外の前置きを除いて取り出したか) として解析する。"""
    text = strip_code_fence(raw)
    recovered = False
    try:
        judgment = json.loads(text)
    except json.JSONDecodeError:
        try:
            judgment = _recover_json(text)
        except StructuredMemoryError as e:
            raise ConfirmationError(str(e).replace("抽出結果", "確認の判断結果")) from e
        recovered = True
    if not isinstance(judgment, dict):
        raise ConfirmationError("確認の判断結果がJSONオブジェクトではありません")
    return judgment, recovered


def judgment_problem(judgment, pending, answer):
    """AIの判断を検証してよければ None、反映できなければその理由を返す。回答の意味が正しいかは判断しない。"""
    if judgment.get("pending_id") != pending["id"]:
        return f"判断の pending_id が確認中の保留（{pending['id']}）と違います: {judgment.get('pending_id')}"
    outcome = judgment.get("outcome")
    if outcome not in OUTCOMES:
        return f"outcome が不正です: {outcome}"
    answers = judgment.get("answers_question")
    if not isinstance(answers, bool):
        return f"answers_question が true / false ではありません: {answers}"
    if outcome == OUTCOME_NOT_AN_ANSWER:
        return None if answers is False else "outcome が not_an_answer なのに answers_question が true です"
    if answers is not True:
        return f"answers_question が false なのに outcome が {outcome} です"
    old_status = judgment.get("old_value_status")
    if old_status not in OLD_VALUE_STATUSES:
        return f"old_value_status が不正です: {old_status}"
    new_status = judgment.get("new_statement_status")
    if new_status not in NEW_STATEMENT_STATUSES:
        return f"new_statement_status が不正です: {new_status}"
    evidence = judgment.get("answer_evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return "answer_evidence がありません"
    if evidence not in answer:
        return f"answer_evidence が回答に含まれていません: {evidence}"
    if outcome == OUTCOME_UNRESOLVED:
        return None
    old_allowed, new_allowed = CONSISTENT_STATUSES[outcome]
    if old_status not in old_allowed or new_status not in new_allowed:
        return (
            f"判断が食い違っています（outcome: {outcome}, old_value_status: {old_status}, "
            f"new_statement_status: {new_status}）"
        )
    if normalize(pending["proposed_value"]) in normalize(answer) and any(
        expression in answer for expression in CORRECTION_EXPRESSIONS
    ):
        return "回答が新しい発言の誤り・訂正を述べている可能性があります（撤回は MVP10 では扱いません）"
    return None


def snapshot(item):
    """変更計画で比べる、現在値の項目の値と更新時刻。項目が無ければ None。"""
    if item is None:
        return None
    return {"value": item["value"], "updated_at": item.get("updated_at")}


def plan_resolution(outcome, pending, items, timestamp, history=()):
    """共存・置換の変更計画を作る。実行はしない。計画を作れなければ ConfirmationError。

    計画には、実行前（before）と実行後（after）の対象項目を入れる。
    復旧するときに、現在値がどちらと同じかで、どこまで実行したかを決めるため。
    history は変更履歴のイベントで、共存の番号付きの key に、過去に使った番号を使わないために渡す。
    """
    subject, target_key = pending["subject"], pending["target_key"]
    value = pending["proposed_value"].strip()
    base = {
        "pending_id": pending["id"],
        "subject": subject,
        "resolution": outcome,
        "resolved_at": timestamp,
        "evidence": pending["evidence"],
    }
    if outcome == OUTCOME_REPLACE:
        current = find_item(items, subject, target_key)
        return {
            **base,
            "operation": OP_REPLACE,
            "key": target_key,
            "old_value": current["value"],
            "value": value,
            "resolved_key": target_key,
            "before": snapshot(current),
            "after": {"value": value, "updated_at": timestamp},
        }
    existing = next(
        (item for item in same_attribute_items(items, subject, target_key) if _same_value(item["value"], value)),
        None,
    )
    if existing is not None:
        # 同じ属性に同じ値が既にあれば、現在値は変えずに保留だけを解決する（重複を作らない）
        return {**base, "operation": None, "key": existing["key"], "value": value, "resolved_key": existing["key"]}
    key = next_numbered_key(items, subject, target_key, history)
    if len(key) > MAX_KEY_LENGTH or not KEY_PATTERN.fullmatch(key):
        raise ConfirmationError(f"共存させる値の key を作れません: {key}")
    return {
        **base,
        "operation": OP_ADD,
        "key": key,
        "value": value,
        "resolved_key": key,
        "before": None,
        "after": {"value": value, "updated_at": timestamp},
    }


def _matches(item, expected):
    if expected is None:
        return item is None
    return item is not None and item["value"] == expected["value"] and item.get("updated_at") == expected["updated_at"]


def approval_prompt(plan):
    """置換の実行前に表示する、具体的な変更内容と y/N の確認。属性の種類によらない文面にする。"""
    return (
        f"「{plan['old_value']}」を {plan['subject']}.{plan['key']} の現在の値から外し、"
        f"「{plan['value']}」を現在の値として登録します（「{plan['old_value']}」は変更履歴に残ります）。"
        "実行しますか？（y/N）"
    )


def history_event(plan, confirmation_id):
    """変更計画に対応する変更履歴のイベント。質問と回答の原文は持たせず、confirmation_id で確認ログを引く。"""
    event = {"operation": plan["operation"], "subject": plan["subject"], "key": plan["key"]}
    if plan["operation"] == OP_REPLACE:
        event["old_value"] = plan["old_value"]
    event.update(
        value=plan["value"],
        evidence=plan["evidence"],
        pending_id=plan["pending_id"],
        confirmation_id=confirmation_id,
    )
    return event


def complete_plan(confirmation_id, plan, state, pending_items, state_path, history_path, pending_path):
    """変更計画を、まだ終わっていない手順から実行し、(新しい現在値, 新しい保留) を返す。

    何度呼んでも同じ結果になる（既に終わった手順は飛ばす）。順序は 現在値 → 変更履歴 → 保留。
    現在値が計画の実行前とも実行後とも違う、保留が別の確認で解決されている、などで続けられなければ ConfirmationError。
    """
    history = load_memory_history(history_path) if history_path else []
    subject, key = plan["subject"], plan["key"]
    if plan["operation"] in (OP_ADD, OP_REPLACE):
        current = find_item(state, subject, key)
        if _matches(current, plan["before"]) and not _matches(current, plan["after"]):
            item = {
                "subject": subject,
                "key": key,
                "value": plan["value"],
                "evidence": plan["evidence"],
                "updated_at": plan["after"]["updated_at"],
            }
            if current is None:
                updated = [*state, item]
            else:
                updated = [item if c is current else c for c in state]
            save_memory_state(state_path, updated)
            state = updated
        elif not _matches(current, plan["after"]):
            raise ConfirmationError(
                f"{confirmation_id}: {subject}.{key} が変更計画の実行前とも実行後とも違うため、自動では続けられません"
            )
        if history_path and not any(e.get("confirmation_id") == confirmation_id for e in history):
            append_memory_history(history_path, [history_event(plan, confirmation_id)], plan["resolved_at"])

    pending = next((p for p in pending_items if p["id"] == plan["pending_id"]), None)
    if pending is None:
        raise ConfirmationError(f"{confirmation_id}: 保留 {plan['pending_id']} が見つかりません")
    if pending["status"] == PENDING_STATUS_RESOLVED:
        if pending.get("confirmation_id") != confirmation_id:
            raise ConfirmationError(
                f"{confirmation_id}: 保留 {pending['id']} は別の確認（{pending.get('confirmation_id')}）で解決されています"
            )
        return state, pending_items
    if pending["status"] != PENDING_STATUS_UNRESOLVED:
        raise ConfirmationError(f"{confirmation_id}: 保留 {pending['id']} は {pending['status']} のため解決できません")
    resolved = {
        **pending,
        "status": PENDING_STATUS_RESOLVED,
        "resolution": plan["resolution"],
        "resolved_key": plan["resolved_key"],
        "resolved_at": plan["resolved_at"],
        "confirmation_id": confirmation_id,
    }
    updated_pending = [resolved if p is pending else p for p in pending_items]
    save_memory_pending(pending_path, updated_pending)
    return state, updated_pending


def reconcile(confirmation_id, plan, state, pending_items, state_path, history_path, pending_path):
    """変更計画を書いたあと結果が書かれていない確認を片付け、(結果のイベント名, 現在値, 保留) を返す。

    現在値が計画の実行後と同じなら、残りの手順（変更履歴・保留）を終えて applied にする。
    実行前と同じなら、まだ何も変えていないので failed にする（計画は実行しない）。
    どちらでもなければ ConfirmationError（自動では続けない）。現在値を変えない計画は、保留の状態で決める。
    """
    pending = next((p for p in pending_items if p["id"] == plan["pending_id"]), None)
    resolved_here = (
        pending is not None
        and pending["status"] == PENDING_STATUS_RESOLVED
        and pending.get("confirmation_id") == confirmation_id
    )
    if plan["operation"] is None:
        return (EVENT_APPLIED if resolved_here else EVENT_FAILED), state, pending_items
    current = find_item(state, plan["subject"], plan["key"])
    if _matches(current, plan["after"]):
        state, pending_items = complete_plan(
            confirmation_id, plan, state, pending_items, state_path, history_path, pending_path
        )
        return EVENT_APPLIED, state, pending_items
    if _matches(current, plan["before"]):
        history = load_memory_history(history_path) if history_path else []
        if resolved_here or any(e.get("confirmation_id") == confirmation_id for e in history):
            raise ConfirmationError(
                f"{confirmation_id}: 現在値は変更前のままなのに、変更履歴か保留には反映されています。自動では続けられません"
            )
        return EVENT_FAILED, state, pending_items
    raise ConfirmationError(
        f"{confirmation_id}: {plan['subject']}.{plan['key']} が変更計画の実行前とも実行後とも違うため、"
        "自動では復旧できません。ファイルを確認してください"
    )
