"""会話。現在のプロセス内の履歴はメモリ上に保持し、会話原文・長期記憶・作業記憶をファイルへ保存する。"""

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
from .search import MAX_ROUNDS, SearchPlanError, normalize, parse_search_plan, search_memories
from .working_memory import (
    WorkingMemoryError,
    append_working_memory_history,
    apply_candidates,
    empty_working_memory,
    load_working_memory,
    save_working_memory,
)
from .working_memory import parse_candidates as parse_working_memory_candidates


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
        working_memory_history_path へは作業記憶の変更履歴を追記するだけで、読み戻さない。
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

    def recall(self, user_text):
        """ユーザー発話に関係しそうな長期記憶を検索し、(ヒットした記憶, 検索ログ, 警告メッセージ) を返す。

        検索語はLLMに考えさせ、検索と回数の制御はここで行う。
        検索ログは検索を実行したラウンドごとの (検索語のリスト, ヒット件数)。
        失敗しても例外にはせず、警告として返す（会話本体を止めないため）。
        """
        log = []
        if not self.memories:
            return [], log, []
        tried = []
        for _ in range(MAX_ROUNDS):
            try:
                queries = parse_search_plan(self.llm.plan_search(user_text, tried))
            except (LLMError, SearchPlanError) as e:
                return [], log, [f"記憶を検索できませんでした: {e}"]
            used = {normalize(q) for q in tried}
            queries = [q for q in queries if normalize(q) not in used]
            if not queries:
                # 記憶は不要という判断か、使用済みの検索語しか出てこなかった
                break
            found = search_memories(self.memories, queries)
            log.append((queries, len(found)))
            if found:
                return found, log, []
            tried += queries
        return [], log, []

    def send(self, user_text, memories=()):
        """ユーザー発話を履歴に追加し、LLMへ渡して応答を返す。

        渡すのは作業記憶（常に全項目）、memories（recall の結果）、現在の会話だけ。
        """
        self.messages.append({"role": "user", "content": user_text})
        try:
            reply = self.llm.complete(self.messages, memories, self.working_memory)
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
