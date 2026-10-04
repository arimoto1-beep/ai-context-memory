"""会話。現在のプロセス内の履歴はメモリ上に保持し、会話原文と長期記憶をファイルへ保存する。"""

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


class ChatSession:
    def __init__(self, llm, history_path=None, memory_path=None):
        """history_path へは会話原文を追記するだけで、読み戻さない。

        memory_path を渡すと、保存済みの長期記憶を読み込んでLLMへ渡す。
        """
        self.llm = llm
        self.history_path = history_path
        self.memory_path = memory_path
        # 今回の起動からの会話だけ。再起動前の会話はここへ戻さない
        self.messages = []
        self.memories = load_memories(memory_path) if memory_path else []

    def send(self, user_text):
        """ユーザー発話を履歴に追加し、現在の会話と長期記憶をLLMへ渡して応答を返す。"""
        self.messages.append({"role": "user", "content": user_text})
        try:
            reply = self.llm.complete(self.messages, self.memories)
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
