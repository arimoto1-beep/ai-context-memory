"""CLIの対話ループ。"""

import os
import sys
from pathlib import Path

from .chat import (
    CONFIRMATION_APPLIED,
    CONFIRMATION_AWAITING_APPROVAL,
    CONFIRMATION_DECLINED,
    CONFIRMATION_KEPT,
    CONFIRMATION_NOT_AN_ANSWER,
    ChatSession,
)
from .confirmation import QUESTION_SOURCE_PENDING, ConfirmationError
from .history import HistoryError
from .llm import ClaudeCLI, LLMError
from .memory import MemoryStoreError
from .revert import RevertError
from .structured_memory import (
    COVERAGE_COMPLETE,
    DECISION_PENDING,
    OP_ADD,
    OP_PENDING,
    OP_REPLACE,
    PENDING_STATUS_REVERTED,
    VIEW_FULL,
    VIEW_KEYS,
    StructuredMemoryError,
)
from .working_memory import OP_ADD as WORKING_OP_ADD
from .working_memory import OP_REMOVE, WorkingMemoryError, count_items

EXIT_COMMANDS = {"exit", "quit"}

# 起動時のカレントディレクトリからの相対パス。data/ はGit管理対象外
DEFAULT_HISTORY_FILE = Path("data") / "conversation.jsonl"
DEFAULT_MEMORY_FILE = Path("data") / "memories.jsonl"
DEFAULT_WORKING_MEMORY_FILE = Path("data") / "working_memory.json"
DEFAULT_WORKING_MEMORY_HISTORY_FILE = Path("data") / "working_memory_history.jsonl"
DEFAULT_MEMORY_STATE_FILE = Path("data") / "long_term_memory_state.json"
DEFAULT_MEMORY_HISTORY_FILE = Path("data") / "long_term_memory_history.jsonl"
DEFAULT_MEMORY_PENDING_FILE = Path("data") / "long_term_memory_pending.json"
DEFAULT_MEMORY_CONFIRMATIONS_FILE = Path("data") / "long_term_memory_confirmations.jsonl"
DEFAULT_MEMORY_REVERTS_FILE = Path("data") / "long_term_memory_reverts.jsonl"
# AI呼び出しごとの記録（役割・入力文字数・usage・時間・成否）。プロンプトや応答の本文は含めない
DEFAULT_AI_CALL_LOG_FILE = Path("data") / "ai_calls.jsonl"

# 保留の確認と、確定の取り消しのコマンド。これ以外の「/」で始まる入力は、これまでどおり通常の発言として扱う
COMMAND_PENDING = "/pending"
COMMAND_CONFIRM = "/confirm"
COMMAND_CANCEL = "/cancel"
COMMAND_REVERT = "/revert"


def parse_command(user_text):
    """確認・取り消しのコマンドなら (コマンド, 引数か None) を、そうでなければ None を返す。"""
    parts = user_text.split()
    if parts[0] in (COMMAND_PENDING, COMMAND_CONFIRM, COMMAND_REVERT) and len(parts) <= 2:
        return parts[0], parts[1] if len(parts) == 2 else None
    if parts[0] == COMMAND_CANCEL and len(parts) == 1:
        return parts[0], None
    return None


def run(session, input_fn=input, output_fn=print):
    output_fn("チャットを開始します。終了するには exit または quit と入力してください。")
    while True:
        try:
            user_text = input_fn("\nあなた> ").strip()
        except (EOFError, KeyboardInterrupt):
            output_fn("")
            for warning in session.cancel_confirmation("入力が終了しました") + session.cancel_revert("入力が終了しました"):
                output_fn(f"[警告] {warning}")
            break

        if not user_text:
            continue
        if user_text.lower() in EXIT_COMMANDS:
            for warning in session.cancel_confirmation("終了しました") + session.cancel_revert("終了しました"):
                output_fn(f"[警告] {warning}")
            break

        session.begin_turn()
        if session.awaiting_revert:
            # 取り消しの実行前の確認。y 以外の入力（コマンドを含む）は、すべて「実行しない」
            report_revert(session.approve_revert(user_text), output_fn)
            continue

        if session.awaiting_approval:
            # 置換の実行前の確認。y 以外の入力（コマンドを含む）は、すべて「実行しない」
            report_confirmation(session.approve(user_text), output_fn)
            continue

        command = parse_command(user_text)
        if command:
            if session.active_confirmation:
                # コマンドは回答として扱わない
                for warning in session.cancel_confirmation(f"確認中に {command[0]} が入力されました"):
                    output_fn(f"[警告] {warning}")
                output_fn("[確認] 確認を取り消しました。保留はそのままです")
            run_command(session, command, output_fn)
            continue

        if session.active_confirmation:
            try:
                result = session.answer_confirmation(user_text)
            except KeyboardInterrupt:
                output_fn("\n[確認を中断しました。保留はそのままです]")
                continue
            report_confirmation(result, output_fn)
            if result.status != CONFIRMATION_NOT_AN_ANSWER:
                continue
            # 回答としては会話原文に保存済みなので、通常のターンではもう一度書かない
            normal_turn(session, user_text, output_fn, recorded=True)
            continue

        normal_turn(session, user_text, output_fn)

    output_fn("終了します。")


def run_command(session, command, output_fn):
    name, argument = command
    if name == COMMAND_CANCEL:
        output_fn("[確認] 確認中ではありません")
        return
    if name == COMMAND_PENDING:
        lines = format_pending_details(session, argument) if argument else format_pending_overview(session)
        for line in lines:
            output_fn(line)
        return
    if name == COMMAND_REVERT:
        run_revert(session, argument, output_fn)
        return
    if argument is None:
        output_fn("[確認] 確認する保留の ID を指定してください（例: /confirm p001）。一覧は /pending")
        return
    try:
        started = session.start_confirmation(argument)
    except ConfirmationError as e:
        output_fn(f"[確認] {e}")
        return
    pending, current = started["pending"], started["current"]
    try:
        output_fn(f"[確認] {pending['id']} {pending['subject']}.{pending['target_key']}（確認 {started['confirmation_id']}、{started['attempt']}回目）")
        output_fn(f"[確認] 記録されている値: {current['value']}")
        output_fn(f"[確認] 新しい発言: {pending['evidence']}（値: {pending['proposed_value']}）")
        # 保留を作ったときにAIが生成した質問は AI>、Pythonの汎用の質問は 確認> として表示する
        speaker = "AI" if started["question_source"] == QUESTION_SOURCE_PENDING else "確認"
        output_fn(f"\n{speaker}> {started['question']}")
        output_fn("[確認] この質問への回答を入力してください（やめるには /cancel）")
    except BaseException:
        # 出力できなかった質問は表示済みにせず、回答待ちにもしない
        session.abandon_prepared("質問を出力できませんでした")
        raise
    try:
        warnings = session.mark_presented(started["confirmation_id"])
    except ConfirmationError as e:
        output_fn(f"[確認] {e}。次の入力は回答として扱いません")
        return
    for warning in warnings:
        output_fn(f"[警告] {warning}")


def run_revert(session, argument, output_fn):
    """/revert（一覧）と /revert <確認 ID>（変更内容を表示して y/N を待つ）。"""
    if argument is None:
        for line in format_revert_overview(session):
            output_fn(line)
        return
    try:
        started = session.start_revert(argument)
    except RevertError as e:
        output_fn(f"[取り消し] {e}")
        return
    try:
        output_fn(f"[取り消し] {started['revert_id']}: 確認 {started['confirmation_id']} の取り消し")
        output_fn(f"[取り消し] {started['prompt']}")
    except BaseException:
        # 出力できなかった文面は表示済みにせず、y/N も待たない
        session.abandon_prepared_revert("変更内容を出力できませんでした")
        raise
    try:
        session.mark_revert_presented(started["revert_id"])
    except RevertError as e:
        output_fn(f"[取り消し] {e}。次の入力は承認として扱いません")


def report_revert(result, output_fn):
    """取り消しの y/N の結果を表示する。"""
    for warning in result.warnings:
        output_fn(f"[警告] {warning}")
    plan = result.plan
    if result.status == CONFIRMATION_APPLIED:
        reopened = plan["reopened_pending"]["id"]
        done = (
            f"{result.revert_id} で確認 {result.confirmation_id} を取り消し。"
            f"{plan['target_pending_id']} は reverted、発言は保留 {reopened} に戻しました"
        )
        if plan["restored"] is not None:
            output_fn(f"[長期状態] {plan['subject']}.{plan['key']}: {plan['removed_value']} → {plan['restored']['value']}（{done}）")
        else:
            output_fn(f"[長期状態] {plan['subject']}.{plan['key']}: {plan['removed_value']} → 現在の値から外しました（{done}）")
        output_fn(f"[取り消し] もう一度確認するには /confirm {reopened}")
    elif result.status == CONFIRMATION_DECLINED:
        output_fn(f"[取り消し] 取り消しませんでした。確認 {result.confirmation_id} の変更はそのままです")
    else:
        output_fn(f"[取り消し] 取り消しませんでした: {result.reason}")


def format_revert_overview(session):
    try:
        candidates = session.revert_overview()
    except StructuredMemoryError as e:
        return [f"[取り消し] {e}"]
    if not candidates:
        return ["[取り消し] 記憶を変更した確認はありません"]
    lines = []
    for confirmation_id, plan, problem in candidates:
        if plan and plan.get("operation") == OP_REPLACE:
            change = f"{plan['subject']}.{plan['key']}: {plan['old_value']} → {plan['value']}"
        elif plan and plan.get("operation") == OP_ADD:
            change = f"{plan['subject']}.{plan['key']}: {plan['value']} を追加"
        elif plan:
            change = f"{plan['subject']}.{plan['key']}: 現在値の変更なし"
        else:
            change = "変更内容不明"
        pending_id = plan["pending_id"] if plan else "?"
        lines.append(f"[取り消し] {confirmation_id}（{pending_id}）{change}")
        if problem:
            lines.append(f"[取り消し]   取り消せません: {problem}")
        else:
            lines.append(f"[取り消し]   /revert {confirmation_id} で取り消せます")
    return lines


def report_confirmation(result, output_fn):
    """確認の回答・承認の結果を表示する。"""
    for warning in result.warnings:
        output_fn(f"[警告] {warning}")
    plan = result.plan
    if result.status == CONFIRMATION_APPLIED:
        resolved = f"確認 {result.confirmation_id} で {result.pending_id} を解決"
        if plan["operation"] == OP_REPLACE:
            output_fn(f"[長期状態] {plan['subject']}.{plan['key']}: {plan['old_value']} → {plan['value']}（{resolved}）")
        elif plan["operation"] == OP_ADD:
            output_fn(f"[長期状態] {plan['subject']}.{plan['key']}: {plan['value']}（{resolved}。既存の値はそのままです）")
        else:
            output_fn(
                f"[長期状態] {plan['subject']}.{plan['key']} に「{plan['value']}」が既にあるため変更しません（{resolved}）"
            )
    elif result.status == CONFIRMATION_AWAITING_APPROVAL:
        output_fn(f"[確認] {result.approval_prompt}")
    elif result.status == CONFIRMATION_DECLINED:
        output_fn(f"[確認] 実行しませんでした。{result.pending_id} は保留のままです")
    elif result.status == CONFIRMATION_KEPT:
        output_fn(f"[確認] {result.pending_id} は保留のままにしました（判断: 不明）")
        if result.withdrawn:
            output_fn("[確認] 回答は新しい発言の取り消しと判断されました。取り消しは MVP10 では扱えないため、保留のままです")
    elif result.status == CONFIRMATION_NOT_AN_ANSWER:
        output_fn(f"[確認] 質問への回答ではないと判断したため、確認を終了しました（{result.pending_id} は保留のまま）。通常の会話として扱います")
    else:
        output_fn(f"[確認] 反映しませんでした: {result.reason}。{result.pending_id} は保留のままです")


def format_pending_overview(session):
    overview = session.pending_overview()
    if not overview:
        return ["[長期保留] 未解決の保留はありません"]
    lines = []
    for pending, problem, attempts, last in overview:
        key = pending["target_key"] or pending["proposed_key"]
        line = f"[長期保留] {pending['id']} {pending['subject']}.{key}: {pending['proposed_value'] or '値不明'}"
        if pending["target_value"] is not None:
            line += f"（現在の値「{pending['target_value']}」）"
        line += f" 発言: {pending['evidence']}"
        if pending.get("reopened_from"):
            line += f"（{pending['reopened_by']} で {pending['reopened_from']} の確定を取り消して保留に戻したもの）"
        lines.append(line)
        history = f"これまでの確認 {attempts}回" + (f"、前回の判断: {last}" if last else "")
        if problem:
            lines.append(f"[長期保留]   確認できません: {problem}（{history}）")
        else:
            lines.append(f"[長期保留]   /confirm {pending['id']} で確認できます（{history}）")
    return lines


def format_pending_details(session, pending_id):
    try:
        pending, events = session.pending_details(pending_id)
    except ConfirmationError as e:
        return [f"[長期保留] {e}"]
    lines = [f"[長期保留] {pending['id']} status: {pending['status']}"]
    lines += [
        f"[長期保留]   {key}: {pending[key]}"
        for key in (
            "subject",
            "target_key",
            "target_value",
            "proposed_key",
            "proposed_value",
            "evidence",
            "confirmation_question",
            "created_at",
            "resolution",
            "resolved_key",
            "resolved_at",
            "confirmation_id",
            "reverted_by",
            "reverted_at",
            "reopened_from",
            "reopened_by",
        )
        if pending.get(key)
    ]
    prepared = {e["confirmation_id"] for e in events if e["event"] == "prepared"}
    for event in events:
        legacy = event["confirmation_id"] not in prepared
        lines.append(
            f"[確認記録] {event['confirmation_id']} {event['event']} {event['timestamp']}{describe_event(event, legacy)}"
        )
    for event in session.pending_reverts(pending_id):
        lines.append(
            f"[取り消し記録] {event['revert_id']} {event['event']} {event['timestamp']}"
            f"（確認 {event['target_confirmation_id']} の取り消し）{describe_revert_event(event)}"
        )
    return lines


def describe_revert_event(event):
    name = event["event"]
    if name == "prepared":
        return " 変更内容を準備（表示したことは意味しない）"
    if name == "presented":
        return " 変更内容の出力処理が正常に終了"
    if name == "approval":
        return f" 入力「{event['input']}」 {'承認' if event['approved'] else '承認されず'}"
    if name == "commit":
        return f" 変更計画: {event['plan']['subject']}.{event['plan']['key']} の {event['plan']['reverted_operation']} を取り消し"
    if event.get("reason") or event.get("error"):
        return f" {event.get('reason') or event.get('error')}"
    return ""


def describe_event(event, legacy=False):
    name = event["event"]
    if name == "prepared":
        return f" 質問を準備「{event['question']}」（{event['question_source']}。表示したことは意味しない）"
    if name == "presented" and legacy:
        return f" 質問「{event['question']}」（{event['question_source']}。旧形式: 出力に成功したかは記録されていない）"
    if name == "presented":
        return f" 質問の出力処理が正常に終了「{event['question']}」（{event['question_source']}）"
    if name == "judged":
        judgment = event.get("judgment") or {}
        verdict = "検証を通過" if event.get("accepted") else f"反映しない: {event.get('rejection_reason')}"
        return f" 回答「{event['answer']}」 AIの判断: {judgment.get('outcome')}（{judgment.get('reason')}） {verdict}"
    if name == "approval":
        return f" 入力「{event['input']}」 {'承認' if event['approved'] else '承認されず'}"
    if name == "commit":
        plan = event["plan"]
        return f" 変更計画: {plan['operation'] or '変更なし'} {plan['subject']}.{plan['key']} = {plan['value']}"
    if event.get("reason") or event.get("error"):
        return f" {event.get('reason') or event.get('error')}"
    return ""


def format_count(count):
    """検索のヒット件数。変更履歴を読み込めなかったとき（None）は「0件」と表示しない。"""
    return "読み込めませんでした（0件ではありません）" if count is None else f"{count}件"


STATUS_LABELS = {
    "state": "現在値",
    "pending": "保留",
    "history": "長期履歴",
    "memories": "記憶",
    "working_memory_history": "作業履歴",
}


def format_status_summary(status):
    """回答AIへ渡す検索の状態を、CLI 用に1行にまとめる（回答AIへ渡すものと同じ状態名）。"""
    parts = [f"検索: {status['search']}"]
    for scope, scope_status in status["scopes"]:
        text = scope_status["result"]
        if "count" in scope_status:
            text += f" {scope_status['count']}"
        omitted = scope_status.get("omitted", 0) + scope_status.get("omitted_attributes", 0)
        if omitted:
            text += f"（省略 {omitted}）"
        parts.append(f"{STATUS_LABELS[scope]}: {text}")
    return " / ".join(parts)


def notable_status(status):
    """検索の状態に、検索の表示だけでは分からないこと（失敗・読み込めない範囲・省略・一覧を使わなかった）があるか。

    回答AIへは毎ターン渡すが、CLI では、これらがあるターンだけ1行で表示する。
    """
    if not status:
        return False
    if status["search"] in ("failed", "retry_failed"):
        return True
    if (status.get("attribute_list") or {}).get("result") == "not_provided":
        return True
    return any(
        s["result"] in ("unavailable", "not_included") or s.get("omitted") or s.get("omitted_attributes")
        for _, s in status["scopes"]
    )


def format_attribute_report(retrieval):
    """属性一覧・属性の選択・R4 についての CLI の表示行。"""
    lines = []
    attribute_list = retrieval.attribute_list
    if attribute_list and attribute_list["total"]:
        if attribute_list["provided"]:
            lines.append(f"[属性一覧] {attribute_list['total']}件を渡しました（{attribute_list['chars']}文字）")
        else:
            lines.append(
                f"[属性一覧] {attribute_list['total']}件が上限 {attribute_list['limit']} を超えるため渡していません"
                "（検索語だけで探しました）"
            )
    if retrieval.selected_attributes:
        names = ", ".join(f"{subject}.{key}" for subject, key in retrieval.selected_attributes)
        lines.append(f"[属性選択] {names}")
    reasons = {"not_in_list": "一覧にありません", "ambiguous": "subject が決まりません"}
    for identifier, reason in retrieval.rejected_attributes:
        lines.append(f"[属性選択] {identifier} は使いません（{reasons.get(reason, reason)}）")
    if retrieval.ignored_attributes:
        lines.append(f"[属性選択] ほかに {len(retrieval.ignored_attributes)} 件の選択を使いませんでした")
    if retrieval.auto_history_attributes:
        names = ", ".join(f"{subject}.{key}" for subject, key in retrieval.auto_history_attributes)
        lines.append(f"[長期履歴] 保留・変更履歴だけに一致したため、変更履歴も渡します: {names}")
    return lines


def normal_turn(session, user_text, output_fn, recorded=False):
    """確認と関係のない、通常の1ターン（検索・回答・各記憶の抽出）。recorded は ChatSession.send と同じ。"""
    try:
        # 検索は会話本体より優先度が低いので、失敗しても警告だけで記憶なしの回答へ進む
        retrieval = session.retrieve(user_text)
        for queries, count in retrieval.history_log:
            output_fn(f"[履歴検索] {' / '.join(queries)}")
            output_fn(f"[履歴検索結果] {format_count(count)}")
        for queries, count in retrieval.memory_history_log:
            output_fn(f"[長期履歴検索] {' / '.join(queries)}")
            output_fn(f"[長期履歴検索結果] {format_count(count)}")
        for number, (queries, count) in enumerate(retrieval.search_log):
            output_fn(f"[{'再検索' if number else '検索'}] {' / '.join(queries)}")
            output_fn(f"[検索結果] {count}件")
        for terms, count in retrieval.memory_pending_log:
            output_fn(f"[長期保留検索] {' / '.join(terms)}")
            output_fn(f"[長期保留検索結果] {count}件")
        scope = retrieval.memory_history_scope
        if scope:
            # 回答AIへ渡す変更履歴は、ヒットしたイベントではなく、検索で見つかった属性ごとのイベント（渡した件数/全件数）
            for attribute in scope["attributes"]:
                output_fn(
                    f"[長期履歴] {attribute['subject']}.{attribute['key']} "
                    f"{attribute['shown']}/{attribute['total']}件"
                )
            if scope["omitted_attributes"]:
                output_fn(f"[長期履歴] ほかに {scope['omitted_attributes']} 属性の変更履歴は渡していません")
        for line in format_attribute_report(retrieval):
            output_fn(line)
        if notable_status(retrieval.status):
            output_fn(f"[検索状態] {format_status_summary(retrieval.status)}")
        for warning in retrieval.warnings:
            output_fn(f"[警告] {warning}")
        reply = session.send(
            user_text,
            retrieval.memories,
            retrieval.history_events,
            retrieval.memory_state,
            retrieval.memory_history_events,
            retrieval.memory_pending,
            recorded=recorded,
            memory_history_scope=retrieval.memory_history_scope,
            retrieval_status=retrieval.status,
        )
    except LLMError as e:
        output_fn(f"[エラー] {e}")
        return
    except HistoryError as e:
        # 応答は得られているので表示し、保存できなかったことを伝える
        output_fn(f"\nAI> {e.reply}")
        output_fn(f"[エラー] {e}")
        return
    except KeyboardInterrupt:
        output_fn("\n[中断しました]")
        return
    output_fn(f"\nAI> {reply}")
    for pending in retrieval.memory_pending:
        # 確認を始めるのは利用者で、回答AIは質問しない。どのコマンドで確認できるかだけを案内する
        output_fn(f"[確認コマンド] {pending['id']} は /confirm {pending['id']} で確認できます")

    # 記憶抽出は会話本体より優先度が低いので、失敗しても警告だけで続行する
    try:
        saved, warnings = session.remember(user_text)
    except KeyboardInterrupt:
        output_fn("\n[記憶抽出を中断しました]")
        return
    for memory in saved:
        output_fn(f"[記憶] {memory['text']}")
    for warning in warnings:
        output_fn(f"[警告] {warning}")

    # 構造化した長期記憶の抽出も同様に、失敗しても警告だけで続行する
    try:
        changes, warnings = session.update_memory_state(user_text, retrieval)
    except KeyboardInterrupt:
        output_fn("\n[長期状態の抽出を中断しました]")
        return
    line = format_extraction_view(session.last_extraction_view)
    if line:
        output_fn(line)
    for line in format_memory_state_changes(changes):
        output_fn(f"[長期状態] {line}")
    for line in format_memory_pending_changes(changes):
        output_fn(line)
    for warning in warnings:
        output_fn(f"[警告] {warning}")

    # 作業記憶の抽出も同様に、失敗しても警告だけで続行する
    try:
        changes, warnings = session.update_working_memory(user_text)
    except KeyboardInterrupt:
        output_fn("\n[作業記憶の抽出を中断しました]")
        return
    for line in format_working_memory_changes(changes):
        output_fn(f"[作業記憶] {line}")
    for warning in warnings:
        output_fn(f"[警告] {warning}")
    report_call_log_error(session, output_fn)


def report_call_log_error(session, output_fn):
    """AI呼び出しの記録をファイルへ書けなかったことを、一度だけ表示する（会話は止めない）。"""
    error = getattr(session.llm, "call_log_error", None)
    if error and not getattr(session, "call_log_error_reported", False):
        session.call_log_error_reported = True
        output_fn(f"[警告] {error}")


def format_extraction_view(view):
    """抽出AIへ渡した現在値の範囲を、従来（全量・一覧は完全）と違うときだけ1行にする。"""
    if not view or (view["mode"] == VIEW_FULL and view["key_coverage"] == COVERAGE_COMPLETE):
        return None
    if view["mode"] == VIEW_FULL:
        scope = f"全{view['total_attributes']}属性の値"
    elif view["mode"] == VIEW_KEYS:
        scope = f"全{view['total_attributes']}属性の key の一覧と、{len(view['value_attributes'])}属性の値"
    else:
        scope = (
            f"{view['total_attributes']}属性のうち {len(view['value_attributes'])}属性の値"
            "（key の一覧は上限を超えるため渡していません）"
        )
    line = f"[長期状態の入力] 抽出に渡した範囲: {scope}"
    if view["key_coverage"] != COVERAGE_COMPLETE:
        line += f"。新しい属性は自動では追加しません（{view['coverage_reason']}）"
    return line


def format_working_memory_changes(changes):
    """作業記憶の変更を表示用の行にする。追加はフィールドごとに1行、置き換え・削除は1件ごとに1行。"""
    added = {}
    lines = []
    for change in changes:
        if change["operation"] == WORKING_OP_ADD:
            added.setdefault(change["field"], []).append(change["text"])
        elif change["operation"] == OP_REMOVE:
            lines.append(f"{change['field']}: {change['text']} → 削除")
        else:
            lines.append(f"{change['field']}: {change['target']} → {change['text']}")
    return [f"{field}: {' / '.join(texts)}" for field, texts in added.items()] + lines


def format_memory_state_changes(changes):
    """構造化した長期記憶の現在値の変更を表示用の行にする。追加は新しい値、置き換えは「古い値 → 新しい値」。"""
    lines = []
    for change in changes:
        if change["operation"] == OP_PENDING:
            continue
        value = change["value"]
        if change["operation"] == OP_REPLACE:
            value = f"{change['old_value']} → {value}"
        lines.append(f"{change['subject']}.{change['key']}: {value}")
    return lines


def format_memory_pending_changes(changes):
    """新しく保存した保留を、見出し付きの表示用の行にする。現在値を変えていないこと、確定は自動では行わないことを示す。"""
    lines = []
    for change in changes:
        if change["operation"] != OP_PENDING:
            continue
        key = change["target_key"] or change["proposed_key"]
        line = f"[長期保留] {change['subject']}.{key}: {change['proposed_value'] or '値不明'}"
        if change["target_value"] is not None:
            line += f"（現在の値「{change['target_value']}」は変更していません）"
        lines.append(line)
        if change["decision"] != DECISION_PENDING:
            lines.append(
                f"[長期保留の理由] AIの判断は {change['decision']} でしたが、"
                f"自動では反映しませんでした: {change['hold_reason']}"
            )
        confirmable = bool(change["target_key"]) and change["target_value"] is not None
        if change["confirmation_question"] and confirmable:
            lines.append(
                f"[長期保留の確認事項] {change['confirmation_question']}"
                f"（/confirm {change['id']} で確認できます）"
            )
        elif not confirmable:
            # MVP15: 既存の項目に関係しない保留（新しい属性の追加を止めたものなど）は、/confirm では解決できない
            if change["confirmation_question"]:
                lines.append(f"[長期保留の確認事項] {change['confirmation_question']}")
            lines.append(
                f"[長期保留] {change['id']} は既存の項目に関係しないため、/confirm では確定できません"
                "（現在値は変更していません。内容は /pending で確認できます）"
            )
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
    memory_pending_path = Path(
        os.environ.get("ACM_LONG_TERM_MEMORY_PENDING_FILE") or DEFAULT_MEMORY_PENDING_FILE
    )
    memory_confirmations_path = Path(
        os.environ.get("ACM_LONG_TERM_MEMORY_CONFIRMATIONS_FILE") or DEFAULT_MEMORY_CONFIRMATIONS_FILE
    )
    memory_reverts_path = Path(os.environ.get("ACM_LONG_TERM_MEMORY_REVERTS_FILE") or DEFAULT_MEMORY_REVERTS_FILE)
    llm = ClaudeCLI()
    # AI呼び出しごとの記録の保存先（プロンプトや応答の本文は記録しない）
    llm.call_log_path = Path(os.environ.get("ACM_AI_CALL_LOG_FILE") or DEFAULT_AI_CALL_LOG_FILE)
    try:
        session = ChatSession(
            llm,
            history_path,
            memory_path,
            working_memory_path,
            working_memory_history_path,
            memory_state_path,
            memory_history_path,
            memory_pending_path,
            memory_confirmations_path,
            memory_reverts_path,
        )
    except (MemoryStoreError, WorkingMemoryError, StructuredMemoryError) as e:
        print(f"[エラー] {e}")
        return 1
    if session.memories:
        print(f"長期記憶を読み込みました（{len(session.memories)} 件）。")
    if session.memory_state:
        print(f"長期状態を読み込みました（{len(session.memory_state)}項目）。")
    if session.memory_pending:
        unresolved = len(session.unresolved_pending())
        reverted = sum(1 for p in session.memory_pending if p["status"] == PENDING_STATUS_REVERTED)
        resolved = len(session.memory_pending) - unresolved - reverted
        if reverted:
            print(f"長期保留を読み込みました（未解決 {unresolved}件、解決済み {resolved}件、取り消し済み {reverted}件）。")
        elif resolved:
            print(f"長期保留を読み込みました（未解決 {unresolved}件、解決済み {resolved}件）。")
        else:
            print(f"長期保留を読み込みました（{unresolved}件）。")
    for message in session.startup_messages:
        print(f"[復旧] {message}")
    if count_items(session.working_memory):
        print(f"作業記憶を読み込みました（{count_items(session.working_memory)}項目）。")
    run(session)
    return 0
