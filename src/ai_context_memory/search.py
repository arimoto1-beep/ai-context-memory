"""記憶の検索。検索語を考えるのはAIで、ここでは検索プランの検証と単純な文字列検索だけを行う。

検索対象は、長期記憶（従来の記憶と、構造化した現在値）、長期記憶の変更履歴、作業記憶の変更履歴。
どれを検索するかもAIが検索プランで決める。
"""

import json
import unicodedata
from dataclasses import dataclass, field

from .memory import strip_code_fence

MAX_QUERIES = 3  # 1回の検索プランで使う検索語の上限（検索対象ごと）
MAX_RESULTS = 5  # 通常回答用のLLMへ渡す記憶の上限（従来の記憶・構造化した現在値のそれぞれ）
MAX_HISTORY_RESULTS = 10  # 通常回答用のLLMへ渡す変更履歴のイベントの上限（履歴ごと）
MAX_ROUNDS = 2  # 検索プラン作成の上限（0件のときの再検索は1回だけ）

# 作業記憶の変更履歴のイベントのうち、検索対象にする項目
HISTORY_SEARCH_KEYS = ("operation", "field", "text", "target", "evidence")
# 構造化した長期記憶の現在値と、その変更履歴のイベントのうち、検索対象にする項目
STATE_SEARCH_KEYS = ("subject", "key", "value", "evidence")
MEMORY_HISTORY_SEARCH_KEYS = ("operation", "subject", "key", "value", "old_value", "evidence")


class SearchPlanError(Exception):
    """検索プランの解析の失敗。メッセージはそのままユーザーに表示できる。"""


@dataclass(frozen=True)
class SearchPlan:
    """検索プランナーの判断。検索語が空のリストなら、その検索対象は不要という判断。"""

    queries: list  # 長期記憶（従来の記憶と構造化した現在値）の検索語
    history_queries: list  # 作業記憶の変更履歴の検索語
    memory_history_queries: list = field(default_factory=list)  # 長期記憶の変更履歴の検索語


def normalize(text):
    """英字の大文字小文字と全角・半角の違いだけをそろえる。"""
    return unicodedata.normalize("NFKC", text).casefold()


def _clean_queries(plan, queries_key, needed):
    queries = plan.get(queries_key, [])
    if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
        raise SearchPlanError(f"検索プランの {queries_key} が文字列の配列ではありません")
    if not needed:
        return []

    cleaned = []
    seen = set()
    for query in queries:
        query = " ".join(query.split())
        if query and normalize(query) not in seen:
            seen.add(normalize(query))
            cleaned.append(query)
    return cleaned[:MAX_QUERIES]


def parse_plan(raw):
    """検索プランナーの出力を、長期記憶と2つの変更履歴それぞれの検索語として解析する。

    変更履歴のキーが無い出力（MVP4・MVP7 形式）は、その変更履歴は不要という判断として扱う。
    """
    try:
        plan = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as e:
        raise SearchPlanError(f"検索プランがJSONとして読めません ({e.msg})") from e
    if not isinstance(plan, dict):
        raise SearchPlanError("検索プランがJSONオブジェクトではありません")
    return SearchPlan(
        # needs_memory は省略されていても検索する（MVP4 と同じ）
        _clean_queries(plan, "queries", plan.get("needs_memory") is not False),
        # 変更履歴は、必要という明示的な判断があるときだけ検索する
        _clean_queries(plan, "history_queries", plan.get("needs_working_memory_history") is True),
        _clean_queries(
            plan, "memory_history_queries", plan.get("needs_long_term_memory_history") is True
        ),
    )


def parse_search_plan(raw):
    """検索プランナーの出力を長期記憶の検索語のリストとして解析する。記憶が不要という判断なら空のリストを返す。"""
    return parse_plan(raw).queries


def _score(haystack, keyword_sets):
    """ヒットした検索語のキーワード数の合計。検索語は、キーワードがすべて含まれるときにヒットする。"""
    return sum(
        len(keywords)
        for keywords in keyword_sets
        if keywords and all(keyword in haystack for keyword in keywords)
    )


def _scored(records, queries, keys):
    """ヒットした記録を (スコア, records 内の位置) のリストで返す。keys は検索対象にする項目。"""
    keyword_sets = [normalize(query).split() for query in queries]
    scored = []
    for index, record in enumerate(records):
        haystack = normalize("\n".join(str(record[key]) for key in keys if record.get(key)))
        score = _score(haystack, keyword_sets)
        if score:
            scored.append((score, index))
    return scored


def _search_by_score(records, queries, keys, limit):
    scored = _scored(records, queries, keys)
    # sortedは安定なので、同点は保存順のままになる
    scored.sort(key=lambda item: item[0], reverse=True)
    return [records[index] for _, index in scored[:limit]]


def _search_events(events, queries, keys, limit):
    scored = _scored(events, queries, keys)
    scored.sort(reverse=True)
    # 回答用のLLMが経緯を追いやすいよう、選んだあとで時系列へ戻す
    return [events[index] for index in sorted(index for _, index in scored[:limit])]


def search_memories(memories, queries, limit=MAX_RESULTS):
    """検索語のどれかにヒットした記憶を、スコアの高い順に limit 件まで返す。

    検索語は空白区切りのキーワードで、キーワードがすべて text か evidence に含まれる記憶がヒットする。
    スコアはヒットした検索語のキーワード数の合計。同点は保存順。
    """
    return _search_by_score(memories, queries, ("text", "evidence"), limit)


def search_memory_state(items, queries, limit=MAX_RESULTS):
    """検索語のどれかにヒットした構造化した現在値を、スコアの高い順に limit 件まで返す。

    ヒットの判定とスコアは search_memories と同じで、検索対象は subject / key / value / evidence。
    """
    return _search_by_score(items, queries, STATE_SEARCH_KEYS, limit)


def search_working_memory_history(events, queries, limit=MAX_HISTORY_RESULTS):
    """検索語のどれかにヒットした変更履歴のイベントを limit 件まで、古い順（events の順）で返す。

    ヒットの判定とスコアは search_memories と同じで、
    検索対象は operation / field / text / target / evidence。timestamp は検索しない。
    limit を超える場合はスコアの高い順、同点なら新しいイベントを優先して残す。
    """
    return _search_events(events, queries, HISTORY_SEARCH_KEYS, limit)


def search_memory_history(events, queries, limit=MAX_HISTORY_RESULTS):
    """検索語のどれかにヒットした長期記憶の変更履歴のイベントを limit 件まで、古い順（events の順）で返す。

    選び方と並びは search_working_memory_history と同じで、
    検索対象は operation / subject / key / value / old_value / evidence。timestamp は検索しない。
    """
    return _search_events(events, queries, MEMORY_HISTORY_SEARCH_KEYS, limit)
