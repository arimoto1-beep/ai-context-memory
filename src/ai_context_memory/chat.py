"""会話。現在のプロセス内の履歴はメモリ上に保持し、会話原文・長期記憶・作業記憶をファイルへ保存する。"""

from dataclasses import dataclass, field

from .confirmation import (
    APPROVAL_INPUTS,
    EVENT_APPLIED,
    EVENT_APPROVAL,
    EVENT_CANCELLED,
    EVENT_COMMIT,
    EVENT_EXPIRED,
    EVENT_FAILED,
    EVENT_JUDGED,
    EVENT_PREPARED,
    EVENT_PRESENTED,
    NEW_STATEMENT_WITHDRAWN,
    OUTCOME_COEXIST,
    OUTCOME_NOT_AN_ANSWER,
    OUTCOME_REPLACE,
    OUTCOME_UNRESOLVED,
    PENDING_ID_PATTERN,
    ConfirmationError,
    append_confirmation,
    approval_prompt,
    choose_question,
    complete_plan,
    confirmation_ids,
    events_of,
    judgment_problem,
    load_confirmations,
    next_confirmation_id,
    parse_judgment,
    plan_resolution,
    presentation,
    progress,
    reconcile,
    resolution_problem,
    snapshot,
)
from .history import HistoryError, append_history, load_history
from .llm import LLMError
from .memory import (
    ORIGIN_USER,
    MemoryStoreError,
    append_memories,
    load_memories,
    parse_candidates,
    rejection_reason,
)
from .search import (
    MAX_ROUNDS,
    SearchPlanError,
    normalize,
    parse_plan,
    related_memory_pending,
    search_memories,
    search_memory_history,
    search_memory_pending,
    search_memory_state,
    search_working_memory_history,
)
from .structured_memory import (
    OP_ADD,
    OP_PENDING,
    OP_REPLACE,
    PENDING_STATUS_UNRESOLVED,
    PartialWriteError,
    StructuredMemoryError,
    add_pending,
    append_memory_history,
    load_memory_history,
    load_memory_pending,
    load_memory_state,
    find_item,
    parse_extraction,
    related_memory_state,
    save_memory_pending,
    save_memory_state,
    timestamp_now,
)
from .structured_memory import apply_candidates as apply_structured_candidates
from .working_memory import (
    WorkingMemoryError,
    append_working_memory_history,
    apply_candidates,
    empty_working_memory,
    load_working_memory,
    load_working_memory_history,
    save_working_memory,
)
from .working_memory import parse_candidates as parse_working_memory_candidates


@dataclass
class Retrieval:
    """1ターン分の検索結果。検索ログは、検索を実行した回ごとの (検索語のリスト, ヒット件数)。"""

    memories: list = field(default_factory=list)  # ヒットした従来の長期記憶
    memory_state: list = field(default_factory=list)  # ヒットした構造化した長期記憶の現在値
    search_log: list = field(default_factory=list)  # 長期記憶の検索ログ。件数は上の2つの合計
    memory_history_events: list = field(default_factory=list)  # ヒットした長期記憶の変更履歴（古い順）
    memory_history_log: list = field(default_factory=list)  # 検索しなかったターンは空
    memory_pending: list = field(default_factory=list)  # 回答に渡す構造化した長期記憶の保留（保存順）
    memory_pending_log: list = field(default_factory=list)  # 保留を検索しなかったターンは空
    history_events: list = field(default_factory=list)  # ヒットした作業記憶の変更履歴（古い順）
    history_log: list = field(default_factory=list)  # 検索しなかったターンは空
    warnings: list = field(default_factory=list)


# 保留の確認の1ステップの結果（ConfirmationResult.status）
CONFIRMATION_APPLIED = "applied"  # 共存・置換を実行した
CONFIRMATION_AWAITING_APPROVAL = "awaiting_approval"  # 置換の実行前の y/N を待っている
CONFIRMATION_DECLINED = "declined"  # 置換を承認されなかった
CONFIRMATION_KEPT = "kept"  # 判断は unresolved。保留のまま
CONFIRMATION_NOT_AN_ANSWER = "not_an_answer"  # 確認を閉じ、入力を通常の会話として扱う
CONFIRMATION_REJECTED = "rejected"  # AIの判断・状態が検証を通らなかった。保留のまま
CONFIRMATION_FAILED = "failed"  # 呼び出し・保存の失敗。保留のまま（状態を変えていない）


@dataclass
class ConfirmationResult:
    """保留の確認で、回答または承認を1つ受け取った結果。"""

    status: str
    confirmation_id: str
    pending_id: str
    reason: str = None  # 保留のままにした理由
    plan: dict = None  # 実行した（または承認を待っている）変更計画
    approval_prompt: str = None  # status が awaiting_approval のときに表示する確認
    withdrawn: bool = False  # AIが、新しい発言を取り消す回答と判断した（撤回は MVP10 では扱わない）
    warnings: list = field(default_factory=list)


class ChatSession:
    def __init__(
        self,
        llm,
        history_path=None,
        memory_path=None,
        working_memory_path=None,
        working_memory_history_path=None,
        memory_state_path=None,
        memory_history_path=None,
        memory_pending_path=None,
        memory_confirmations_path=None,
    ):
        """history_path へは会話原文を追記するだけで、読み戻さない。

        memory_path を渡すと、保存済みの長期記憶を検索対象として読み込む。
        working_memory_path を渡すと、保存済みの作業記憶を読み込み、毎回LLMへ渡す。
        working_memory_history_path へは作業記憶の変更履歴を追記し、検索が必要なターンで読み込む。
        memory_state_path を渡すと、構造化した長期記憶の現在値を検索対象として読み込む。
        memory_history_path へはその変更履歴を追記し、検索が必要なターンで読み込む。
        memory_pending_path を渡すと、構造化した長期記憶の保留を読み込み、現在値と同じ検索で取得する。
        memory_confirmations_path を渡すと、保留の確認（/confirm）を使えるようにし、確認ログを読み込む。
        変更計画を書いたまま結果が書かれていない確認があれば、会話を始める前にここで片付ける
        （片付けられなければ ConfirmationError で起動を止める）。
        """
        self.llm = llm
        self.history_path = history_path
        self.memory_path = memory_path
        self.working_memory_path = working_memory_path
        self.working_memory_history_path = working_memory_history_path
        # 今回の起動からの会話だけ。再起動前の会話はここへ戻さない
        self.messages = []
        self.memories = load_memories(memory_path) if memory_path else []
        self.working_memory = (
            load_working_memory(working_memory_path)
            if working_memory_path
            else empty_working_memory()
        )
        self.memory_state_path = memory_state_path
        self.memory_history_path = memory_history_path
        self.memory_state = load_memory_state(memory_state_path) if memory_state_path else []
        self.memory_pending_path = memory_pending_path
        self.memory_pending = load_memory_pending(memory_pending_path) if memory_pending_path else []
        self.memory_confirmations_path = memory_confirmations_path
        self.confirmations = load_confirmations(memory_confirmations_path) if memory_confirmations_path else []
        # 確認の途中の状態はプロセス内だけに持ち、保存しない（再起動を挟んだ回答・承認を受け付けないため）
        self.prepared_confirmation = None  # 質問を決めて prepared を書き、出力の成功を待っている
        self.active_confirmation = None  # 質問の出力処理が正常に終わり、回答を待っている
        self.awaiting_approval = None  # 置換の実行前の y/N を待っている
        # 反映の途中で失敗して復旧できなかったときの理由。設定されている間は長期状態を書き換えない
        self.write_blocked = None
        self.startup_messages = []
        if memory_confirmations_path and memory_state_path and memory_pending_path:
            self._recover_confirmations()

    # --- 保留の確認 ---

    def unresolved_pending(self):
        return [p for p in self.memory_pending if p["status"] == PENDING_STATUS_UNRESOLVED]

    def _pending(self, pending_id):
        return next((p for p in self.memory_pending if p["id"] == pending_id), None)

    def _log(self, name, confirmation_id, pending_id, **fields):
        """確認ログへイベントを追記し、追記できたものだけをメモリ上にも持つ。"""
        event = {
            "event": name,
            "confirmation_id": confirmation_id,
            "pending_id": pending_id,
            "timestamp": timestamp_now(),
            **fields,
        }
        try:
            append_confirmation(self.memory_confirmations_path, event)
        except PartialWriteError as e:
            # 確認ログの末尾が壊れている可能性があるので、以後は長期状態を書き換えない
            self.write_blocked = str(e)
            raise ConfirmationError(str(e)) from e
        self.confirmations.append(event)
        return event

    def _save_conversation(self, role, content, confirmation_id, confirmation_event):
        """確認の質問・回答・承認を会話原文へ追記する。失敗したら HistoryError を、成功したら None を返す。

        呼び出し側は、保存できなかった発言を使って確認を進めない。
        途中まで書かれた行を取り除けなかったときは、以後は長期状態を書き換えない。
        """
        if not self.history_path:
            return None
        entry = {
            "role": role,
            "content": content,
            "confirmation_id": confirmation_id,
            "confirmation_event": confirmation_event,
        }
        try:
            append_history(self.history_path, [entry])
        except HistoryError as e:
            if e.partial:
                self.write_blocked = str(e)
            return e
        return None

    def _recover_confirmations(self):
        """前回の起動で終わらなかった確認を片付ける。Claude は呼ばない。

        変更計画を書いた確認は、現在値が計画の実行後なら残りを実行して applied、実行前なら failed にする。
        回答・承認の途中で終わった確認は、続きを行わないことを expired として記録する。
        """
        for confirmation_id in confirmation_ids(self.confirmations):
            events = events_of(self.confirmations, confirmation_id)
            stage = progress(events)
            pending_id = events[0]["pending_id"]
            if stage == "committed":
                plan = [e for e in events if e["event"] == EVENT_COMMIT][-1]["plan"]
                result, self.memory_state, self.memory_pending = reconcile(
                    confirmation_id,
                    plan,
                    self.memory_state,
                    self.memory_pending,
                    self.memory_state_path,
                    self.memory_history_path,
                    self.memory_pending_path,
                )
                self._log(result, confirmation_id, pending_id, recovered=True)
                done = "続きを実行して反映しました" if result == EVENT_APPLIED else "反映されていなかったため、実行しませんでした"
                self.startup_messages.append(f"{confirmation_id}（{pending_id}）: 前回途中で止まった変更を確認し、{done}")
            elif stage == "open" and presentation(events) == "not_presented":
                # 質問を決めただけで、出力処理が正常に終わった記録が無い
                self._log(EVENT_EXPIRED, confirmation_id, pending_id, reason="質問を表示した記録が無いまま終わったため、続きを行いません")
                self.startup_messages.append(
                    f"{confirmation_id}（{pending_id}）: 前回の確認は質問を表示した記録が無いまま終わったため、続きは行いません（保留のままです）"
                )
            elif stage == "open":
                self._log(EVENT_EXPIRED, confirmation_id, pending_id, reason="再起動を挟んだため、続きを行いません")
                self.startup_messages.append(
                    f"{confirmation_id}（{pending_id}）: 前回の確認は完了していないため、続きは行いません（保留のままです）"
                )

    def confirmation_attempts(self, pending_id):
        """その保留について、質問の出力処理が正常に終わった確認の回数と、最後に判断した結果（無ければ None）。

        prepared だけの確認（表示していない・表示できなかった）と、表示の成功が分からない旧形式の確認は数えない。
        """
        events = [e for e in self.confirmations if e["pending_id"] == pending_id]
        presented = [
            confirmation_id
            for confirmation_id in confirmation_ids(events)
            if presentation(events_of(events, confirmation_id)) in ("presented", "legacy_presented")
        ]
        judged = [e for e in events if e["event"] == EVENT_JUDGED]
        last = None
        if judged:
            last = judged[-1]["outcome"] if judged[-1].get("accepted") else "rejected"
        return len(presented), last

    def pending_overview(self):
        """未解決の保留ごとに、(保留, 確認できない理由か None, 確認した回数, 前回の判断) を返す。"""
        return [
            (p, resolution_problem(p, self.memory_state), *self.confirmation_attempts(p["id"]))
            for p in self.unresolved_pending()
        ]

    def pending_details(self, pending_id):
        """保留1つと、その確認ログのイベントを返す。保留が無ければ ConfirmationError。"""
        pending = self._pending(pending_id)
        if pending is None:
            raise ConfirmationError(f"保留 {pending_id} はありません")
        return pending, [e for e in self.confirmations if e["pending_id"] == pending_id]

    def start_confirmation(self, pending_id):
        """保留の確認を準備し、表示する内容を dict で返す。始められなければ ConfirmationError。

        表示する質問を決めて、確認ログへ prepared を書く（書けなければ表示しない）。ここではまだ回答を待たない。
        呼び出し側は質問を出力し、出力処理が正常に終わったときだけ mark_presented を、
        失敗したときは abandon_prepared を呼ぶ。
        """
        if self.prepared_confirmation:
            # 前の準備が出力されないまま残っていれば、表示しなかったものとして閉じる
            self.abandon_prepared("出力されないまま、次の確認が始められました")
        if not self.memory_confirmations_path or not self.memory_state_path or not self.memory_pending_path:
            raise ConfirmationError("確認ログ・長期状態・長期保留の保存先が設定されていないため、確認できません")
        if self.write_blocked:
            raise ConfirmationError(f"長期状態の書き込みを停止しているため、確認できません（{self.write_blocked}）。再起動してください")
        if not PENDING_ID_PATTERN.fullmatch(pending_id or ""):
            raise ConfirmationError(f"保留の ID の形式が不正です: {pending_id}（例: /confirm p001）")
        if self.history_path:
            try:
                # 会話原文に途中までの行などが残っていれば、その後ろに確認の記録を足さない
                load_history(self.history_path)
            except HistoryError as e:
                raise ConfirmationError(f"会話原文が読めないため、確認できません（{e}）") from e
        pending = self._pending(pending_id)
        if pending is None:
            raise ConfirmationError(f"保留 {pending_id} はありません")
        problem = resolution_problem(pending, self.memory_state)
        if problem:
            raise ConfirmationError(f"{pending_id} は確認できません: {problem}")
        question, source = choose_question(pending)
        current = find_item(self.memory_state, pending["subject"], pending["target_key"])
        confirmation_id = next_confirmation_id(self.confirmations)
        self._log(
            EVENT_PREPARED,
            confirmation_id,
            pending_id,
            subject=pending["subject"],
            target_key=pending["target_key"],
            current=dict(current),
            proposed_value=pending["proposed_value"],
            pending_evidence=pending["evidence"],
            # 生成された質問（保留を作ったときのAIの質問）と、実際に表示した質問を分けて残す
            generated_question=pending.get("confirmation_question"),
            generated_at=pending["created_at"],
            question=question,
            question_source=source,
        )
        self.prepared_confirmation = {
            "confirmation_id": confirmation_id,
            "pending_id": pending_id,
            "question": question,
            "question_source": source,
            "current": dict(current),
        }
        return {
            "confirmation_id": confirmation_id,
            "pending": pending,
            "current": current,
            "question": question,
            "question_source": source,
            # 今回の確認は、出力に成功すれば何回目になるか
            "attempt": self.confirmation_attempts(pending_id)[0] + 1,
        }

    def mark_presented(self, confirmation_id):
        """質問の出力処理が正常に終わったあとに呼ぶ。presented を書き、会話原文へ残し、回答待ちにする。

        警告メッセージを返す。presented を書けない、または会話原文に質問を保存できなければ
        ConfirmationError で、回答待ちにしない（記録の揃っていない質問に、次の入力を結び付けないため）。
        presented は出力処理が終わったことだけを表し、ユーザーが読んだことは表さない。
        """
        prepared = self.prepared_confirmation
        if prepared is None or prepared["confirmation_id"] != confirmation_id:
            raise ConfirmationError(f"表示を記録する確認 {confirmation_id} が準備されていません")
        self.prepared_confirmation = None
        pending_id = prepared["pending_id"]
        try:
            self._log(
                EVENT_PRESENTED,
                confirmation_id,
                pending_id,
                question=prepared["question"],
                question_source=prepared["question_source"],
            )
        except ConfirmationError as e:
            raise ConfirmationError(f"質問を表示したことを記録できなかったため、確認を中止しました（{e}）") from e
        error = self._save_conversation("assistant", prepared["question"], confirmation_id, EVENT_PRESENTED)
        if error:
            # presented は残る（出力処理は終わっている）。確認はここで閉じ、回答待ちにしない
            self.cancel_confirmation_quietly(
                {"confirmation_id": confirmation_id, "pending_id": pending_id},
                f"会話原文に質問を保存できませんでした: {error}",
            )
            raise ConfirmationError(f"会話原文に質問を保存できなかったため、確認を中止しました（{error}）") from error
        self.messages.append({"role": "assistant", "content": prepared["question"]})
        self.active_confirmation = {k: prepared[k] for k in ("confirmation_id", "pending_id", "question", "current")}
        return []

    def abandon_prepared(self, reason):
        """準備した質問を出力できなかったときに呼ぶ。回答待ちにせず、取り消しを記録し、警告メッセージを返す。"""
        prepared = self.prepared_confirmation
        self.prepared_confirmation = None
        if prepared is None:
            return []
        try:
            self._log(EVENT_CANCELLED, prepared["confirmation_id"], prepared["pending_id"], reason=reason)
        except ConfirmationError as e:
            # 書けなくても、prepared だけが残り、次の起動時に expired になる
            return [str(e)]
        return []

    def cancel_confirmation(self, reason):
        """回答・承認を待っている確認を取り消し、警告メッセージを返す。待っていなければ何もしない。"""
        warnings = self.abandon_prepared(reason)
        waiting = self.active_confirmation or self.awaiting_approval
        self.active_confirmation = None
        self.awaiting_approval = None
        if not waiting:
            return warnings
        try:
            self._log(EVENT_CANCELLED, waiting["confirmation_id"], waiting["pending_id"], reason=reason)
        except ConfirmationError as e:
            return [str(e)]
        return []

    def _fresh_problem(self, waiting):
        """反映する直前に、保留と現在値が確認を始めたときのままかを確かめる。問題が無ければ None。"""
        pending = self._pending(waiting["pending_id"])
        if pending is None:
            return f"保留 {waiting['pending_id']} がありません"
        problem = resolution_problem(pending, self.memory_state)
        if problem:
            return problem
        current = find_item(self.memory_state, pending["subject"], pending["target_key"])
        if snapshot(current) != snapshot(waiting["current"]):
            return f"確認を始めた後に {pending['subject']}.{pending['target_key']} が変わっています"
        try:
            # このセッションの外でファイルが書き換えられていたら、古い内容で上書きしない
            if load_memory_state(self.memory_state_path) != self.memory_state:
                return "長期状態ファイルが、確認を始めた後にこのセッションの外で変更されています"
            if load_memory_pending(self.memory_pending_path) != self.memory_pending:
                return "長期保留ファイルが、確認を始めた後にこのセッションの外で変更されています"
        except StructuredMemoryError as e:
            return f"ファイルを確かめられません: {e}"
        return None

    def answer_confirmation(self, user_text):
        """表示した質問への回答をLLMに判断させ、検証して、ConfirmationResult を返す。

        共存は検証を通れば実行する。置換は実行せず、y/N の承認を待つ（approve へ）。
        不明・検証失敗・呼び出し失敗では何も変えない。回答ではないと判断したら確認を閉じる
        （呼び出し側は、その入力を通常の会話として処理する）。
        この回答は通常の記憶抽出に渡さない（同じ発言で二重に更新しないため）。
        回答は判断の前に会話原文へ保存し、保存できなければ判断せずに確認を閉じる
        （not_an_answer で通常のターンへ回すときも、この保存を使い、二重に書かない）。
        """
        waiting = self.active_confirmation
        self.active_confirmation = None
        confirmation_id, pending_id, question = waiting["confirmation_id"], waiting["pending_id"], waiting["question"]
        error = self._save_conversation("user", user_text, confirmation_id, "answer")
        if error:
            self.cancel_confirmation_quietly(waiting, f"回答を会話原文に保存できませんでした: {error}")
            return ConfirmationResult(
                CONFIRMATION_FAILED,
                confirmation_id,
                pending_id,
                reason=f"回答を会話原文に保存できなかったため、確認を中止しました（{error}）",
            )
        self.messages.append({"role": "user", "content": user_text})
        pending = self._pending(pending_id)
        current = find_item(self.memory_state, pending["subject"], pending["target_key"])
        record = {"answer": user_text, "llm_model": getattr(self.llm, "model", None)}
        judgment = None
        problem = None
        try:
            raw = self.llm.judge_confirmation(pending, current, question, user_text)
        except LLMError as e:
            raw = None
            problem = f"確認の判断を得られませんでした: {e}"
        except KeyboardInterrupt:
            self.cancel_confirmation_quietly(waiting, "回答の判断を中断しました")
            raise
        record["llm_output"] = raw
        if raw is not None:
            try:
                judgment, record["recovered"] = parse_judgment(raw)
            except ConfirmationError as e:
                problem = str(e)
        record["judgment"] = judgment
        if judgment is not None and problem is None:
            problem = judgment_problem(judgment, pending, user_text)
        outcome = judgment["outcome"] if judgment is not None and problem is None else None
        plan = None
        if outcome in (OUTCOME_COEXIST, OUTCOME_REPLACE):
            problem = self._fresh_problem(waiting)
            if problem is None:
                try:
                    plan = plan_resolution(outcome, pending, self.memory_state, timestamp_now())
                except ConfirmationError as e:
                    problem = str(e)
            if problem:
                outcome = None
        result = ConfirmationResult(CONFIRMATION_REJECTED, confirmation_id, pending_id, reason=problem)
        try:
            self._log(
                EVENT_JUDGED,
                confirmation_id,
                pending_id,
                **record,
                accepted=problem is None,
                rejection_reason=problem,
                outcome=outcome,
                plan=plan,
            )
        except ConfirmationError as e:
            return ConfirmationResult(CONFIRMATION_FAILED, confirmation_id, pending_id, reason=str(e))
        if raw is None:
            result.status = CONFIRMATION_FAILED

        if outcome == OUTCOME_NOT_AN_ANSWER:
            # この入力は通常のターンとして処理する（会話原文には保存済み）
            return ConfirmationResult(CONFIRMATION_NOT_AN_ANSWER, confirmation_id, pending_id)
        if outcome == OUTCOME_UNRESOLVED:
            result.status = CONFIRMATION_KEPT
            result.withdrawn = judgment.get("new_statement_status") == NEW_STATEMENT_WITHDRAWN
        elif outcome == OUTCOME_COEXIST:
            return self._commit(confirmation_id, pending_id, plan, result.warnings)
        elif outcome == OUTCOME_REPLACE:
            prompt = approval_prompt(plan)
            error = self._save_conversation("assistant", prompt, confirmation_id, "approval_prompt")
            if error:
                self.cancel_confirmation_quietly(waiting, f"変更内容の確認を会話原文に保存できませんでした: {error}")
                result.status = CONFIRMATION_FAILED
                result.reason = f"変更内容の確認を会話原文に保存できなかったため、実行しませんでした（{error}）"
                return result
            self.awaiting_approval = {**waiting, "prompt": prompt}
            result.status = CONFIRMATION_AWAITING_APPROVAL
            result.plan = plan
            result.approval_prompt = prompt
        return result

    def cancel_confirmation_quietly(self, waiting, reason):
        try:
            self._log(EVENT_CANCELLED, waiting["confirmation_id"], waiting["pending_id"], reason=reason)
        except ConfirmationError:
            pass

    def approve(self, user_text):
        """置換の実行前の y/N への入力を受け取り、ConfirmationResult を返す。y（Y）のときだけ実行する。

        承認した事実（表示した内容と入力）を確認ログへ書いてから実行する。変更計画は承認した時点の状態で作り直す。
        """
        waiting = self.awaiting_approval
        self.awaiting_approval = None
        confirmation_id, pending_id = waiting["confirmation_id"], waiting["pending_id"]
        approved = user_text.strip() in APPROVAL_INPUTS
        error = self._save_conversation("user", user_text, confirmation_id, EVENT_APPROVAL)
        if error:
            # 承認の入力を残せなければ、approval を書かず、y でも実行しない（再起動後も承認として扱わない）
            self.cancel_confirmation_quietly(waiting, f"承認の入力を会話原文に保存できませんでした: {error}")
            return ConfirmationResult(
                CONFIRMATION_FAILED,
                confirmation_id,
                pending_id,
                reason=f"承認の入力を会話原文に保存できなかったため、実行しませんでした（{error}）",
            )
        warnings = []
        problem = self._fresh_problem(waiting) if approved else None
        try:
            self._log(
                EVENT_APPROVAL,
                confirmation_id,
                pending_id,
                prompt=waiting["prompt"],
                input=user_text,
                approved=approved,
            )
        except ConfirmationError as e:
            return ConfirmationResult(CONFIRMATION_FAILED, confirmation_id, pending_id, reason=str(e), warnings=warnings)
        if not approved:
            return ConfirmationResult(CONFIRMATION_DECLINED, confirmation_id, pending_id, warnings=warnings)
        if problem is None:
            pending = self._pending(pending_id)
            try:
                plan = plan_resolution(OUTCOME_REPLACE, pending, self.memory_state, timestamp_now())
            except ConfirmationError as e:
                problem = str(e)
        if problem:
            try:
                self._log(EVENT_FAILED, confirmation_id, pending_id, error=problem)
            except ConfirmationError as e:
                warnings.append(str(e))
            return ConfirmationResult(CONFIRMATION_REJECTED, confirmation_id, pending_id, reason=problem, warnings=warnings)
        return self._commit(confirmation_id, pending_id, plan, warnings)

    def _commit(self, confirmation_id, pending_id, plan, warnings):
        """変更計画を確認ログへ書いてから実行する。途中で失敗したら、その場で1回だけ復旧を試みる。"""
        if self.write_blocked:
            reason = f"長期状態の書き込みを停止しているため、実行しませんでした（{self.write_blocked}）"
            try:
                self._log(EVENT_FAILED, confirmation_id, pending_id, error=reason)
            except ConfirmationError as e:
                warnings.append(str(e))
            return ConfirmationResult(CONFIRMATION_FAILED, confirmation_id, pending_id, reason=reason, warnings=warnings)
        try:
            self._log(EVENT_COMMIT, confirmation_id, pending_id, plan=plan)
        except ConfirmationError as e:
            return ConfirmationResult(CONFIRMATION_FAILED, confirmation_id, pending_id, reason=str(e), warnings=warnings)
        try:
            state, pending = complete_plan(
                confirmation_id,
                plan,
                self.memory_state,
                self.memory_pending,
                self.memory_state_path,
                self.memory_history_path,
                self.memory_pending_path,
            )
        except StructuredMemoryError as e:
            return self._recover_after_failure(confirmation_id, pending_id, plan, e, warnings)
        self.memory_state, self.memory_pending = state, pending
        try:
            self._log(EVENT_APPLIED, confirmation_id, pending_id)
        except ConfirmationError as e:
            warnings.append(f"{e}（変更は反映済みです。次の起動時に確認ログへ記録を補います）")
        return ConfirmationResult(CONFIRMATION_APPLIED, confirmation_id, pending_id, plan=plan, warnings=warnings)

    def _recover_after_failure(self, confirmation_id, pending_id, plan, error, warnings):
        """反映の途中の失敗のあと、ファイルを読み直して片付ける。片付けられなければ長期状態の書き込みを止める。"""
        try:
            state = load_memory_state(self.memory_state_path)
            pending = load_memory_pending(self.memory_pending_path)
            result, state, pending = reconcile(
                confirmation_id,
                plan,
                state,
                pending,
                self.memory_state_path,
                self.memory_history_path,
                self.memory_pending_path,
            )
            self._log(result, confirmation_id, pending_id, recovered=True, error=str(error))
        except StructuredMemoryError as e:
            self.write_blocked = f"{confirmation_id} の反映が途中で止まりました: {e}"
            return ConfirmationResult(
                CONFIRMATION_FAILED,
                confirmation_id,
                pending_id,
                reason=f"{error}。続きも実行できませんでした（{e}）。長期状態の書き込みを停止しました。再起動してください",
                warnings=warnings,
            )
        self.memory_state, self.memory_pending = state, pending
        if result == EVENT_APPLIED:
            warnings.append(f"反映の途中で失敗しましたが、続きを実行して反映しました: {error}")
            return ConfirmationResult(CONFIRMATION_APPLIED, confirmation_id, pending_id, plan=plan, warnings=warnings)
        return ConfirmationResult(CONFIRMATION_FAILED, confirmation_id, pending_id, reason=str(error), warnings=warnings)

    def _with_confirmation_texts(self, events):
        """変更履歴のイベントに、確認ログから質問・回答・承認の原文を付けたコピーを返す（回答AIに渡すため）。"""
        texts = {}
        for event in self.confirmations:
            entry = texts.setdefault(event["confirmation_id"], {})
            if event["event"] == EVENT_PRESENTED:
                entry["confirmation_question"] = event.get("question")
            elif event["event"] == EVENT_JUDGED:
                entry["confirmation_answer"] = event.get("answer")
            elif event["event"] == EVENT_APPROVAL and event.get("approved"):
                entry["approval_input"] = event.get("input")
        return [{**event, **texts.get(event.get("confirmation_id"), {})} for event in events]

    def retrieve(self, user_text):
        """ユーザー発話に答えるために必要な長期記憶と2つの変更履歴を検索し、Retrieval を返す。

        どれを検索するかと検索語はLLMに1回の検索プランで考えさせ、検索と回数の制御はここで行う。
        発話の意味はここでは解釈しない。
        長期記憶は、従来の記憶と構造化した現在値・保留を同じ検索語で検索し、
        どれも0件なら1回だけ検索語を変えて再検索する。変更履歴の検索はそれぞれ1回だけ。
        保留は、検索語でヒットしたものに加えて、ヒットした現在値の key につながるものも取得する。
        失敗しても例外にはせず、警告として返す（会話本体を止めないため）。
        """
        result = Retrieval()
        events = []
        if self.working_memory_history_path:
            try:
                events = load_working_memory_history(self.working_memory_history_path)
            except WorkingMemoryError as e:
                result.warnings.append(str(e))
        memory_events = []
        if self.memory_history_path:
            try:
                memory_events = load_memory_history(self.memory_history_path)
            except StructuredMemoryError as e:
                result.warnings.append(str(e))
        # 解決済みの保留は回答AIへ渡さない（解決の結果は現在値と変更履歴にある）
        pending_items = self.unresolved_pending()
        has_long_term = bool(self.memories or self.memory_state or pending_items)
        if not has_long_term and not events and not memory_events:
            # 検索できるものが無い
            return result
        tried = []
        for round_number in range(MAX_ROUNDS):
            try:
                plan = parse_plan(self.llm.plan_search(user_text, tried, self.working_memory))
            except (LLMError, SearchPlanError) as e:
                result.warnings.append(f"記憶を検索できませんでした: {e}")
                break
            if round_number == 0 and plan.history_queries:
                found = search_working_memory_history(events, plan.history_queries)
                result.history_events.extend(found)
                result.history_log.append((plan.history_queries, len(found)))
            if round_number == 0 and plan.memory_history_queries:
                found = self._with_confirmation_texts(
                    search_memory_history(memory_events, plan.memory_history_queries)
                )
                result.memory_history_events.extend(found)
                result.memory_history_log.append((plan.memory_history_queries, len(found)))
            used = {normalize(q) for q in tried}
            queries = [q for q in plan.queries if normalize(q) not in used]
            if not queries or not has_long_term:
                # 長期記憶は不要という判断か、使用済みの検索語しか出てこなかったか、検索する長期記憶が無い
                break
            found = search_memories(self.memories, queries)
            found_state = search_memory_state(self.memory_state, queries)
            found_pending = search_memory_pending(pending_items, queries)
            result.search_log.append((queries, len(found) + len(found_state)))
            if found or found_state or found_pending:
                result.memories.extend(found)
                result.memory_state.extend(found_state)
                break
            tried += queries
        if result.memory_state:
            # 同じ属性に値が複数あるとき（home_location と home_location_2）、どれか1つだけで断定しないよう全部渡す
            result.memory_state = related_memory_state(self.memory_state, result.memory_state)
        if result.search_log and pending_items:
            # 回答AIが古い現在値だけで断定しないよう、関係する保留を一緒に渡す
            result.memory_pending = related_memory_pending(pending_items, result.memory_state, found_pending)
            keys = list(dict.fromkeys(item["key"] for item in result.memory_state))
            result.memory_pending_log.append((keys + result.search_log[-1][0], len(result.memory_pending)))
        return result

    def recall(self, user_text):
        """retrieve のうち従来の長期記憶の分だけを、(ヒットした記憶, 検索ログ, 警告メッセージ) で返す。"""
        result = self.retrieve(user_text)
        return result.memories, result.search_log, result.warnings

    def send(
        self,
        user_text,
        memories=(),
        history_events=(),
        memory_state=(),
        memory_history_events=(),
        memory_pending=(),
        recorded=False,
    ):
        """ユーザー発話を履歴に追加し、LLMへ渡して応答を返す。

        渡すのは作業記憶（常に全項目）、retrieve の結果（memories / history_events /
        memory_state / memory_history_events / memory_pending）、現在の会話だけ。
        recorded が真なら、その発話は確認の回答として会話と会話原文に追加済みなので、応答だけを追加する。
        """
        if not recorded:
            self.messages.append({"role": "user", "content": user_text})
        try:
            reply = self.llm.complete(
                self.messages,
                memories,
                self.working_memory,
                history_events,
                memory_state,
                memory_history_events,
                memory_pending,
            )
        except Exception:
            # 失敗した発話を残すとuserが連続するため、履歴を元に戻す（会話原文に保存済みの発話は残す）
            if not recorded:
                self.messages.pop()
            raise
        self.messages.append({"role": "assistant", "content": reply})
        if self.history_path:
            try:
                append_history(self.history_path, self.messages[-1:] if recorded else self.messages[-2:])
            except HistoryError as e:
                # 応答自体は得られているので、呼び出し側が表示できるように渡す
                e.reply = reply
                raise
        return reply

    def remember(self, user_text):
        """ユーザー発話から長期記憶を抽出して保存し、(保存した記憶, 警告メッセージ) を返す。

        抽出の入力はユーザー発話だけで、assistantの発言は渡さない。
        失敗しても例外にはせず、警告として返す（会話本体を止めないため）。
        """
        if not self.memory_path:
            return [], []
        try:
            candidates = parse_candidates(self.llm.extract_memories(user_text))
        except (LLMError, MemoryStoreError) as e:
            return [], [f"記憶を抽出できませんでした: {e}"]

        accepted = []
        warnings = []
        known = {m["text"] for m in self.memories}
        for candidate in candidates:
            reason = rejection_reason(candidate, user_text)
            if reason:
                warnings.append(f"記憶候補を保存しませんでした: {reason}")
                continue
            text = candidate["text"].strip()
            if text in known:
                continue
            known.add(text)
            # originはAIの出力を使わず、ここで決める
            accepted.append(
                {"text": text, "origin": ORIGIN_USER, "evidence": candidate["evidence"]}
            )

        if accepted:
            try:
                append_memories(self.memory_path, accepted)
            except MemoryStoreError as e:
                return [], warnings + [str(e)]
            self.memories.extend(accepted)
        return accepted, warnings

    def update_working_memory(self, user_text):
        """ユーザー発話から作業記憶の候補を提案させ、検証して反映し、(変更のリスト, 警告メッセージ) を返す。

        抽出の入力は現在の作業記憶とユーザー発話だけで、assistantの発言は渡さない。
        LLMは候補（add / replace / remove）を提案するだけで、採用の判断と状態の書き換えはここで行う。
        現在状態を保存してから変更履歴を追記する。
        失敗しても例外にはせず、警告として返す（会話本体を止めないため）。
        """
        if not self.working_memory_path:
            return [], []
        try:
            candidates = parse_working_memory_candidates(
                self.llm.extract_working_memory(user_text, self.working_memory)
            )
        except (LLMError, WorkingMemoryError) as e:
            return [], [f"作業記憶を抽出できませんでした: {e}"]

        updated, changes, warnings = apply_candidates(self.working_memory, candidates, user_text)
        if changes:
            try:
                save_working_memory(self.working_memory_path, updated)
            except WorkingMemoryError as e:
                return [], warnings + [str(e)]
            self.working_memory = updated
            if self.working_memory_history_path:
                try:
                    append_working_memory_history(self.working_memory_history_path, changes)
                except WorkingMemoryError as e:
                    # 現在状態は保存できているので、変更は有効なまま警告だけ返す
                    warnings.append(str(e))
        return changes, warnings

    def update_memory_state(self, user_text):
        """ユーザー発話と既存の値の関係をLLMに判断させ、検証して反映し、(変更のリスト, 警告メッセージ) を返す。

        判断の入力は現在値（subject / key / value）とユーザー発話だけで、assistantの発言は渡さない。
        LLMは関係（replace / coexist / pending / no_change）を判断するだけで、
        実行してよいかの確認と状態の書き換えはここで行う。確認できない置き換えは保留にする。
        変更は、現在値の変更（operation が add / replace）と、新しく保存した保留（operation が pending）。
        現在値を保存してから変更履歴を追記し、保留は別のファイルへ保存する。
        失敗しても例外にはせず、警告として返す（会話本体を止めないため）。
        """
        if not self.memory_state_path:
            return [], []
        if self.write_blocked:
            return [], [f"長期状態の書き込みを停止しています（{self.write_blocked}）。再起動してください"]
        try:
            candidates, recovered = parse_extraction(
                self.llm.extract_structured_memory(user_text, self.memory_state)
            )
        except (LLMError, StructuredMemoryError) as e:
            return [], [f"長期状態を抽出できませんでした: {e}"]

        warnings = []
        if recovered:
            warnings.append("長期状態の抽出結果にJSON以外の前置きがあったため、末尾のJSONだけを解析しました")
        timestamp = timestamp_now()
        updated, results, rejected = apply_structured_candidates(
            self.memory_state, candidates, user_text, timestamp
        )
        warnings += rejected
        changes = [c for c in results if c["operation"] in (OP_ADD, OP_REPLACE)]
        held = [c for c in results if c["operation"] == OP_PENDING]
        if changes:
            try:
                save_memory_state(self.memory_state_path, updated)
            except StructuredMemoryError as e:
                changes = []
                warnings.append(str(e))
            else:
                self.memory_state = updated
                if self.memory_history_path:
                    try:
                        append_memory_history(self.memory_history_path, changes, timestamp)
                    except StructuredMemoryError as e:
                        # 現在値は保存できているので、変更は有効なまま警告だけ返す
                        warnings.append(str(e))
                        if isinstance(e, PartialWriteError):
                            # 変更履歴の末尾が壊れている可能性があるので、以後は長期状態を書き換えない
                            self.write_blocked = str(e)
        added = []
        if held and self.memory_pending_path:
            pending, added = add_pending(self.memory_pending, held, timestamp)
            if added:
                try:
                    save_memory_pending(self.memory_pending_path, pending)
                except StructuredMemoryError as e:
                    added = []
                    warnings.append(str(e))
                else:
                    self.memory_pending = pending
        return changes + [{"operation": OP_PENDING, **item} for item in added], warnings
