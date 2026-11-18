"""会話。現在のプロセス内の履歴はメモリ上に保持し、会話原文・長期記憶・作業記憶をファイルへ保存する。"""

from dataclasses import dataclass, field

from .confirmation import (
    APPROVAL_INPUTS,
    CONFIRMATION_ID_PATTERN,
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
from .llm import LLMError, format_attribute_list
from .memory import (
    ORIGIN_USER,
    MemoryStoreError,
    append_memories,
    load_memories,
    parse_candidates,
    rejection_reason,
)
from .revert import (
    RevertError,
    append_revert,
    complete_revert,
    load_reverts,
    next_revert_id,
    plan_revert,
    reconcile_revert,
    revert_candidates,
    revert_events_of,
    revert_ids,
    revert_progress,
    revert_prompt,
)
from .search import (
    HISTORY_SEARCH_KEYS,
    MAX_ATTRIBUTE_HISTORY_EVENTS,
    MAX_ATTRIBUTES,
    MAX_CATALOG_ATTRIBUTES,
    MAX_HISTORY_ATTRIBUTES,
    MAX_ROUNDS,
    MEMORY_HISTORY_SEARCH_KEYS,
    MEMORY_SEARCH_KEYS,
    PENDING_SEARCH_KEYS,
    STATE_SEARCH_KEYS,
    SearchPlanError,
    matching_records,
    normalize,
    parse_plan,
    rank_memory_history,
    search_memories,
    search_memory_pending,
    search_memory_state,
    search_working_memory_history,
)
from .structured_memory import (
    COVERAGE_COMPLETE,
    COVERAGE_UNAVAILABLE,
    EXTRACTION_MAX_VALUE_ATTRIBUTES,
    OP_ADD,
    OP_PENDING,
    OP_REPLACE,
    PENDING_STATUS_UNRESOLVED,
    PartialWriteError,
    StructuredMemoryError,
    add_pending,
    append_memory_history,
    attribute_catalog,
    attribute_history,
    attribute_of,
    extraction_view,
    load_memory_history,
    load_memory_pending,
    load_memory_state,
    find_item,
    parse_extraction,
    pending_attributes,
    related_memory_pending,
    related_memory_state,
    resolve_attribute,
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
    memory_history_events: list = field(default_factory=list)  # 属性ごとの長期記憶の変更履歴（属性の順、属性内は古い順）
    memory_history_log: list = field(default_factory=list)  # 検索しなかったターンは空。件数はヒットしたイベント数
    # 変更履歴を渡した属性と、それぞれ全件を渡したか。変更履歴を検索しなかったターンは None
    # {"attributes": [{"subject", "key", "shown", "total"}, ...], "omitted_attributes": 変更履歴を渡さなかった属性の数}
    memory_history_scope: dict = None
    memory_pending: list = field(default_factory=list)  # 回答に渡す構造化した長期記憶の保留（保存順）
    memory_pending_log: list = field(default_factory=list)  # 保留を検索しなかったターンは空
    history_events: list = field(default_factory=list)  # ヒットした作業記憶の変更履歴（古い順）
    history_log: list = field(default_factory=list)  # 検索しなかったターンは空。読み込めなかったときの件数は None
    warnings: list = field(default_factory=list)
    # 検索プランナーへ渡した属性一覧。{"attributes", "total", "provided", "limit", "chars"}
    # provided が偽なら、上限を超えたため一覧を渡さず、検索語だけで属性を探した（chars はそのとき渡したブロックの文字数）
    attribute_list: dict = None
    selected_attributes: list = field(default_factory=list)  # 属性一覧から選ばれ、一覧と完全一致した属性
    rejected_attributes: list = field(default_factory=list)  # 一覧と一致しなかった選択の (識別子, 理由)
    ignored_attributes: list = field(default_factory=list)  # 使わなかった選択（不要という判断と一緒に返された、上限超えなど）
    auto_history_attributes: list = field(default_factory=list)  # R4 で、プランナーの判断によらず変更履歴を渡した属性
    # MVP15: 最後に使った長期記憶の検索語が、現在値か保留に一致した属性（一致の順）。抽出AIへ値を渡す属性の候補
    query_attributes: list = field(default_factory=list)
    status: dict = None  # 回答AIへ渡す検索の状態（llm.format_retrieval_status）。検索を始めていなければ None


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


@dataclass
class RevertResult:
    """確定の取り消しで、y/N の入力を1つ受け取った結果。status は ConfirmationResult と同じ値
    （applied / declined / rejected / failed）を使う。"""

    status: str
    revert_id: str
    confirmation_id: str  # 取り消す確認
    reason: str = None  # 取り消さなかった理由
    plan: dict = None  # 実行した取り消しの計画
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
        memory_reverts_path=None,
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
        memory_reverts_path を渡すと、確定の取り消し（/revert）を使えるようにし、取り消しログを読み込む。
        確認と同じく、変更計画を書いたまま結果が書かれていない取り消しを、会話を始める前に片付ける
        （片付けられなければ RevertError で起動を止める）。
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
        self.memory_reverts_path = memory_reverts_path
        self.reverts = load_reverts(memory_reverts_path) if memory_reverts_path else []
        # 取り消しの途中の状態も、確認と同じくプロセス内だけに持つ
        self.prepared_revert = None  # 変更内容を決めて prepared を書き、出力の成功を待っている
        self.awaiting_revert = None  # 変更内容の出力処理が正常に終わり、y/N を待っている
        # 反映の途中で失敗して復旧できなかったときの理由。設定されている間は長期状態を書き換えない
        self.write_blocked = None
        self.turn = 0  # 入力のターンの番号（AI呼び出しの記録に付ける）
        self.last_extraction_view = None  # 直近の update_memory_state で抽出AIへ渡した範囲（structured_memory.extraction_view）
        self.startup_messages = []
        if memory_confirmations_path and memory_state_path and memory_pending_path:
            self._recover_confirmations()
        if self._revert_ready():
            self._recover_reverts()

    def begin_turn(self):
        """入力1つの処理を始める。ターンの番号を進め、AI呼び出しの記録に付けられるようにする。"""
        self.turn += 1
        if hasattr(self.llm, "turn"):
            self.llm.turn = self.turn

    # --- 保留の確認 ---

    def unresolved_pending(self):
        return [p for p in self.memory_pending if p["status"] == PENDING_STATUS_UNRESOLVED]

    def _pending(self, pending_id):
        return next((p for p in self.memory_pending if p["id"] == pending_id), None)

    def _history(self):
        """長期状態の変更履歴を読み込む。保存先が無ければ空。読めなければ StructuredMemoryError。"""
        return load_memory_history(self.memory_history_path) if self.memory_history_path else []

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
        return self._append_conversation(
            {
                "role": role,
                "content": content,
                "confirmation_id": confirmation_id,
                "confirmation_event": confirmation_event,
            }
        )

    def _append_conversation(self, entry):
        """確認・取り消しの発言1行を会話原文へ追記する。失敗したら HistoryError を、成功したら None を返す。"""
        if not self.history_path:
            return None
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
        if self.prepared_revert:
            self.abandon_prepared_revert("出力されないまま、確認が始められました")
        if self.awaiting_revert:
            raise ConfirmationError("取り消しの承認を待っているため、確認できません")
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
                    plan = plan_resolution(outcome, pending, self.memory_state, timestamp_now(), self._history())
                except StructuredMemoryError as e:
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
                plan = plan_resolution(OUTCOME_REPLACE, pending, self.memory_state, timestamp_now(), self._history())
            except StructuredMemoryError as e:
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

    # --- 確定の取り消し ---

    def _revert_ready(self):
        return bool(
            self.memory_reverts_path
            and self.memory_confirmations_path
            and self.memory_state_path
            and self.memory_history_path
            and self.memory_pending_path
        )

    def _revert_log(self, name, revert_id, confirmation_id, **fields):
        """取り消しログへイベントを追記し、追記できたものだけをメモリ上にも持つ。"""
        event = {
            "event": name,
            "revert_id": revert_id,
            "target_confirmation_id": confirmation_id,
            "timestamp": timestamp_now(),
            **fields,
        }
        try:
            append_revert(self.memory_reverts_path, event)
        except PartialWriteError as e:
            # 取り消しログの末尾が壊れている可能性があるので、以後は長期状態を書き換えない
            self.write_blocked = str(e)
            raise RevertError(str(e)) from e
        self.reverts.append(event)
        return event

    def _revert_log_quietly(self, name, waiting, warnings=None, **fields):
        try:
            self._revert_log(name, waiting["revert_id"], waiting["confirmation_id"], **fields)
        except RevertError as e:
            if warnings is not None:
                warnings.append(str(e))

    def _recover_reverts(self):
        """前回の起動で終わらなかった取り消しを片付ける。Claude は呼ばない。

        変更計画を書いた取り消しは、現在値の全体が計画の実行後なら残りを実行して applied、実行前なら failed にする。
        表示・承認の途中で終わった取り消しは、続きを行わないことを expired として記録する。
        """
        for revert_id in revert_ids(self.reverts):
            events = revert_events_of(self.reverts, revert_id)
            stage = revert_progress(events)
            confirmation_id = events[0]["target_confirmation_id"]
            if stage == "committed":
                plan = [e for e in events if e["event"] == EVENT_COMMIT][-1]["plan"]
                result, self.memory_state, self.memory_pending = reconcile_revert(
                    plan,
                    self.memory_state,
                    self.memory_pending,
                    self.memory_state_path,
                    self.memory_history_path,
                    self.memory_pending_path,
                )
                self._revert_log(result, revert_id, confirmation_id, recovered=True)
                done = "続きを実行して取り消しました" if result == EVENT_APPLIED else "反映されていなかったため、取り消しませんでした"
                self.startup_messages.append(f"{revert_id}（確認 {confirmation_id} の取り消し）: 前回途中で止まった取り消しを確認し、{done}")
            elif stage == "open":
                if any(e["event"] == EVENT_PRESENTED for e in events):
                    reason = "再起動を挟んだため、続きを行いません"
                else:
                    reason = "変更内容を表示した記録が無いまま終わったため、続きを行いません"
                self._revert_log(EVENT_EXPIRED, revert_id, confirmation_id, reason=reason)
                self.startup_messages.append(
                    f"{revert_id}（確認 {confirmation_id} の取り消し）: 前回の取り消しは承認の前に終わったため、続きは行いません（{reason}）"
                )

    def revert_overview(self):
        """取り消しの対象になりうる確認ごとに、(確認 ID, 確認の変更計画, 取り消せない理由か None) を返す。"""
        if not self._revert_ready():
            raise RevertError("取り消しログ・確認ログ・長期状態・変更履歴・長期保留の保存先が設定されていないため、取り消せません")
        return revert_candidates(
            self.confirmations, self.reverts, self.memory_state, self._history(), self.memory_pending
        )

    def pending_reverts(self, pending_id):
        """その保留を解決した確認についての、取り消しログのイベント。"""
        confirmation_ids_ = {e["confirmation_id"] for e in self.confirmations if e["pending_id"] == pending_id}
        return [e for e in self.reverts if e["target_confirmation_id"] in confirmation_ids_]

    def _files_problem(self):
        """ファイル上の長期状態・長期保留が、このセッションの持っている内容と同じか。違えば理由を返す。"""
        try:
            if load_memory_state(self.memory_state_path) != self.memory_state:
                return "長期状態ファイルが、このセッションの外で変更されています"
            if load_memory_pending(self.memory_pending_path) != self.memory_pending:
                return "長期保留ファイルが、このセッションの外で変更されています"
        except StructuredMemoryError as e:
            return f"ファイルを確かめられません: {e}"
        return None

    def _plan_revert(self, revert_id, confirmation_id):
        """取り消しの計画を、今の状態から作る。取り消せなければ RevertError（理由つき）。"""
        problem = self._files_problem()
        if problem:
            raise RevertError(problem)
        try:
            history = self._history()
        except StructuredMemoryError as e:
            raise RevertError(f"変更履歴が読めないため、取り消せません（{e}）") from e
        return plan_revert(
            revert_id,
            confirmation_id,
            self.confirmations,
            self.reverts,
            self.memory_state,
            history,
            self.memory_pending,
            timestamp_now(),
        )

    def start_revert(self, confirmation_id):
        """確認 confirmation_id の取り消しを準備し、表示する内容を dict で返す。始められなければ RevertError。

        取り消せるかを確かめて計画を作り、y/N の文面を決めて、取り消しログへ prepared を書く（書けなければ表示しない）。
        ここではまだ y/N を待たない。呼び出し側は文面を出力し、出力処理が正常に終わったときだけ mark_revert_presented を、
        失敗したときは abandon_prepared_revert を呼ぶ。
        """
        if self.prepared_revert:
            self.abandon_prepared_revert("出力されないまま、次の取り消しが始められました")
        if self.prepared_confirmation:
            self.abandon_prepared("出力されないまま、取り消しが始められました")
        if not self._revert_ready():
            raise RevertError("取り消しログ・確認ログ・長期状態・変更履歴・長期保留の保存先が設定されていないため、取り消せません")
        if self.write_blocked:
            raise RevertError(f"長期状態の書き込みを停止しているため、取り消せません（{self.write_blocked}）。再起動してください")
        if self.active_confirmation or self.awaiting_approval or self.awaiting_revert:
            raise RevertError("ほかの確認・取り消しの途中のため、取り消せません")
        if not CONFIRMATION_ID_PATTERN.fullmatch(confirmation_id or ""):
            raise RevertError(f"確認の ID の形式が不正です: {confirmation_id}（例: /revert c001）。一覧は /revert")
        if self.history_path:
            try:
                # 会話原文に途中までの行などが残っていれば、その後ろに取り消しの記録を足さない
                load_history(self.history_path)
            except HistoryError as e:
                raise RevertError(f"会話原文が読めないため、取り消せません（{e}）") from e
        revert_id = next_revert_id(self.reverts)
        plan = self._plan_revert(revert_id, confirmation_id)
        prompt = revert_prompt(plan)
        self._revert_log(EVENT_PREPARED, revert_id, confirmation_id, plan=plan, prompt=prompt)
        self.prepared_revert = {"revert_id": revert_id, "confirmation_id": confirmation_id, "prompt": prompt}
        return {"revert_id": revert_id, "confirmation_id": confirmation_id, "plan": plan, "prompt": prompt}

    def mark_revert_presented(self, revert_id):
        """y/N の文面の出力処理が正常に終わったあとに呼ぶ。presented を書き、会話原文へ残し、y/N 待ちにする。

        presented を書けない、または会話原文に文面を保存できなければ RevertError で、y/N を待たない。
        """
        prepared = self.prepared_revert
        if prepared is None or prepared["revert_id"] != revert_id:
            raise RevertError(f"表示を記録する取り消し {revert_id} が準備されていません")
        self.prepared_revert = None
        try:
            self._revert_log(EVENT_PRESENTED, revert_id, prepared["confirmation_id"], prompt=prepared["prompt"])
        except RevertError as e:
            raise RevertError(f"変更内容を表示したことを記録できなかったため、取り消しを中止しました（{e}）") from e
        error = self._append_conversation(
            {"role": "assistant", "content": prepared["prompt"], "revert_id": revert_id, "revert_event": EVENT_PRESENTED}
        )
        if error:
            self._revert_log_quietly(EVENT_CANCELLED, prepared, reason=f"会話原文に変更内容を保存できませんでした: {error}")
            raise RevertError(f"会話原文に変更内容を保存できなかったため、取り消しを中止しました（{error}）") from error
        self.awaiting_revert = dict(prepared)

    def abandon_prepared_revert(self, reason):
        """準備した文面を出力できなかったときに呼ぶ。y/N を待たず、取り消しを記録し、警告メッセージを返す。"""
        prepared = self.prepared_revert
        self.prepared_revert = None
        if prepared is None:
            return []
        warnings = []
        # 書けなくても、prepared だけが残り、次の起動時に expired になる
        self._revert_log_quietly(EVENT_CANCELLED, prepared, warnings, reason=reason)
        return warnings

    def cancel_revert(self, reason):
        """y/N を待っている取り消しを、実行せずに閉じる。警告メッセージを返す。待っていなければ何もしない。"""
        warnings = self.abandon_prepared_revert(reason)
        waiting = self.awaiting_revert
        self.awaiting_revert = None
        if waiting:
            self._revert_log_quietly(EVENT_CANCELLED, waiting, warnings, reason=reason)
        return warnings

    def approve_revert(self, user_text):
        """取り消しの y/N への入力を受け取り、RevertResult を返す。y（Y）のときだけ実行する。

        入力は会話原文に保存できたときだけ使う（保存できなければ y でも実行しない）。
        y なら、今の状態から計画を作り直し、表示した文面と同じ内容になるときだけ、承認を書いてから実行する。
        """
        waiting = self.awaiting_revert
        self.awaiting_revert = None
        revert_id, confirmation_id = waiting["revert_id"], waiting["confirmation_id"]
        approved = user_text.strip() in APPROVAL_INPUTS
        error = self._append_conversation(
            {"role": "user", "content": user_text, "revert_id": revert_id, "revert_event": EVENT_APPROVAL}
        )
        if error:
            # 承認の入力を残せなければ、approval を書かず、y でも実行しない（再起動後も承認として扱わない）
            self._revert_log_quietly(EVENT_CANCELLED, waiting, reason=f"承認の入力を会話原文に保存できませんでした: {error}")
            return RevertResult(
                CONFIRMATION_FAILED,
                revert_id,
                confirmation_id,
                reason=f"承認の入力を会話原文に保存できなかったため、取り消しませんでした（{error}）",
            )
        warnings = []
        plan = None
        problem = None
        if approved:
            try:
                plan = self._plan_revert(revert_id, confirmation_id)
            except StructuredMemoryError as e:
                problem = str(e)
            else:
                if revert_prompt(plan) != waiting["prompt"]:
                    problem = "変更内容を表示した後に状態が変わったため、表示した内容のとおりには取り消せません"
        try:
            self._revert_log(EVENT_APPROVAL, revert_id, confirmation_id, prompt=waiting["prompt"], input=user_text, approved=approved)
        except RevertError as e:
            return RevertResult(CONFIRMATION_FAILED, revert_id, confirmation_id, reason=str(e))
        if not approved:
            return RevertResult(CONFIRMATION_DECLINED, revert_id, confirmation_id)
        if problem:
            self._revert_log_quietly(EVENT_FAILED, waiting, warnings, error=problem)
            return RevertResult(CONFIRMATION_REJECTED, revert_id, confirmation_id, reason=problem, warnings=warnings)
        return self._commit_revert(revert_id, confirmation_id, plan, warnings)

    def _commit_revert(self, revert_id, confirmation_id, plan, warnings):
        """取り消しの計画を取り消しログへ書いてから実行する。途中で失敗したら、その場で1回だけ復旧を試みる。"""
        waiting = {"revert_id": revert_id, "confirmation_id": confirmation_id}
        if self.write_blocked:
            reason = f"長期状態の書き込みを停止しているため、取り消しませんでした（{self.write_blocked}）"
            self._revert_log_quietly(EVENT_FAILED, waiting, warnings, error=reason)
            return RevertResult(CONFIRMATION_FAILED, revert_id, confirmation_id, reason=reason, warnings=warnings)
        try:
            self._revert_log(EVENT_COMMIT, revert_id, confirmation_id, plan=plan)
        except RevertError as e:
            return RevertResult(CONFIRMATION_FAILED, revert_id, confirmation_id, reason=str(e), warnings=warnings)
        try:
            state, pending = complete_revert(
                plan,
                self.memory_state,
                self.memory_pending,
                self.memory_state_path,
                self.memory_history_path,
                self.memory_pending_path,
            )
        except StructuredMemoryError as e:
            return self._recover_revert_after_failure(revert_id, confirmation_id, plan, e, warnings)
        self.memory_state, self.memory_pending = state, pending
        try:
            self._revert_log(EVENT_APPLIED, revert_id, confirmation_id)
        except RevertError as e:
            warnings.append(f"{e}（取り消しは反映済みです。次の起動時に取り消しログへ記録を補います）")
        return RevertResult(CONFIRMATION_APPLIED, revert_id, confirmation_id, plan=plan, warnings=warnings)

    def _recover_revert_after_failure(self, revert_id, confirmation_id, plan, error, warnings):
        """取り消しの途中の失敗のあと、ファイルを読み直して片付ける。片付けられなければ長期状態の書き込みを止める。"""
        try:
            state = load_memory_state(self.memory_state_path)
            pending = load_memory_pending(self.memory_pending_path)
            result, state, pending = reconcile_revert(
                plan,
                state,
                pending,
                self.memory_state_path,
                self.memory_history_path,
                self.memory_pending_path,
            )
            self._revert_log(result, revert_id, confirmation_id, recovered=True, error=str(error))
        except StructuredMemoryError as e:
            self.write_blocked = f"{revert_id} の取り消しが途中で止まりました: {e}"
            return RevertResult(
                CONFIRMATION_FAILED,
                revert_id,
                confirmation_id,
                reason=f"{error}。続きも実行できませんでした（{e}）。長期状態の書き込みを停止しました。再起動してください",
                warnings=warnings,
            )
        self.memory_state, self.memory_pending = state, pending
        if result == EVENT_APPLIED:
            warnings.append(f"取り消しの途中で失敗しましたが、続きを実行して取り消しました: {error}")
            return RevertResult(CONFIRMATION_APPLIED, revert_id, confirmation_id, plan=plan, warnings=warnings)
        return RevertResult(CONFIRMATION_FAILED, revert_id, confirmation_id, reason=str(error), warnings=warnings)

    def _with_confirmation_texts(self, events):
        """変更履歴のイベントに、確認ログから質問・回答・承認の原文を付けたコピーを返す（回答AIに渡すため）。

        取り消し（revert）のイベントは confirmation_id を持たないので、取り消した確認の質問・回答は付かない。
        代わりに、取り消しログから取り消しの承認の入力を付ける。
        """
        texts = {}
        for event in self.confirmations:
            entry = texts.setdefault(event["confirmation_id"], {})
            if event["event"] == EVENT_PRESENTED:
                entry["confirmation_question"] = event.get("question")
            elif event["event"] == EVENT_JUDGED:
                entry["confirmation_answer"] = event.get("answer")
            elif event["event"] == EVENT_APPROVAL and event.get("approved"):
                entry["approval_input"] = event.get("input")
        revert_inputs = {
            event["revert_id"]: event.get("input")
            for event in self.reverts
            if event["event"] == EVENT_APPROVAL and event.get("approved")
        }
        result = []
        for event in events:
            event = {**event, **texts.get(event.get("confirmation_id"), {})}
            if event.get("revert_id") in revert_inputs:
                event["revert_approval_input"] = revert_inputs[event["revert_id"]]
            result.append(event)
        return result

    def retrieve(self, user_text):
        """ユーザー発話に答えるために必要な長期記憶と2つの変更履歴を検索し、Retrieval を返す。

        どれを検索するかと検索語はLLMに1回の検索プランで考えさせ、検索と回数の制御はここで行う。
        発話の意味はここでは解釈しない。
        長期記憶は、従来の記憶と構造化した現在値・保留を同じ検索語で検索し、
        どれも0件なら1回だけ検索語を変えて再検索する。変更履歴の検索はそれぞれ1回だけ。
        構造化した長期記憶の検索は、関係する属性（subject と番号の無い key）を見つけるためのもので、
        現在値・保留・長期記憶の変更履歴のどれかでヒットした属性について、その属性の現在値と未解決の保留を集め、
        長期記憶の変更履歴の検索が必要なターンでは、その属性の変更履歴を検索語によらず古い順に集める（_expand_history）。
        失敗しても例外にはせず、警告として返す（会話本体を止めないため）。

        MVP14:
        - 現在値と未解決の保留から属性一覧（識別子だけ）を作り、MAX_CATALOG_ATTRIBUTES 以下なら検索プランナーへ渡す。
          プランナーが attributes で選んだ識別子は一覧と完全一致で照らし合わせ（resolve_attribute）、
          一致したものだけを、検索でヒットした属性より前に、属性単位で集める起点にする。
        - R4: 長期記憶の検索語のうち、現在値には一致せず、保留か変更履歴に一致した語がある属性は、
          プランナーが変更履歴を不要と判断していても、その属性の変更履歴を渡す（_non_current_attributes）。
        - 範囲ごとの検索の状態を status にまとめる（_retrieval_status）。
        """
        result = Retrieval()
        events = []
        # 読めなかった変更履歴を「記録が無い」として扱わないよう、読めたかどうかを区別する
        events_loaded = False
        if self.working_memory_history_path:
            try:
                events = load_working_memory_history(self.working_memory_history_path)
                events_loaded = True
            except WorkingMemoryError as e:
                result.warnings.append(str(e))
        memory_events = []
        memory_events_loaded = False
        if self.memory_history_path:
            try:
                memory_events = load_memory_history(self.memory_history_path)
                memory_events_loaded = True
            except StructuredMemoryError as e:
                result.warnings.append(str(e))
        # 解決済みの保留は回答AIへ渡さない（解決の結果は現在値と変更履歴にある）
        pending_items = self.unresolved_pending()
        catalog = attribute_catalog(self.memory_state, pending_items)
        provided = len(catalog) <= MAX_CATALOG_ATTRIBUTES
        result.attribute_list = {
            "attributes": catalog if provided else [],
            "total": len(catalog),
            "provided": provided,
            "limit": MAX_CATALOG_ATTRIBUTES,
        }
        result.attribute_list["chars"] = len(format_attribute_list(result.attribute_list))
        facts = {
            "events_loaded": events_loaded,
            "events": events,
            "memory_events_loaded": memory_events_loaded,
            "memory_events": memory_events,
            "pending_items": pending_items,
        }
        has_long_term = bool(self.memories or self.memory_state or pending_items)
        if not has_long_term and not events and not memory_events:
            # 検索できるものが無い（読めなかったものは unavailable として伝える）
            result.status = self._retrieval_status(result, "skipped", facts)
            return result
        tried = []
        found_pending = []
        history_hits = None  # 長期記憶の変更履歴を検索したターンだけ、ヒットしたイベント（順位順）
        history_queries = []
        chosen = []  # 属性一覧から選ばれ、一覧と完全一致した属性（選ばれた順）
        auto = []  # R4: 現在値には一致せず、保留か変更履歴に一致した検索語がある属性
        evidence = set()  # 検索語が現在値か保留に一致した属性（件数の上限を付けずに調べる）
        search = "not_needed"
        for round_number in range(MAX_ROUNDS):
            try:
                plan = parse_plan(
                    self.llm.plan_search(user_text, tried, self.working_memory, result.attribute_list)
                )
            except (LLMError, SearchPlanError) as e:
                result.warnings.append(f"記憶を検索できませんでした: {e}")
                search = "failed" if round_number == 0 else "retry_failed"
                break
            result.ignored_attributes += plan.ignored_attributes
            # 一覧を渡さなかったとき（上限超え）も、実際の一覧との完全一致だけを認める
            for identifier in plan.attributes:
                attribute, reason = resolve_attribute(identifier, catalog)
                if attribute is None:
                    result.rejected_attributes.append((identifier, reason))
                elif attribute in chosen:
                    continue
                elif len(chosen) < MAX_ATTRIBUTES:
                    chosen.append(attribute)
                else:
                    result.ignored_attributes.append(identifier)
            if round_number == 0 and (
                plan.queries or plan.attributes or plan.history_queries or plan.memory_history_queries
            ):
                search = "done"
            if round_number == 0 and plan.history_queries:
                found = search_working_memory_history(events, plan.history_queries)
                result.history_events.extend(found)
                # 読めなかった変更履歴は「0件」ではない
                result.history_log.append((plan.history_queries, len(found) if events_loaded else None))
                facts["working_memory_history_total"] = len(
                    matching_records(events, plan.history_queries, HISTORY_SEARCH_KEYS)
                )
            if round_number == 0 and plan.memory_history_queries:
                history_queries = plan.memory_history_queries
                history_hits = rank_memory_history(memory_events, history_queries)
                result.memory_history_log.append(
                    (history_queries, len(history_hits) if memory_events_loaded else None)
                )
            used = {normalize(q) for q in tried}
            queries = [q for q in plan.queries if normalize(q) not in used]
            if not queries or not has_long_term:
                # 長期記憶は不要という判断か、使用済みの検索語しか出てこなかったか、検索する長期記憶が無い
                break
            found = search_memories(self.memories, queries)
            found_state = search_memory_state(self.memory_state, queries)
            found_pending = search_memory_pending(pending_items, queries)
            auto = self._non_current_attributes(queries, pending_items, memory_events)
            evidence = {
                attribute_of(item["subject"], item["key"])
                for item in matching_records(self.memory_state, queries, STATE_SEARCH_KEYS)
            } | {
                attribute
                for pending in matching_records(pending_items, queries, PENDING_SEARCH_KEYS)
                for attribute in pending_attributes(pending)
            }
            result.search_log.append((queries, len(found) + len(found_state)))
            facts["memories_total"] = len(matching_records(self.memories, queries, MEMORY_SEARCH_KEYS))
            # 属性を選んだ、または変更履歴だけに一致した（R4）ときも、検索語を変えて探し直す必要はない
            if found or found_state or found_pending or chosen or auto:
                result.memories.extend(found)
                result.memory_state.extend(found_state)
                break
            tried += queries
        result.selected_attributes = list(chosen)
        if result.search_log:
            last_queries = result.search_log[-1][0]
            result.query_attributes = list(
                dict.fromkeys(
                    [
                        attribute_of(item["subject"], item["key"])
                        for item in search_memory_state(self.memory_state, last_queries, EXTRACTION_MAX_VALUE_ATTRIBUTES)
                    ]
                    + [attribute for pending in found_pending for attribute in pending_attributes(pending)]
                )
            )
        hits = (
            [attribute_of(item["subject"], item["key"]) for item in result.memory_state]
            + [attribute for pending in found_pending for attribute in pending_attributes(pending)]
        )
        # 検索語との一致の順位（現在値、保留、R4、変更履歴の順に、それぞれのヒットの順位順）
        ranked = list(
            dict.fromkeys(
                hits
                + auto
                + [
                    attribute_of(event["subject"], event["key"])
                    for event in history_hits or ()
                    if isinstance(event.get("subject"), str) and isinstance(event.get("key"), str)
                ]
            )
        )
        # 属性の上限（MAX_ATTRIBUTES）と変更履歴の上限（MAX_HISTORY_ATTRIBUTES）で落とす順を決める並び。
        # 選ばれ、検索語にも一致した属性（一致の順位順）→ 検索語で現在値・保留に一致した属性 → R4 の属性 →
        # 選ばれただけの属性（選ばれた順）→ 変更履歴だけに一致した属性。
        # プランナーは一覧の順に、関係の薄い属性まで選ぶことがあるため、選んだだけの属性は検索語に一致した属性の後に置く
        # （上限を超える数が選ばれても、検索語に一致した属性の現在値と変更履歴を落とさない）
        # 「検索語にも一致した」は件数の上限の前で判定する（上限で検索結果から外れた現在値でも、一致は一致）
        matched = [a for a in ranked if a in chosen] + [a for a in chosen if a not in ranked and a in evidence]
        unmatched = [a for a in chosen if a not in matched]
        direct = matched + hits + unmatched
        attributes = list(dict.fromkeys(matched + hits + auto + unmatched + ranked))
        with_history = []
        if (history_hits is not None or auto) and memory_events_loaded:
            # プランナーが変更履歴を必要と判断したら、MVP13 と同じくすべての属性が対象。
            # 不要と判断していても、R4 の属性だけは対象にする（件数の上限と省略の表し方は MVP13 と同じ）
            eligible = None if history_hits is not None else set(auto)
            with_history = self._expand_history(result, memory_events, attributes, eligible)
            if history_hits is None:
                result.auto_history_attributes = list(with_history)
        # 現在値・保留でヒットした属性と、変更履歴を渡す属性。変更履歴だけでヒットし、変更履歴を渡さない属性は含めない
        # （「記憶はどう変わった？」のように属性を特定しない検索語で、無関係な属性の現在値まで渡さないため）
        selected = [a for a in attributes[:MAX_ATTRIBUTES] if a in direct or a in with_history]
        # 同じ属性に値が複数あるとき（home_location と home_location_2）、どれか1つだけで断定しないよう全部渡す。
        # 保留や変更履歴だけでヒットした属性も、その属性の現在値を渡す
        result.memory_state = related_memory_state(self.memory_state, result.memory_state, selected)
        # 状態として伝える「特定できた属性」は、実際に現在値か保留・変更履歴を渡す属性
        # （検索語で直接ヒットした現在値は、属性の上限を超えても渡る: related_memory_state）。
        # 省いた属性は、一致したのに現在値を1つも渡せなかった属性だけを数える
        passed = [attribute_of(item["subject"], item["key"]) for item in result.memory_state]
        facts["identified"] = list(dict.fromkeys(selected + passed))
        stored = {attribute_of(item["subject"], item["key"]) for item in self.memory_state}
        facts["omitted_state_attributes"] = len(
            [a for a in dict.fromkeys(direct) if a in stored and a not in facts["identified"]]
        )
        if pending_items and (result.search_log or selected):
            # 回答AIが古い現在値だけで断定しないよう、ヒットした属性につながる保留を一緒に渡す
            result.memory_pending = related_memory_pending(pending_items, selected, found_pending)
            keys = list(dict.fromkeys(item["key"] for item in result.memory_state))
            terms = result.search_log[-1][0] if result.search_log else history_queries
            result.memory_pending_log.append((keys + terms, len(result.memory_pending)))
            linked = {p["id"] for p in pending_items if set(selected).intersection(pending_attributes(p))}
            facts["pending_total"] = len(linked | {p["id"] for p in found_pending})
        facts["long_term_searched"] = bool(
            result.search_log or chosen or result.rejected_attributes or history_hits is not None
        )
        facts["history_attempted"] = history_hits is not None or bool(auto)
        result.status = self._retrieval_status(result, search, facts)
        return result

    def _non_current_attributes(self, queries, pending_items, memory_events):
        """R4。検索語ごとに、現在値には一致せず、未解決の保留か変更履歴に一致した属性を、重複なく返す。

        検索語ごとに判定するので、属性一覧からの選択や、別の検索語で現在値にも一致したことでは無効にならない
        （例: 「名古屋」が保留だけに一致すれば、同じ属性の現在値が別の語で見つかっていても対象）。
        件数の上限で現在値の一致を取りこぼさないよう、上限を付けずに一致を調べる。
        """
        found = []
        for query in queries:
            in_state = {
                attribute_of(item["subject"], item["key"])
                for item in matching_records(self.memory_state, [query], STATE_SEARCH_KEYS)
            }
            elsewhere = [
                attribute
                for pending in matching_records(pending_items, [query], PENDING_SEARCH_KEYS)
                for attribute in pending_attributes(pending)
            ] + [
                attribute_of(event["subject"], event["key"])
                for event in matching_records(memory_events, [query], MEMORY_HISTORY_SEARCH_KEYS)
                if isinstance(event.get("subject"), str) and isinstance(event.get("key"), str)
            ]
            found += [attribute for attribute in elsewhere if attribute not in in_state]
        return list(dict.fromkeys(found))

    def _retrieval_status(self, result, search, facts):
        """範囲（現在値・保留・変更履歴・従来の記憶・作業記憶の変更履歴）ごとの検索の状態をまとめる。

        状態は found / no_match / not_searched / unavailable / none_stored（変更履歴は not_included も）。
        none_stored は、読み込めた（または起動時に読み込み済みの）範囲が本当に空のときだけ使う。
        読み込めなかった範囲は、検索したかどうかによらず unavailable。
        保存先が設定されていない範囲は含めない。値・ファイルパス・例外のメッセージは含めない。
        """
        searched = search in ("done", "retry_failed")
        failed = search == "failed"

        def scope_status(stored, loaded, attempted, count, **omitted):
            if not loaded:
                return {"result": "unavailable"}
            if not stored:
                return {"result": "none_stored"}
            if failed:
                return {"result": "unavailable"}
            if not (searched and attempted):
                return {"result": "not_searched"}
            if count:
                return {"result": "found", "count": count, **{k: v for k, v in omitted.items() if v > 0}}
            return {"result": "no_match"}

        long_term = facts.get("long_term_searched", False)
        scopes = []
        if self.memory_state_path:
            scopes.append(("state", scope_status(
                self.memory_state, True, long_term, len(result.memory_state),
                omitted_attributes=facts.get("omitted_state_attributes", 0),
            )))
        if self.memory_pending_path:
            scopes.append(("pending", scope_status(
                facts["pending_items"], True, long_term, len(result.memory_pending),
                omitted=facts.get("pending_total", 0) - len(result.memory_pending),
            )))
        if self.memory_history_path:
            history = result.memory_history_scope or {"attributes": [], "omitted_attributes": 0}
            status = scope_status(
                facts["memory_events"], facts["memory_events_loaded"], facts.get("history_attempted", False),
                len(history["attributes"]), omitted_attributes=history["omitted_attributes"],
            )
            if status["result"] == "no_match" and history["omitted_attributes"]:
                status = {"result": "not_included", "omitted_attributes": history["omitted_attributes"]}
            scopes.append(("history", status))
        if self.memory_path:
            scopes.append(("memories", scope_status(
                self.memories, True, bool(result.search_log), len(result.memories),
                omitted=facts.get("memories_total", 0) - len(result.memories),
            )))
        if self.working_memory_history_path:
            scopes.append(("working_memory_history", scope_status(
                facts["events"], facts["events_loaded"], bool(result.history_log), len(result.history_events),
                omitted=facts.get("working_memory_history_total", 0) - len(result.history_events),
            )))
        status = {"search": search, "scopes": scopes}
        attribute_list = result.attribute_list
        if self.memory_state_path and attribute_list and attribute_list["total"]:
            status["attribute_list"] = {
                "result": "provided" if attribute_list["provided"] else "not_provided",
                "count": attribute_list["total"],
            }
        if self.memory_state_path and searched and long_term:
            status["attributes"] = [f"{subject}.{key}" for subject, key in facts.get("identified", [])]
        return status

    def _expand_history(self, result, memory_events, attributes, eligible=None):
        """先頭から MAX_ATTRIBUTES 個の属性のうち、変更履歴のあるものを先頭から MAX_HISTORY_ATTRIBUTES 個選び、
        その変更履歴を結果へ入れて、選んだ属性のリストを返す。

        変更履歴は検索語によらず、その属性のイベント（番号付きの key のものも含む）を古い順に集め、
        MAX_ATTRIBUTE_HISTORY_EVENTS を超える分は古いほうを省く。全件を渡したかどうかと、
        変更履歴があるのに渡さなかった属性の数を memory_history_scope に残し、回答AIへ伝える。
        eligible を渡すと、その属性だけを対象にする（ほかの属性は、渡さなかった属性としても数えない）。
        """
        scope = {"attributes": [], "omitted_attributes": 0}
        chosen = []
        for index, attribute in enumerate(attributes):
            if eligible is not None and attribute not in eligible:
                continue
            shown, total = attribute_history(memory_events, attribute, MAX_ATTRIBUTE_HISTORY_EVENTS)
            if not total:
                continue
            if index >= MAX_ATTRIBUTES or len(chosen) >= MAX_HISTORY_ATTRIBUTES:
                scope["omitted_attributes"] += 1
                continue
            chosen.append(attribute)
            subject, key = attribute
            scope["attributes"].append({"subject": subject, "key": key, "shown": len(shown), "total": total})
            result.memory_history_events.extend(self._with_confirmation_texts(shown))
        if scope["attributes"] or scope["omitted_attributes"]:
            result.memory_history_scope = scope
        return chosen

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
        memory_history_scope=None,
        retrieval_status=None,
    ):
        """ユーザー発話を履歴に追加し、LLMへ渡して応答を返す。

        渡すのは作業記憶（常に全項目）、retrieve の結果（memories / history_events /
        memory_state / memory_history_events / memory_pending / memory_history_scope / status）、現在の会話だけ。
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
                memory_history_scope,
                retrieval_status,
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

    def _store_problem(self):
        """保存されている現在値が、このセッションの持っている現在値と同じかを確かめる。同じなら None、違えば理由。

        抽出AIへ渡す key の一覧が「保存されている属性のすべて」だと言えるのは、同じだと確かめられたときだけ。
        """
        try:
            stored = load_memory_state(self.memory_state_path)
        except StructuredMemoryError:
            return "保存されている長期状態を読み込めませんでした"
        if stored != self.memory_state:
            return "保存されている長期状態が、このセッションの外で変更されています"
        return None

    def update_memory_state(self, user_text, retrieval=None):
        """ユーザー発話と既存の値の関係をLLMに判断させ、検証して反映し、(変更のリスト, 警告メッセージ) を返す。

        判断の入力は現在値とユーザー発話だけで、assistantの発言は渡さない。
        LLMは関係（replace / coexist / pending / no_change）を判断するだけで、
        実行してよいかの確認と状態の書き換えはここで行う。確認できない置き換えは保留にする。
        変更は、現在値の変更（operation が add / replace）と、新しく保存した保留（operation が pending）。
        現在値を保存してから変更履歴を追記し、保留は別のファイルへ保存する。
        失敗しても例外にはせず、警告として返す（会話本体を止めないため）。

        MVP15: LLMへ見せる現在値の範囲は extraction_view で決める（属性が少なければ従来どおり全量、
        多ければ key の一覧と一部の値）。retrieval（このターンの retrieve の結果）があれば、検索プランナーが選んだ属性と
        検索語に一致した属性の値を渡す。検証は、見せた範囲によらず現在値の全量に対して行う。
        LLMへ渡した key の一覧が保存されている属性のすべてだと確かめられないとき（属性が多すぎて一覧を渡さなかった、
        保存されている現在値を読めない・セッションの外で変わった）は、新しい key を自動では追加せず保留にする。
        保存されている現在値を読めない・セッションの外で変わったときは、置き換えも自動では行わず保留にする
        （このセッションの現在値で、保存されている内容を上書きしないため。/confirm と /revert もこの状態では実行しない）。
        """
        if not self.memory_state_path:
            return [], []
        if self.write_blocked:
            return [], [f"長期状態の書き込みを停止しています（{self.write_blocked}）。再起動してください"]
        view = extraction_view(
            self.memory_state,
            user_text,
            store_problem=self._store_problem(),
            selected=retrieval.selected_attributes if retrieval else (),
            queried=retrieval.query_attributes if retrieval else (),
            pending=self.unresolved_pending(),
        )
        self.last_extraction_view = view
        try:
            # mode が full なら view["items"] は現在値の全量で、LLMへの入力は従来と同じ
            raw = self.llm.extract_structured_memory(user_text, view["items"], view)
            candidates, recovered = parse_extraction(raw)
        except (LLMError, StructuredMemoryError) as e:
            return [], [f"長期状態を抽出できませんでした: {e}"]

        warnings = []
        if recovered:
            warnings.append("長期状態の抽出結果にJSON以外の前置きがあったため、末尾のJSONだけを解析しました")
        timestamp = timestamp_now()
        updated, results, rejected = apply_structured_candidates(
            self.memory_state,
            candidates,
            user_text,
            timestamp,
            new_key_hold=view["coverage_reason"] if view["key_coverage"] != COVERAGE_COMPLETE else None,
            write_hold=view["coverage_reason"] if view["key_coverage"] == COVERAGE_UNAVAILABLE else None,
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
