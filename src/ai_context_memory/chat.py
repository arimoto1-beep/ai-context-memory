"""1プロセス内の会話。履歴はメモリ上にのみ保持する。"""


class ChatSession:
    def __init__(self, llm):
        self.llm = llm
        self.messages = []

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
        return reply
