"""長期記憶の検索。検索語を考えるのはAIで、ここでは検索プランの検証と単純な文字列検索だけを行う。"""

import json
import unicodedata

from .memory import strip_code_fence

MAX_QUERIES = 3  # 1回の検索プランで使う検索語の上限
MAX_RESULTS = 5  # 通常回答用のLLMへ渡す記憶の上限
MAX_ROUNDS = 2  # 検索プラン作成の上限（0件のときの再検索は1回だけ）


class SearchPlanError(Exception):
    """検索プランの解析の失敗。メッセージはそのままユーザーに表示できる。"""


def normalize(text):
    """英字の大文字小文字と全角・半角の違いだけをそろえる。"""
    return unicodedata.normalize("NFKC", text).casefold()


def parse_search_plan(raw):
    """検索プランナーの出力を検索語のリストとして解析する。記憶が不要という判断なら空のリストを返す。"""
    try:
        plan = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as e:
        raise SearchPlanError(f"検索プランがJSONとして読めません ({e.msg})") from e
    if not isinstance(plan, dict):
        raise SearchPlanError("検索プランがJSONオブジェクトではありません")
    queries = plan.get("queries", [])
    if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
        raise SearchPlanError("検索プランの queries が文字列の配列ではありません")
    if plan.get("needs_memory") is False:
        return []

    cleaned = []
    seen = set()
    for query in queries:
        query = " ".join(query.split())
        if query and normalize(query) not in seen:
            seen.add(normalize(query))
            cleaned.append(query)
    return cleaned[:MAX_QUERIES]


def search_memories(memories, queries, limit=MAX_RESULTS):
    """検索語のどれかにヒットした記憶を、スコアの高い順に limit 件まで返す。

    検索語は空白区切りのキーワードで、キーワードがすべて text か evidence に含まれる記憶がヒットする。
    スコアはヒットした検索語のキーワード数の合計。同点は保存順。
    """
    keyword_sets = [normalize(query).split() for query in queries]
    scored = []
    for memory in memories:
        haystack = normalize(memory["text"] + "\n" + memory["evidence"])
        score = sum(
            len(keywords)
            for keywords in keyword_sets
            if keywords and all(keyword in haystack for keyword in keywords)
        )
        if score:
            scored.append((score, memory))
    # sortedは安定なので、同点は保存順のままになる
    scored.sort(key=lambda item: item[0], reverse=True)
    return [memory for _, memory in scored[:limit]]
