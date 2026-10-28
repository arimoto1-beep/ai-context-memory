"""確定した記憶変更の取り消し（/revert）。

保留の確認（MVP10）で現在値を変えた確定1件を、利用者のコマンドと y/N の承認でだけ取り消す。Claude は呼ばない。
取り消しは、過去の記録を消したり書き換えたりせず、新しい記録として残す。
- 現在値: 置換は確認の前の項目（値と evidence）に戻し、updated_at は取り消した時刻にする。共存で追加した key は外す
- 変更履歴: operation が revert のイベントを追記する。元の確認は reverted_confirmation_id で指し、confirmation_id のキーは持たせない
  （confirmation_id のキーは「その確認で行った変更」を表し、確認の質問・回答が根拠として付くため）
- 保留: 確認で解決した保留を reverted にし、同じ発言の新しい保留を作る（確定の取り消しで、発言の撤回ではないため）
取り消しログ（long_term_memory_reverts.jsonl）に、表示した内容・承認・変更計画を、状態を変える前に書く。
復旧は確認（confirmation.py）と同じく現在値の書き込みを確定点とし、現在値の全体が計画の実行前・実行後のどちらと同じかで決める。
"""

import hashlib
import json
import re

from .confirmation import (
    CONFIRMATION_ID_PATTERN,
    EVENT_APPLIED,
    EVENT_APPROVAL,
    EVENT_CANCELLED,
    EVENT_COMMIT,
    EVENT_EXPIRED,
    EVENT_FAILED,
    EVENT_PREPARED,
    EVENT_PRESENTED,
    _matches,
    events_of,
    history_event,
    resolution_problem,
    snapshot,
)
from .structured_memory import (
    OP_ADD,
    OP_REPLACE,
    PENDING_KEYS,
    PENDING_STATUS_RESOLVED,
    PENDING_STATUS_REVERTED,
    PENDING_STATUS_UNRESOLVED,
    StructuredMemoryError,
    _next_pending_number,
    _same_value,
    append_memory_history,
    append_text,
    find_item,
    load_memory_history,
    same_attribute_items,
    save_memory_pending,
    save_memory_state,
)

# 変更履歴のイベントの operation。ユーザーが述べた変更（add / replace）とは別の種類にする
OP_REVERT = "revert"
# 取り消せる確認の変更。現在値を変えなかった確認（operation が None）は、現在値の書き込みを確定点にできないので扱わない
REVERTABLE_OPERATIONS = (OP_REPLACE, OP_ADD)

# 取り消しログのイベント。名前と意味は確認ログと同じ。同じ revert_id のイベントの並びで、1回の取り消しを表す
# prepared: 表示する変更内容を決めた（表示する前に書く）。presented: 変更内容の出力処理が正常に終わった（出力した後に書く）
# approval: y/N の入力。commit: これから実行する変更計画（状態を変える前に書く）。applied / failed: 実行の結果
# cancelled: 承認の前に取り消した。expired: 再起動を挟んだため続きを行わない
EVENTS = (
    EVENT_PREPARED,
    EVENT_PRESENTED,
    EVENT_APPROVAL,
    EVENT_COMMIT,
    EVENT_APPLIED,
    EVENT_FAILED,
    EVENT_CANCELLED,
    EVENT_EXPIRED,
)
TERMINAL_EVENTS = (EVENT_APPLIED, EVENT_FAILED, EVENT_CANCELLED, EVENT_EXPIRED)

REVERT_ID_PATTERN = re.compile(r"r\d+")


class RevertError(StructuredMemoryError):
    """取り消しの失敗、または取り消せない理由。メッセージはそのままユーザーに表示できる。"""


def load_reverts(path):
    """取り消しログを全イベント読み込む。ファイルが無ければ空のリストを返す。

    途中で途切れた行を含め、読めない行があれば失敗にする（どの取り消しがどこまで進んだか分からないまま状態を変えないため）。
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        raise RevertError(f"取り消しログを読み込めませんでした: {path} ({e})") from e

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
            raise RevertError(f"取り消しログがJSONとして読めません: {path} ({number} 行目: {detail})") from e
        if (
            not isinstance(event, dict)
            or event.get("event") not in EVENTS
            or not isinstance(event.get("revert_id"), str)
            or not REVERT_ID_PATTERN.fullmatch(event["revert_id"])
            or not isinstance(event.get("target_confirmation_id"), str)
            or not CONFIRMATION_ID_PATTERN.fullmatch(event["target_confirmation_id"])
            or not isinstance(event.get("timestamp"), str)
        ):
            raise RevertError(
                f"取り消しログの形式が不正です: {path} "
                f"({number} 行目: event / revert_id / target_confirmation_id / timestamp が必要です)"
            )
        events.append(event)
    return events


def append_revert(path, event):
    """取り消しログへイベントを1行追記する。途中で失敗したら追記する前の長さに戻す（戻せなければ PartialWriteError）。"""
    try:
        append_text(path, json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as e:
        raise RevertError(f"取り消しログを保存できませんでした: {path} ({e})") from e


def next_revert_id(events):
    numbers = [int(e["revert_id"][1:]) for e in events]
    return f"r{max(numbers, default=0) + 1:03d}"


def revert_ids(events):
    return list(dict.fromkeys(e["revert_id"] for e in events))


def revert_events_of(events, revert_id):
    return [e for e in events if e["revert_id"] == revert_id]


def revert_progress(revert_events):
    """1回の取り消しがどこまで進んだかを返す（confirmation.progress と同じ意味）。

    closed: 終わっている。committed: 変更計画を書いたが、実行の結果が書かれていない（復旧が必要）。
    open: 表示・承認の途中（再起動を挟んだら続きを行わない）。
    """
    names = [e["event"] for e in revert_events]
    if any(name in TERMINAL_EVENTS for name in names):
        return "closed"
    if EVENT_COMMIT in names:
        return "committed"
    approvals = [e for e in revert_events if e["event"] == EVENT_APPROVAL]
    if approvals and not approvals[-1].get("approved"):
        return "closed"
    return "open"


def state_digest(items):
    """現在値の全体を表す値。復旧するときに、対象の key だけでなく全体が計画どおりかを確かめるため。

    共存で追加した key を外す取り消しでは、実行後は「key が無い」状態になる。
    key が無いことだけでは、取り消しを書き込んだのか、ファイルが壊れた・外で編集されたのかを区別できないので、全体で比べる。
    """
    text = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _applied_plan(confirmation_id, confirmations):
    """確認が反映した変更計画と、その確認の全イベントを返す。取り消せる確認でなければ RevertError。"""
    events = events_of(confirmations, confirmation_id)
    if not events:
        raise RevertError(f"確認 {confirmation_id} はありません")
    names = [e["event"] for e in events]
    commits = [e for e in events if e["event"] == EVENT_COMMIT]
    if EVENT_APPLIED not in names or not commits:
        raise RevertError(f"確認 {confirmation_id} は記憶を変更していないため、取り消せません（確認の結果: {names[-1]}）")
    plan = commits[-1]["plan"]
    if plan.get("operation") is None:
        raise RevertError(
            f"確認 {confirmation_id} は、同じ値が既にあったため現在値を変更していません。"
            "現在値を変更していない確認の取り消しは、MVP11 では扱いません"
        )
    if plan["operation"] not in REVERTABLE_OPERATIONS:
        raise RevertError(f"確認 {confirmation_id} の変更（{plan['operation']}）は取り消せません")
    return plan, events


def _check_not_reverted(confirmation_id, plan, reverts, history, pending_items):
    """既に取り消した確認でないこと、取り消しの記録どうしが食い違っていないことを確かめる。"""
    for revert_id in revert_ids(reverts):
        events = revert_events_of(reverts, revert_id)
        if events[0]["target_confirmation_id"] != confirmation_id:
            continue
        names = [e["event"] for e in events]
        if EVENT_APPLIED in names:
            raise RevertError(f"確認 {confirmation_id} は既に {revert_id} で取り消しています")
        if revert_progress(events) == "committed":
            raise RevertError(f"確認 {confirmation_id} の取り消し {revert_id} が完了していません。再起動してください")
    # 取り消しログに完了の記録が無いのに、変更履歴か保留には取り消しが残っている
    if any(e.get("reverted_confirmation_id") == confirmation_id for e in history):
        raise RevertError(
            f"取り消しログと変更履歴が一致しません（確認 {confirmation_id} を取り消したイベントが変更履歴にあります）"
        )
    pending = next((p for p in pending_items if p["id"] == plan["pending_id"]), None)
    if pending is not None and pending["status"] == PENDING_STATUS_REVERTED:
        raise RevertError(f"取り消しログと保留が一致しません（保留 {pending['id']} は既に取り消されています）")


def _confirmed_event_index(confirmation_id, plan, history):
    """変更履歴の中で、確認が書いたイベントの位置を返す。確認ログと一致しなければ RevertError。"""
    indexes = [i for i, e in enumerate(history) if e.get("confirmation_id") == confirmation_id]
    if len(indexes) != 1:
        raise RevertError(
            f"確認ログと変更履歴が一致しません（変更履歴に確認 {confirmation_id} のイベントが {len(indexes)} 件あります）"
        )
    event = history[indexes[0]]
    expected = history_event(plan, confirmation_id)
    if any(event.get(k) != v for k, v in expected.items()):
        raise RevertError(f"確認ログと変更履歴が一致しません（確認 {confirmation_id} の変更内容が違います）")
    return indexes[0]


def _item_before(events, plan):
    """置換の前の項目（値と evidence）を確認ログから返す。復元できなければ RevertError。

    質問を準備したとき（旧形式では表示したとき）に記録した項目を使う。
    反映の直前に、その項目と現在値が同じことを確かめてから置き換えているので、計画の実行前（before）とも一致するはず。
    """
    current = next(
        (
            e["current"]
            for e in events
            if e["event"] in (EVENT_PREPARED, EVENT_PRESENTED) and isinstance(e.get("current"), dict)
        ),
        None,
    )
    if (
        current is None
        or (current.get("subject"), current.get("key")) != (plan["subject"], plan["key"])
        or not isinstance(current.get("value"), str)
        or not isinstance(current.get("evidence"), str)
        or snapshot(current) != plan["before"]
    ):
        raise RevertError("確認の前の項目（値と根拠の発言）を確認ログから復元できないため、取り消せません")
    return current


def _later_change_problem(confirmation_id, plan, current):
    """確認の後に対象の項目が変わっていれば、その理由を返す。"""
    subject, key = plan["subject"], plan["key"]
    if current is None:
        return f"確認 {confirmation_id} の後に {subject}.{key} が現在の長期状態から無くなっています"
    if current["value"] != plan["after"]["value"]:
        return f"確認 {confirmation_id} の後に {subject}.{key} が「{plan['after']['value']}」から「{current['value']}」に変わっています"
    # 値が同じでも、更新時刻が違えば、一度変わって戻った可能性がある
    return f"確認 {confirmation_id} の後に {subject}.{key} が更新されています（値は同じでも、一度変わって戻った可能性があります）"


def apply_to_state(plan, items):
    """取り消しの計画を現在値に当てはめた新しい現在値を返す。元の items は書き換えない。項目の並びは保つ。"""
    address = (plan["subject"], plan["key"])
    if plan["reverted_operation"] == OP_REPLACE:
        return [dict(plan["restored"]) if (i["subject"], i["key"]) == address else i for i in items]
    return [i for i in items if (i["subject"], i["key"]) != address]


def plan_revert(revert_id, confirmation_id, confirmations, reverts, items, history, pending_items, timestamp):
    """確認 confirmation_id の取り消しの変更計画を作る。実行はしない。取り消せなければ RevertError（理由つき）。

    次をすべて満たすときだけ計画を作る。
    R1 確認が commit と applied まで進み、現在値を変更している（add / replace）
    R2 まだ取り消していない（取り消しログ・変更履歴・保留のどれにも取り消しが無い）
    R3 現在値の対象項目が、確認の実行後（値と updated_at）のまま
    R4 変更履歴に、確認のイベントがちょうど1件あって確認ログと一致し、その後に同じ key のイベントが無い
    R5 確認が解決した保留が resolved で、その確認による解決になっている
    R6 置換なら、確認の前の項目を確認ログから復元でき、戻しても同じ属性に同じ値が重ならない
    （R7 ファイルとセッションの一致、R8 書き込み停止・ほかの操作の途中でないことは、呼び出し側で確かめる）
    """
    plan, events = _applied_plan(confirmation_id, confirmations)
    _check_not_reverted(confirmation_id, plan, reverts, history, pending_items)
    subject, key = plan["subject"], plan["key"]
    index = _confirmed_event_index(confirmation_id, plan, history)
    later = [e for e in history[index + 1:] if (e.get("subject"), e.get("key")) == (subject, key)]
    if later:
        raise RevertError(
            f"確認 {confirmation_id} の後に {subject}.{key} が変更されています"
            f"（{later[-1].get('timestamp')} の {later[-1].get('operation')}）。後の変更を失うため、取り消せません"
        )
    current = find_item(items, subject, key)
    if not _matches(current, plan["after"]):
        raise RevertError(_later_change_problem(confirmation_id, plan, current) + "。取り消せません")
    original = next((p for p in pending_items if p["id"] == plan["pending_id"]), None)
    if (
        original is None
        or original["status"] != PENDING_STATUS_RESOLVED
        or original.get("confirmation_id") != confirmation_id
    ):
        raise RevertError(f"確認 {confirmation_id} が解決した保留 {plan['pending_id']} の状態が、確認ログと一致しません")

    restored = None
    after = None
    if plan["operation"] == OP_REPLACE:
        before = _item_before(events, plan)
        duplicates = [
            i["key"]
            for i in same_attribute_items(items, subject, key)
            if i["key"] != key and _same_value(i["value"], before["value"])
        ]
        if duplicates:
            raise RevertError(
                f"同じ属性の {subject}.{duplicates[0]} に「{before['value']}」があるため、戻すと値が重複します。取り消せません"
            )
        restored = {
            "subject": subject,
            "key": key,
            "value": before["value"],
            "evidence": before["evidence"],
            # 元の更新時刻に戻すと、確定の前に作られた古い保留が、変わって戻った値を相手に確認できてしまう
            "updated_at": timestamp,
        }
        after = {"value": restored["value"], "updated_at": timestamp}

    revert = {
        "revert_id": revert_id,
        "target_confirmation_id": confirmation_id,
        "target_pending_id": original["id"],
        "subject": subject,
        "key": key,
        "reverted_operation": plan["operation"],
        "removed_value": plan["value"],
        "restored": restored,
        "before": snapshot(current),
        "after": after,
        "reverted_at": timestamp,
    }
    new_items = apply_to_state(revert, items)
    reopened = _reopened_pending(revert, original, restored, _next_pending_number(pending_items), timestamp)
    # 取り消すと確認できなくなる、未解決の保留（確定した値を相手にして作られたものなど）
    affected = [
        {"id": p["id"], "reason": resolution_problem(p, new_items)}
        for p in pending_items
        if p["status"] == PENDING_STATUS_UNRESOLVED
        and resolution_problem(p, items) is None
        and resolution_problem(p, new_items) is not None
    ]
    revert.update(
        state_before=state_digest(items),
        state_after=state_digest(new_items),
        reopened_pending=reopened,
        # 新しい保留が、取り消した後の状態ですぐ確認できるか（確認できなければ理由）
        reopened_problem=resolution_problem(reopened, new_items),
        affected_pending=affected,
    )
    return revert


def _reopened_pending(revert, original, restored, number, timestamp):
    """取り消した確認の保留と同じ発言を持つ、新しい未解決の保留を作る。

    比べる相手（target_*）は、置換の取り消しでは戻した項目（更新時刻は取り消した時刻）にする。
    共存の取り消しでは、確認の前から変えていない既存の項目なので、元の保留のものをそのまま使う。
    """
    fields = {k: original.get(k) for k in PENDING_KEYS}
    fields.update(
        id=f"p{number:03d}",
        hold_reason=(
            f"確認 {revert['target_confirmation_id']} による変更を取り消し（{revert['revert_id']}）、"
            "確認の前の状態に戻したため、もう一度保留にしました"
        ),
        status=PENDING_STATUS_UNRESOLVED,
        created_at=timestamp,
        resolution=None,
        resolved_key=None,
        resolved_at=None,
        confirmation_id=None,
        reverted_by=None,
        reverted_at=None,
        reopened_from=original["id"],
        reopened_by=revert["revert_id"],
    )
    if restored is not None:
        fields.update(target_value=restored["value"], target_updated_at=restored["updated_at"])
    return fields


def revert_prompt(plan):
    """取り消しの実行前に表示する、具体的な変更内容と y/N の確認。時刻は含めない（承認の前に作り直して比べるため）。"""
    subject, key = plan["subject"], plan["key"]
    head = f"確認 {plan['target_confirmation_id']}（{plan['target_pending_id']}）による変更を取り消します。"
    if plan["reverted_operation"] == OP_REPLACE:
        restored = plan["restored"]
        change = (
            f"{subject}.{key} を「{plan['removed_value']}」から、確認の前の値「{restored['value']}」"
            f"（根拠の発言: {restored['evidence']}）に戻します。"
        )
    else:
        change = f"確認で追加した {subject}.{key}「{plan['removed_value']}」を現在の値から外します。ほかの値は変わりません。"
    reopened = plan["reopened_pending"]
    keep = (
        f"「{reopened['proposed_value']}」についての発言（{reopened['evidence']}）は撤回されず、"
        f"新しい保留 {reopened['id']} として残ります。"
    )
    if plan["reopened_problem"]:
        keep += f"ただし {reopened['id']} は今の状態では確認できません（{plan['reopened_problem']}）。"
    else:
        keep += f"/confirm {reopened['id']} で確認し直せます。"
    record = "元の確認の記録・会話原文・変更履歴は削除せず、取り消したことを変更履歴に追加します。"
    warning = ""
    if plan["affected_pending"]:
        warning = "注意: 取り消すと、次の保留は確認できなくなります。" + " ".join(
            f"{p['id']}（{p['reason']}）" for p in plan["affected_pending"]
        )
    return head + change + keep + record + warning + "実行しますか？（y/N）"


def revert_history_event(plan):
    """取り消しの変更履歴のイベント。value は取り消した後の値（共存の取り消しでは無い）、old_value は外した値。"""
    event = {"operation": OP_REVERT, "subject": plan["subject"], "key": plan["key"], "old_value": plan["removed_value"]}
    if plan["restored"] is not None:
        event["value"] = plan["restored"]["value"]
    event.update(
        revert_id=plan["revert_id"],
        reverted_confirmation_id=plan["target_confirmation_id"],
        reverted_pending_id=plan["target_pending_id"],
        reverted_operation=plan["reverted_operation"],
        reopened_pending_id=plan["reopened_pending"]["id"],
    )
    return event


def _reverted_pending(plan, pending_items):
    """保留に取り消しを反映した (新しい保留, 変更したか) を返す。既に反映済みの部分は変えない。食い違えば RevertError。"""
    revert_id = plan["revert_id"]
    original = next((p for p in pending_items if p["id"] == plan["target_pending_id"]), None)
    if original is None:
        raise RevertError(f"{revert_id}: 保留 {plan['target_pending_id']} が見つかりません")
    updated = list(pending_items)
    changed = False
    if original["status"] == PENDING_STATUS_RESOLVED and original.get("confirmation_id") == plan["target_confirmation_id"]:
        reverted = {**original, "status": PENDING_STATUS_REVERTED, "reverted_by": revert_id, "reverted_at": plan["reverted_at"]}
        updated = [reverted if p is original else p for p in updated]
        changed = True
    elif not (original["status"] == PENDING_STATUS_REVERTED and original.get("reverted_by") == revert_id):
        raise RevertError(f"{revert_id}: 保留 {original['id']} が、取り消しの計画と違う状態です（{original['status']}）")
    reopened = plan["reopened_pending"]
    existing = next((p for p in updated if p["id"] == reopened["id"]), None)
    if existing is None:
        updated.append(dict(reopened))
        changed = True
    elif existing.get("reopened_by") != revert_id:
        raise RevertError(f"{revert_id}: 新しい保留の ID {reopened['id']} が、別の保留に使われています")
    return updated, changed


def complete_revert(plan, items, pending_items, state_path, history_path, pending_path):
    """取り消しの計画を、まだ終わっていない手順から実行し、(新しい現在値, 新しい保留) を返す。

    何度呼んでも同じ結果になる（既に終わった手順は飛ばす）。順序は 現在値 → 変更履歴 → 保留。
    現在値の全体が計画の実行前とも実行後とも違う、保留が計画と食い違う、などで続けられなければ RevertError。
    """
    revert_id = plan["revert_id"]
    digest = state_digest(items)
    if digest not in (plan["state_before"], plan["state_after"]):
        raise RevertError(f"{revert_id}: 長期状態が取り消しの実行前とも実行後とも違うため、自動では続けられません")
    # 保留が計画と食い違っていれば、何も書く前に止める（現在値・変更履歴だけが取り消された状態を作らないため）
    updated_pending, changed = _reverted_pending(plan, pending_items)
    if digest == plan["state_before"]:
        updated = apply_to_state(plan, items)
        if state_digest(updated) != plan["state_after"]:
            raise RevertError(f"{revert_id}: 取り消した後の長期状態が計画と一致しません")
        save_memory_state(state_path, updated)
        items = updated
    history = load_memory_history(history_path)
    if not any(e.get("revert_id") == revert_id for e in history):
        append_memory_history(history_path, [revert_history_event(plan)], plan["reverted_at"])
    if changed:
        save_memory_pending(pending_path, updated_pending)
    return items, updated_pending


def reconcile_revert(plan, items, pending_items, state_path, history_path, pending_path):
    """変更計画を書いたあと結果が書かれていない取り消しを片付け、(結果のイベント名, 現在値, 保留) を返す。

    現在値の全体が計画の実行後と同じなら、残りの手順（変更履歴・保留）を終えて applied にする。
    実行前と同じで、変更履歴にも保留にも取り消しが無ければ、まだ何も変えていないので failed にする。
    どちらでもなければ RevertError（自動では続けない）。
    """
    revert_id = plan["revert_id"]
    digest = state_digest(items)
    if digest == plan["state_after"]:
        items, pending_items = complete_revert(plan, items, pending_items, state_path, history_path, pending_path)
        return EVENT_APPLIED, items, pending_items
    if digest == plan["state_before"]:
        history = load_memory_history(history_path)
        touched = any(e.get("revert_id") == revert_id for e in history) or any(
            p.get("reverted_by") == revert_id or p.get("reopened_by") == revert_id for p in pending_items
        )
        if touched:
            raise RevertError(
                f"{revert_id}: 長期状態は取り消し前のままなのに、変更履歴か保留には取り消しが反映されています。自動では続けられません"
            )
        return EVENT_FAILED, items, pending_items
    raise RevertError(
        f"{revert_id}: 長期状態が取り消しの実行前とも実行後とも違うため、自動では復旧できません。ファイルを確認してください"
    )


def revert_candidates(confirmations, reverts, items, history, pending_items):
    """現在値を変更した（または変更しようとして反映した）確認ごとに、(確認 ID, 確認の変更計画, 取り消せない理由か None)。

    applied まで進んだ確認だけを挙げる。理由は plan_revert と同じ検証で決める。
    """
    candidates = []
    for confirmation_id in dict.fromkeys(e["confirmation_id"] for e in confirmations):
        events = events_of(confirmations, confirmation_id)
        if not any(e["event"] == EVENT_APPLIED for e in events):
            continue
        commits = [e for e in events if e["event"] == EVENT_COMMIT]
        plan = commits[-1]["plan"] if commits else None
        try:
            plan_revert("r000", confirmation_id, confirmations, reverts, items, history, pending_items, "")
        except RevertError as e:
            candidates.append((confirmation_id, plan, str(e)))
        else:
            candidates.append((confirmation_id, plan, None))
    return candidates
