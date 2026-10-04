"""会話。履歴はメモリ上に保持し、history_path があればファイルにも保存する。"""

from .history import HistoryError, append_history, load_history


class ChatSession:
    def __init__(self, llm, history_path=None):
        """history_path を渡すと、保存済みの履歴を読み込んで続きから会話する。"""
        self.llm = llm
        self.history_path = history_path
        self.messages = load_history(history_path) if history_path else []

    def send(self, user_text):
        """ユーザー発話を履歴に追加し、履歴全体をLLMへ渡して応答を返す。"""
        self.messages.append({"role": "user", "content": user_text})
        try:
            reply = self.llm.complete(self.messages)
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
