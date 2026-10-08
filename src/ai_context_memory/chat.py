"""会話。現在のプロセス内の履歴はメモリ上に保持し、会話原文・長期記憶・作業記憶をファイルへ保存する。"""

from typing import NamedTuple

from .history import HistoryError, append_history
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
    search_memories,
    search_working_memory_history,
)
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


class Retrieval(NamedTuple):
    """1ターン分の検索結果。検索ログは、検索を実行した回ごとの (検索語のリスト, ヒット件数)。"""

    memories: list  # ヒットした長期記憶
    search_log: list  # 長期記憶の検索ログ
    history_events: list  # ヒットした作業記憶の変更履歴（古い順）
    history_log: list  # 変更履歴の検索ログ。検索しなかったターンは空
    warnings: list


class ChatSession:
    def __init__(
        self,
        llm,
        history_path=None,
        memory_path=None,
        working_memory_path=None,
        working_memory_history_path=None,
    ):
        """history_path へは会話原文を追記するだけで、読み戻さない。

        memory_path を渡すと、保存済みの長期記憶を検索対象として読み込む。
        working_memory_path を渡すと、保存済みの作業記憶を読み込み、毎回LLMへ渡す。
        working_memory_history_path へは作業記憶の変更履歴を追記し、検索が必要なターンで読み込む。
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

    def retrieve(self, user_text):
        """ユーザー発話に答えるために必要な長期記憶と作業記憶の変更履歴を検索し、Retrieval を返す。

        どちらを検索するかと検索語はLLMに1回の検索プランで考えさせ、検索と回数の制御はここで行う。
        発話の意味はここでは解釈しない。
        長期記憶は0件なら1回だけ検索語を変えて再検索する。変更履歴の検索は1回だけ。
        失敗しても例外にはせず、警告として返す（会話本体を止めないため）。
        """
        result = Retrieval([], [], [], [], [])
        events = []
        if self.working_memory_history_path:
            try:
                events = load_working_memory_history(self.working_memory_history_path)
            except WorkingMemoryError as e:
                result.warnings.append(str(e))
        if not self.memories and not events:
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
            used = {normalize(q) for q in tried}
            queries = [q for q in plan.queries if normalize(q) not in used]
            if not queries or not self.memories:
                # 長期記憶は不要という判断か、使用済みの検索語しか出てこなかったか、検索する長期記憶が無い
                break
            found = search_memories(self.memories, queries)
            result.search_log.append((queries, len(found)))
            if found:
                result.memories.extend(found)
                break
            tried += queries
        return result

    def recall(self, user_text):
        """retrieve のうち長期記憶の分だけを、(ヒットした記憶, 検索ログ, 警告メッセージ) で返す。"""
        result = self.retrieve(user_text)
        return result.memories, result.search_log, result.warnings

    def send(self, user_text, memories=(), history_events=()):
        """ユーザー発話を履歴に追加し、LLMへ渡して応答を返す。

        渡すのは作業記憶（常に全項目）、history_events と memories（retrieve の結果）、現在の会話だけ。
        """
        self.messages.append({"role": "user", "content": user_text})
        try:
            reply = self.llm.complete(self.messages, memories, self.working_memory, history_events)
        except Exception:
            # 失敗した発話を残すとuserが連続するため、履歴を元に戻す
            self.messages.pop()
            raise
        self.messages.append({"role": "assistant", "content": reply})
        if self.history_path:
            try:
                append_history(self.history_path, self.messages[-2:])
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
