"""MVP14: 属性一覧による属性の選択、検索の状態（[retrieval status]）、R4（変更履歴の自動取得）。

データは MVP13 のテストと同じく実際の処理（発言の抽出 → add・保留、/confirm、/revert）で作るか、保存形式で直接書く。
検索プラン（AIの判断）だけを差し替えて、検索プランナーと回答AIへ渡る入力を確かめる。
"""

import json

import pytest

from ai_context_memory import chat as chat_module
from ai_context_memory.chat import ChatSession
from ai_context_memory.llm import (
    SEARCH_PLAN_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    format_attribute_list,
    format_retrieval_status,
    format_search_request,
)
from ai_context_memory.search import (
    MAX_ATTRIBUTE_HISTORY_EVENTS,
    MAX_ATTRIBUTES,
    MAX_CATALOG_ATTRIBUTES,
    MAX_RESULTS,
    parse_plan,
)
from ai_context_memory.memory import append_memories
from ai_context_memory.structured_memory import (
    ATTRIBUTE_AMBIGUOUS,
    ATTRIBUTE_NOT_IN_LIST,
    attribute_catalog,
    load_memory_state,
    resolve_attribute,
    save_memory_pending,
    save_memory_state,
)
from test_memory_confirmation import KAWASAKI, NAGOYA_TEXT, FakeLLM, make_paths, pending_item, run_cli
from test_memory_history_scope import (  # noqa: F401  （clock と paths は pytest のフィクスチャ）
    EMPLOYER_TEXT,
    HOME,
    block,
    clock,
    confirm_coexist,
    confirm_replace,
    history_entries,
    paths,
    pending_ids,
    revert,
    state_lines,
)

EMPLOYER = {**KAWASAKI, "key": "employer", "value": "A社", "evidence": EMPLOYER_TEXT}
HOMETOWN = {**KAWASAKI, "key": "hometown", "value": "札幌", "evidence": "出身は札幌です。"}


def attr_plan(queries=(), attributes=(), history=(), needs_memory=None, needs_history=None):
    """attributes を持つ検索プラン（MVP14 形式）。"""
    return json.dumps(
        {
            "needs_memory": bool(queries or attributes) if needs_memory is None else needs_memory,
            "queries": list(queries),
            "attributes": list(attributes),
            "needs_long_term_memory_history": bool(history) if needs_history is None else needs_history,
            "memory_history_queries": list(history),
            "needs_working_memory_history": False,
            "history_queries": [],
        },
        ensure_ascii=False,
    )


class PlannerLLM(FakeLLM):
    """検索プランナーへの入力も記録する。"""

    def __init__(self, plans=(), **kwargs):
        super().__init__(plans=plans, **kwargs)
        self.plan_inputs = []

    def plan_search(self, user_text, previous_queries=(), working_memory=None, attribute_list=None):
        self.plan_inputs.append(format_search_request(user_text, previous_queries, working_memory, attribute_list))
        return super().plan_search(user_text, previous_queries, working_memory, attribute_list)


def turn(files, *plans, question="住まいについて教えて"):
    """1ターン実行し、(LLM, CLIの出力) を返す。回答AIへの入力は llm.prompts[-1]。"""
    llm = PlannerLLM(plans=plans)
    outputs = run_cli(ChatSession(llm, *files), [question])
    return llm, outputs


def retrieve(files, *plans, question="住まいについて教えて"):
    llm = PlannerLLM(plans=plans)
    return ChatSession(llm, *files).retrieve(question), llm


def status_lines(prompt):
    return block(prompt, "retrieval status").splitlines()


def write_events(files, events):
    files[5].write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")


def event(minute, operation, key, value=None, old_value=None, evidence=None, subject="user"):
    """日時が1つの時計で進む変更履歴のイベント（ISO 8601）。"""
    item = {"timestamp": f"2026-10-17T11:{minute:02d}:00+09:00", "operation": operation, "subject": subject, "key": key}
    item.update({k: v for k, v in (("old_value", old_value), ("value", value), ("evidence", evidence)) if v})
    return item


# --- 属性一覧 ---


def test_catalog_has_only_identifiers_of_current_and_unresolved_pending(paths):
    """一覧は現在値と未解決の保留の属性だけで、値・発言を含まない。並びは決定的（subject・key の順）。"""
    llm, _ = turn(paths, attr_plan(["住まい"]))

    assert llm.plan_inputs[0] == (
        "[stored attributes]\n- user.employer\n- user.home_location\n[/stored attributes]\n\n[question]\n住まいについて教えて"
    )
    for word in ("川崎", "A社", "名古屋", "住んで", "働いて"):
        assert word not in llm.plan_inputs[0]


def test_catalog_merges_numbered_keys_and_separates_subjects():
    items = [
        {"subject": "user", "key": "hobby"},
        {"subject": "user", "key": "hobby_2"},
        {"subject": "partner", "key": "hobby"},
        {"subject": "user", "key": "home_location"},
        {"subject": "user", "key": "hobby_1"},  # _1 は番号付きの key ではない（既存の規則）
    ]
    pending = [pending_item(key="employer", proposed_key="side_job_3")]

    catalog = attribute_catalog(items, pending)

    assert catalog == [
        ("partner", "hobby"),
        ("user", "employer"),
        ("user", "hobby"),
        ("user", "hobby_1"),
        ("user", "home_location"),
        ("user", "side_job"),
    ]
    assert attribute_catalog(list(reversed(items)), pending) == catalog


def test_resolved_and_reverted_pending_are_not_in_the_catalog(paths):
    confirm_replace(paths)
    revert(paths)  # p001 は reverted、p002 が未解決として戻る

    llm, _ = turn(paths, attr_plan(["住まい"]))

    assert block(llm.plan_inputs[0], "stored attributes") == "- user.employer\n- user.home_location"


# --- 完全一致の検証 ---


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("user.home_location", (("user", "home_location"), None)),
        ("home_location", (("user", "home_location"), None)),
        (" user.home_location ", (("user", "home_location"), None)),
        ("user.home", (None, ATTRIBUTE_NOT_IN_LIST)),  # 部分一致しない
        ("home", (None, ATTRIBUTE_NOT_IN_LIST)),
        ("user.hometown", (("user", "hometown"), None)),
        ("user.home_location_2", (None, ATTRIBUTE_NOT_IN_LIST)),  # 番号付きの key を読み替えない
        ("User.Home_Location", (None, ATTRIBUTE_NOT_IN_LIST)),
        ("user.address", (None, ATTRIBUTE_NOT_IN_LIST)),
        ("partner.hobby", (("partner", "hobby"), None)),
        ("hobby", (None, ATTRIBUTE_AMBIGUOUS)),  # subject の無い key が2つの subject に当てはまる
        ("user.hobby", (("user", "hobby"), None)),
    ],
)
def test_resolve_attribute_accepts_exact_identifiers_only(identifier, expected):
    catalog = [("partner", "hobby"), ("user", "hobby"), ("user", "home_location"), ("user", "hometown")]

    assert resolve_attribute(identifier, catalog) == expected


def test_selected_attribute_is_found_without_a_matching_query(paths):
    """「住まい」は値にも発言にも key にも無いが、一覧から選んだ home_location の現在値と保留が渡る。"""
    llm, outputs = turn(paths, attr_plan(["住まい"], ["user.home_location"]))

    prompt = llm.prompts[-1]
    assert state_lines(prompt) == ["- user.home_location: 川崎"]
    assert pending_ids(prompt) == ["p001"]
    assert "A社" not in prompt
    # 属性を選べたので、検索語が0件でも再検索しない
    assert len(llm.plan_inputs) == 1
    assert "[属性選択] user.home_location" in outputs
    assert "attributes identified: user.home_location" in status_lines(prompt)


def test_actual_stored_key_is_used_even_if_it_is_named_residence(tmp_path, clock):
    """実際の抽出AIは住まいの key を residence と付けた（調査）。一覧にある実際の key だけを使う。"""
    residence = {**KAWASAKI, "key": "residence"}
    files = make_paths(tmp_path, state=(residence, EMPLOYER), pending=())

    llm, _ = turn(files, attr_plan(["住まい"], ["residence"]))
    assert state_lines(llm.prompts[-1]) == ["- user.residence: 川崎"]

    # 一覧に無い home_location を選んでも、residence へ読み替えない
    llm, outputs = turn(files, attr_plan(["住まい"], ["user.home_location"]), attr_plan(["住所"]))
    assert "[long-term memory state]" not in llm.prompts[-1]
    assert "[属性選択] user.home_location は使いません（一覧にありません）" in outputs
    assert load_memory_state(files[4]) == [residence, EMPLOYER]


@pytest.mark.parametrize("question", ["勤務先は？", "職場はどこ？", "どこで働いている？"])
def test_paraphrased_questions_select_the_same_attribute(tmp_path, clock, question):
    """言い換えから属性を選ぶのはプランナー。Python は選ばれた属性を、言い換えに関係なく同じように集める。"""
    files = make_paths(tmp_path, state=(KAWASAKI, EMPLOYER), pending=())

    llm, _ = turn(files, attr_plan(["勤務先", "職場"], ["user.employer"]), question=question)

    assert state_lines(llm.prompts[-1]) == ["- user.employer: A社"]


def test_numbered_key_is_collected_through_its_base_attribute(paths):
    confirm_coexist(paths)  # home_location_2 = 名古屋

    llm, outputs = turn(paths, attr_plan([], ["user.home_location"]))
    assert state_lines(llm.prompts[-1]) == ["- user.home_location: 川崎", "- user.home_location_2: 名古屋"]
    assert block(llm.plan_inputs[0], "stored attributes") == "- user.employer\n- user.home_location"

    llm, outputs = turn(paths, attr_plan(["住まい"], ["user.home_location_2"]), attr_plan(["住居"]))
    assert "[long-term memory state]" not in llm.prompts[-1]
    assert "[属性選択] user.home_location_2 は使いません（一覧にありません）" in outputs


def test_same_key_of_another_subject_is_not_mixed(tmp_path, clock):
    partner = {**KAWASAKI, "subject": "partner", "value": "札幌", "evidence": "妻は札幌に住んでいます。"}
    files = make_paths(tmp_path, state=(KAWASAKI, partner), pending=())

    llm, _ = turn(files, attr_plan([], ["user.home_location"]))
    assert state_lines(llm.prompts[-1]) == ["- user.home_location: 川崎"]

    # subject の無い key は、どちらの subject か決まらないので使わない
    llm, outputs = turn(files, attr_plan(["住まい"], ["home_location"]), attr_plan(["住居"]))
    assert "[long-term memory state]" not in llm.prompts[-1]
    assert "[属性選択] home_location は使いません（subject が決まりません）" in outputs


def test_similar_but_different_keys_are_not_matched_partially(tmp_path, clock):
    files = make_paths(tmp_path, state=(KAWASAKI, HOMETOWN), pending=())

    llm, _ = turn(files, attr_plan([], ["user.hometown"]), question="出身は？")
    assert state_lines(llm.prompts[-1]) == ["- user.hometown: 札幌"]

    llm, outputs = turn(files, attr_plan(["住まい"], ["user.home"]), attr_plan(["住居"]))
    assert "[long-term memory state]" not in llm.prompts[-1]
    assert "[属性選択] user.home は使いません（一覧にありません）" in outputs


def test_attribute_not_in_the_list_is_ignored_and_never_created(paths):
    state_before = load_memory_state(paths[4])

    retrieval, _ = retrieve(paths, attr_plan(["住まい"], ["user.address", 3]), attr_plan(["住居"]))

    assert retrieval.selected_attributes == []
    assert retrieval.rejected_attributes == [("user.address", ATTRIBUTE_NOT_IN_LIST)]
    assert retrieval.ignored_attributes == ["3"]
    assert retrieval.memory_state == []
    assert retrieval.status["attributes"] == []
    assert load_memory_state(paths[4]) == state_before


def test_selection_with_needs_memory_false_is_not_used(paths):
    """長期記憶もその変更履歴も不要という判断と一緒に返された属性の選択は使わない（記憶を渡さないほうを選ぶ）。"""
    llm, outputs = turn(
        paths, attr_plan([], ["user.home_location"], needs_memory=False), question="1+1は？"
    )

    prompt = llm.prompts[-1]
    assert "[long-term memory state]" not in prompt
    assert status_lines(prompt)[0] == "search: not_needed"
    assert "[属性選択] ほかに 1 件の選択を使いませんでした" in outputs


def test_selection_with_history_needed_is_used_even_if_needs_memory_is_false(paths):
    confirm_replace(paths)

    llm, _ = turn(
        paths, attr_plan([], ["user.home_location"], history=["replace"], needs_memory=False),
        question="住まいの記憶はどう変わった？",
    )

    prompt = llm.prompts[-1]
    assert state_lines(prompt) == ["- user.home_location: 名古屋"]
    assert history_entries(prompt)[0] == f"{HOME} (events: 2 of 2, complete)"


def test_plan_without_attributes_key_is_the_mvp13_format():
    plan = parse_plan('{"needs_memory": true, "queries": ["名前"]}')

    assert plan.attributes == [] and plan.ignored_attributes == []
    assert parse_plan('{"needs_memory": true, "queries": [], "attributes": "user.name"}').ignored_attributes == [
        "'user.name'"
    ]
    assert parse_plan('{"needs_memory": true, "queries": [], "attributes": ["a", "a", " b "]}').attributes == ["a", "b"]


def test_selected_attributes_are_capped(tmp_path, clock):
    state = [{**KAWASAKI, "key": f"attr_{c}", "value": c, "evidence": c} for c in "abcdefg"]
    files = make_paths(tmp_path, state=state, pending=())

    retrieval, _ = retrieve(files, attr_plan([], [f"user.attr_{c}" for c in "abcdefg"]))

    assert len(retrieval.selected_attributes) == MAX_ATTRIBUTES
    assert len(retrieval.ignored_attributes) == 7 - MAX_ATTRIBUTES


def test_selected_attributes_are_ordered_by_search_evidence(tmp_path, clock):
    """一覧の順（アルファベット順）にすべて選ばれても、変更履歴の上限で、検索語に一致した属性の変更履歴を落とさない。

    実際の Claude の評価（修正前のプロンプト）で、「なぜ記憶が変わった？」に5属性すべてが選ばれ、
    一覧の順で先頭3つ（employer / favorite_color / hobby）の変更履歴だけが渡り、home_location が落ちた。
    """
    state = [
        {**KAWASAKI, "key": key, "value": value, "evidence": text, "updated_at": f"2026-10-17T11:{minute:02d}:00+09:00"}
        for key, value, text, minute in (
            ("hometown", "札幌", "出身は札幌です。", 1),
            ("employer", "A社", EMPLOYER_TEXT, 2),
            ("hobby", "登山", "趣味は登山です。", 3),
            ("favorite_color", "青", "好きな色は青です。", 4),
            ("home_location", "川崎", KAWASAKI["evidence"], 7),
        )
    ]
    files = make_paths(tmp_path, state=state, pending=())
    write_events(files, [
        event(1, "add", "hometown", "札幌", evidence="出身は札幌です。"),
        event(2, "add", "employer", "A社", evidence=EMPLOYER_TEXT),
        event(3, "add", "hobby", "登山", evidence="趣味は登山です。"),
        event(4, "add", "favorite_color", "青", evidence="好きな色は青です。"),
        event(5, "add", "home_location", "川崎", evidence=KAWASAKI["evidence"]),
        event(6, "replace", "home_location", "名古屋", "川崎", NAGOYA_TEXT),
        event(7, "revert", "home_location", "川崎", "名古屋"),
    ])
    everything = ["user.employer", "user.favorite_color", "user.hobby", "user.home_location", "user.hometown"]

    llm, _ = turn(files, attr_plan(["変わ"], everything, history=["replace", "revert"]), question="なぜ記憶が変わった？")

    headers = [e for e in history_entries(llm.prompts[-1]) if isinstance(e, str)]
    assert headers[0] == f"{HOME} (events: 3 of 3, complete)"
    assert headers[-1] == "other matching attributes not included: 2"


def test_over_selection_does_not_push_out_the_attribute_the_query_matched(tmp_path, clock):
    """プランナーが関係の薄い属性を上限いっぱい（5つ）選んでも、検索語が一致した属性を上限から落とさない。

    「名古屋」は home_location の保留にだけ一致する。選ばれた5属性を先に置くと home_location は6番目になり、
    現在値（川崎）も R4 の変更履歴も渡らなかった。選んだ属性そのもの（過剰な選択）は Python では防げない。
    """
    others = [
        {**KAWASAKI, "key": key, "value": value, "evidence": text}
        for key, value, text in (
            ("employer", "A社", EMPLOYER_TEXT),
            ("favorite_color", "青", "好きな色は青です。"),
            ("favorite_food", "寿司", "好きな食べ物は寿司です。"),
            ("hobby", "登山", "趣味は登山です。"),
            ("hometown", "札幌", "出身は札幌です。"),
        )
    ]
    files = make_paths(tmp_path, state=[*others, KAWASAKI], pending=(pending_item(),))
    write_events(files, [
        event(1, "add", "employer", "A社", evidence=EMPLOYER_TEXT),
        event(2, "add", "hobby", "登山", evidence="趣味は登山です。"),
        event(3, "add", "home_location", "川崎", evidence=KAWASAKI["evidence"]),
    ])
    five = ["user.employer", "user.favorite_color", "user.favorite_food", "user.hobby", "user.hometown"]

    llm, outputs = turn(files, attr_plan(["名古屋"], five), question="名古屋の件はどうなった？")

    prompt = llm.prompts[-1]
    assert "- user.home_location: 川崎" in state_lines(prompt)  # 現在値の行は保存の順
    assert len(state_lines(prompt)) == MAX_ATTRIBUTES
    assert "- user.hometown: 札幌" not in state_lines(prompt)  # 最後に選ばれた属性が上限で落ちる
    assert pending_ids(prompt) == ["p001"]
    # R4 は検索語が一致した属性だけ。選ばれた employer・hobby の変更履歴は渡さない
    assert [e for e in history_entries(prompt) if isinstance(e, str)] == [f"{HOME} (events: 1 of 1, complete)"]
    assert "long-term memory state: found 5 (1 more matching attributes omitted)" in status_lines(prompt)
    assert "[長期履歴] 保留・変更履歴だけに一致したため、変更履歴も渡します: user.home_location" in outputs


def test_history_limit_prefers_matched_attributes_over_selected_ones(tmp_path, clock):
    """変更履歴の上限（3属性）を超える数が選ばれ、変更履歴が求められたときも、検索語が一致した属性の変更履歴を先に渡す。"""
    keys = ("employer", "favorite_color", "hobby", "hometown", "home_location")
    state = [{**KAWASAKI, "key": key, "value": f"値{n}", "evidence": f"発言{n}"} for n, key in enumerate(keys)]
    state[-1] = KAWASAKI
    files = make_paths(tmp_path, state=state, pending=())
    write_events(files, [event(n, "add", key, f"値{n}", evidence=f"発言{n}") for n, key in enumerate(keys[:-1])] + [
        event(5, "add", "home_location", "名古屋", evidence=NAGOYA_TEXT),
        event(6, "replace", "home_location", "川崎", "名古屋", KAWASAKI["evidence"]),
    ])

    llm, _ = turn(
        files,
        attr_plan(["川崎"], [f"user.{key}" for key in keys], history=["home_location"]),
        question="住まいはどう変わった？",
    )

    headers = [e for e in history_entries(llm.prompts[-1]) if isinstance(e, str)]
    assert headers[0] == f"{HOME} (events: 2 of 2, complete)"
    assert len(headers) == 3 + 1  # 3属性と「渡さなかった属性」の行
    assert headers[-1] == "other matching attributes not included: 2"
    # 検索の状態も、MVP13 の変更履歴の見出しと同じ数を伝える
    assert "long-term memory history: found 3 attributes (2 more matching attributes not included)" in status_lines(
        llm.prompts[-1]
    )


# --- 一覧の上限 ---


def catalog_files(tmp_path, count):
    # key を attr_002 のような「_ と2以上の番号」で終わる形にすると、既存の規則で1つの属性 attr にまとまるので避ける
    state = [KAWASAKI] + [
        {"subject": "user", "key": f"topic{n:03d}", "value": f"値{n}", "evidence": f"発言{n}", "updated_at": KAWASAKI["updated_at"]}
        for n in range(count - 1)
    ]
    return make_paths(tmp_path, state=state, pending=())


def test_catalog_at_the_limit_is_provided(tmp_path, clock):
    files = catalog_files(tmp_path, MAX_CATALOG_ATTRIBUTES)

    retrieval, llm = retrieve(files, attr_plan([], ["user.home_location"]))

    assert retrieval.attribute_list["provided"] is True
    assert retrieval.attribute_list["chars"] == len(format_attribute_list(retrieval.attribute_list))
    assert llm.plan_inputs[0].count("\n- user.") == MAX_CATALOG_ATTRIBUTES
    assert retrieval.status["attribute_list"] == {"result": "provided", "count": MAX_CATALOG_ATTRIBUTES}


def test_catalog_over_the_limit_falls_back_to_search_terms(tmp_path, clock):
    """上限を超えたら一覧を渡さず、検索語だけで探す（MVP13 までの方式）。そのことを状態として伝える。"""
    files = catalog_files(tmp_path, MAX_CATALOG_ATTRIBUTES + 1)

    llm, outputs = turn(files, attr_plan(["川崎"]))

    plan_input = llm.plan_inputs[0]
    assert block(plan_input, "stored attributes") == (
        f"(not listed: {MAX_CATALOG_ATTRIBUTES + 1} attributes, more than the limit of {MAX_CATALOG_ATTRIBUTES})"
    )
    assert "topic000" not in plan_input and "home_location" not in plan_input
    # 検索語による検索はこれまでどおり動く
    assert state_lines(llm.prompts[-1]) == ["- user.home_location: 川崎"]
    assert f"attribute list: not_provided ({MAX_CATALOG_ATTRIBUTES + 1} attributes)" in status_lines(llm.prompts[-1])
    assert any(o.startswith(f"[属性一覧] {MAX_CATALOG_ATTRIBUTES + 1}件が上限") for o in outputs)
    assert any(o.startswith("[検索状態]") for o in outputs)


def test_attribute_list_size_is_measurable(tmp_path, clock):
    sizes = {}
    for count in (10, 50, 100):
        retrieval, _ = retrieve(catalog_files(tmp_path / str(count), count), attr_plan(["川崎"]))
        sizes[count] = retrieval.attribute_list["chars"]
    assert sizes[10] < sizes[50] < sizes[100]


# --- 変更履歴だけに残っている属性 ---


def history_only_files(tmp_path):
    """previous_employer は変更履歴にだけある（現在値にも保留にも無い）。"""
    files = make_paths(tmp_path, state=(KAWASAKI,), pending=())
    write_events(files, [
        event(1, "add", "home_location", "川崎", evidence=KAWASAKI["evidence"]),
        event(2, "add", "previous_employer", "C社", evidence="前はC社にいました。"),
    ])
    return files


def test_history_only_attribute_is_not_in_the_list_and_not_described_as_missing(tmp_path, clock):
    files = history_only_files(tmp_path)

    llm, outputs = turn(files, attr_plan(["C社"], ["user.previous_employer"]), question="C社のことは覚えてる？")

    assert "previous_employer" not in llm.plan_inputs[0]
    assert "[属性選択] user.previous_employer は使いません（一覧にありません）" in outputs
    # 検索語が変更履歴にだけ一致したので、R4 でその属性の変更履歴を渡す。存在しないとは扱わない
    prompt = llm.prompts[-1]
    assert history_entries(prompt) == [
        "attribute: user.previous_employer (events: 1 of 1, complete)",
        ("add", "previous_employer", "C社"),
    ]
    assert "long-term memory history: found 1 attributes" in status_lines(prompt)


# --- 検索の状態 ---


def test_status_found(paths):
    llm, _ = turn(paths, attr_plan(["名古屋"], ["user.home_location"]))

    assert status_lines(llm.prompts[-1]) == [
        "search: done",
        "attribute list: provided (2 attributes)",
        "attributes identified: user.home_location",
        "long-term memory state: found 1",
        "long-term memory pending: found 1",
        "long-term memory history: found 1 attributes",
        "retrieved memories: none_stored",
        "working memory history: none_stored",
    ]


def test_status_searched_but_no_match_is_not_none_stored(paths):
    llm, outputs = turn(paths, attr_plan(["横浜"]), attr_plan(["浜"]))

    lines = status_lines(llm.prompts[-1])
    assert "attributes identified: none" in lines
    assert "long-term memory state: no_match" in lines
    assert "long-term memory pending: no_match" in lines
    assert "long-term memory history: not_searched" in lines
    assert "[検索結果] 0件" in outputs


def test_status_attribute_not_identified_while_legacy_memories_are_found(paths):
    append_memories(paths[1], [{"text": "ユーザーは住まいを探している", "origin": "user", "evidence": "住まいを探しています"}])

    llm, _ = turn(paths, attr_plan(["住まい"]))

    lines = status_lines(llm.prompts[-1])
    assert "retrieved memories: found 1" in lines
    assert "attributes identified: none" in lines
    assert "long-term memory state: no_match" in lines


def test_status_not_searched(paths):
    llm, _ = turn(paths, attr_plan(needs_memory=False), question="1+1は？")

    assert status_lines(llm.prompts[-1]) == [
        "search: not_needed",
        "attribute list: provided (2 attributes)",
        "long-term memory state: not_searched",
        "long-term memory pending: not_searched",
        "long-term memory history: not_searched",
        "retrieved memories: none_stored",
        "working memory history: none_stored",
    ]


def test_status_history_unreadable_but_current_and_pending_are_found(paths):
    """Current と Pending は読めて、History だけ読めない。範囲ごとに分けて伝え、パスや例外は渡さない。"""
    paths[5].write_text('{"broken', encoding="utf-8")

    llm, outputs = turn(paths, attr_plan(["川崎"], history=["home_location"]), question="以前住んでいた場所は？")

    prompt = llm.prompts[-1]
    lines = status_lines(prompt)
    assert "long-term memory state: found 1" in lines
    assert "long-term memory pending: found 1" in lines
    assert "long-term memory history: unavailable" in lines
    assert "[long-term memory history]" not in prompt
    for word in ("jsonl", "JSON", "data", "Unterminated", "行目"):
        assert word not in block(prompt, "retrieval status")
    # 回答AIへの入力全体にも、実際のパス・ファイル名は入らない（読めなかったことは CLI の警告にだけ出す）
    assert paths[5].name not in prompt and str(paths[5].parent) not in prompt
    # CLI でも「0件」と表示しない
    assert "[長期履歴検索結果] 読み込めませんでした（0件ではありません）" in outputs
    assert "[長期履歴検索結果] 0件" not in outputs
    assert any(o.startswith("[検索状態]") and "長期履歴: unavailable" in o for o in outputs)


def test_status_working_memory_history_unreadable(paths):
    paths[3].write_text("壊れた行\n", encoding="utf-8")
    llm = PlannerLLM(plans=[json.dumps({
        "needs_memory": False, "queries": [], "needs_working_memory_history": True, "history_queries": ["scope"],
    })])

    outputs = run_cli(ChatSession(llm, *paths), ["対象範囲はどう変わった？"])

    assert "working memory history: unavailable" in status_lines(llm.prompts[-1])
    assert "[履歴検索結果] 読み込めませんでした（0件ではありません）" in outputs


def test_status_omitted_pending_and_memories(paths):
    pending = [pending_item(f"p{n:03d}", new=f"町{n}", evidence=f"今は町{n}に住んでいます。") for n in range(1, 8)]
    save_memory_pending(paths[6], pending)
    append_memories(paths[1], [
        {"text": f"ユーザーは川崎の話をした{n}", "origin": "user", "evidence": f"川崎{n}"} for n in range(MAX_RESULTS + 1)
    ])

    llm, outputs = turn(paths, attr_plan(["川崎"]))

    lines = status_lines(llm.prompts[-1])
    assert f"long-term memory pending: found {MAX_RESULTS} ({7 - MAX_RESULTS} omitted)" in lines
    assert f"retrieved memories: found {MAX_RESULTS} (1 omitted)" in lines
    assert any(o.startswith("[検索状態]") and "省略" in o for o in outputs)


def test_status_counts_direct_hits_beyond_the_attribute_cap_as_passed(tmp_path, clock):
    """検索語で直接ヒットした現在値は属性の上限を超えても渡る（MVP13）。渡したものは省略と数えない。"""
    state = [{**KAWASAKI, "key": f"attr_{c}", "value": f"値{c}", "evidence": f"発言{c}共通"} for c in "abcdef"]
    files = make_paths(tmp_path, state=state, pending=())

    llm, _ = turn(files, attr_plan(["共通"], ["user.attr_f"]))

    lines = status_lines(llm.prompts[-1])
    assert len(state_lines(llm.prompts[-1])) == 6
    assert "long-term memory state: found 6" in lines
    assert "attributes identified: user.attr_f, user.attr_a, user.attr_b, user.attr_c, user.attr_d, user.attr_e" in lines


def test_status_omitted_attributes_of_current_values(tmp_path, clock):
    """属性の上限で現在値を1つも渡せなかった属性を、省いた属性として数える。

    上限で落ちるのは、選ばれただけで検索語に一致しなかった属性（最後に選ばれた attr_e）。
    検索語が保留に一致した attr_f は、その現在値も渡す。
    """
    state = [{**KAWASAKI, "key": f"attr_{c}", "value": f"値{c}", "evidence": f"発言{c}"} for c in "abcdef"]
    pending = [pending_item(key="attr_f", old="値f", new="新しい値", evidence="attr_f は新しい値になりました。")]
    files = make_paths(tmp_path, state=state, pending=pending)

    llm, _ = turn(files, attr_plan(["新しい値"], [f"user.attr_{c}" for c in "abcde"]))

    prompt = llm.prompts[-1]
    assert "- user.attr_f: 値f" in state_lines(prompt)
    assert "- user.attr_e: 値e" not in state_lines(prompt)
    assert pending_ids(prompt) == ["p001"]
    assert "long-term memory state: found 5 (1 more matching attributes omitted)" in status_lines(prompt)


def test_status_none_stored_only_where_confirmed(paths):
    """現在値・保留は保存されていて、従来の記憶と作業記憶の変更履歴は空。空だと確かめた範囲だけ none_stored。"""
    llm, _ = turn(paths, attr_plan(["川崎"]))

    lines = status_lines(llm.prompts[-1])
    assert [line for line in lines if "none_stored" in line] == [
        "retrieved memories: none_stored",
        "working memory history: none_stored",
    ]


def test_status_retry_failed(paths):
    llm, outputs = turn(paths, attr_plan(["横浜"]), "検索します。")

    assert status_lines(llm.prompts[-1])[0] == "search: retry_failed"
    assert "long-term memory state: no_match" in status_lines(llm.prompts[-1])
    assert any(o.startswith("[検索状態]") for o in outputs)


def test_status_block_format_has_no_values():
    status = {
        "search": "done",
        "attribute_list": {"result": "provided", "count": 2},
        "attributes": ["user.home_location"],
        "scopes": [
            ("state", {"result": "found", "count": 2, "omitted_attributes": 1}),
            ("pending", {"result": "found", "count": 5, "omitted": 2}),
            ("history", {"result": "found", "count": 3, "omitted_attributes": 4}),
            ("memories", {"result": "no_match"}),
            ("working_memory_history", {"result": "unavailable"}),
        ],
    }

    assert format_retrieval_status(status) == (
        "[retrieval status]\n"
        "search: done\n"
        "attribute list: provided (2 attributes)\n"
        "attributes identified: user.home_location\n"
        "long-term memory state: found 2 (1 more matching attributes omitted)\n"
        "long-term memory pending: found 5 (2 omitted)\n"
        "long-term memory history: found 3 attributes (4 more matching attributes not included)\n"
        "retrieved memories: no_match\n"
        "working memory history: unavailable\n"
        "[/retrieval status]"
    )
    assert format_retrieval_status(None) == ""


# --- R4 ---


def test_r4_nagoya_brings_history_even_when_the_attribute_is_also_selected(paths):
    """一覧から home_location を選んで Current も取得されても、名古屋が保留にだけ一致したので変更履歴を渡す。"""
    confirm_replace(paths)
    revert(paths)

    llm, outputs = turn(paths, attr_plan(["名古屋"], ["user.home_location"]), question="名古屋の件はどうなった？")

    prompt = llm.prompts[-1]
    assert state_lines(prompt) == ["- user.home_location: 川崎"]
    assert pending_ids(prompt) == ["p002"]
    assert [e if isinstance(e, str) else e[0] for e in history_entries(prompt)] == [
        f"{HOME} (events: 3 of 3, complete)", "add", "replace", "revert"
    ]
    assert "[長期履歴] 保留・変更履歴だけに一致したため、変更履歴も渡します: user.home_location" in outputs


def test_r4_does_not_fire_when_the_query_matches_the_current_value(paths):
    confirm_replace(paths)
    revert(paths)

    llm, outputs = turn(paths, attr_plan(["川崎"]), question="今どこに住んでいる？")

    assert "[long-term memory history]" not in llm.prompts[-1]
    assert "long-term memory history: not_searched" in status_lines(llm.prompts[-1])
    assert not any("変更履歴も渡します" in o for o in outputs)


def test_r4_does_not_fire_when_the_same_words_are_in_current_and_pending(paths):
    """「住ん」は現在値（今は川崎に住んでいます。）にも保留（今は名古屋に住んでいます。）にも一致する。"""
    llm, _ = turn(paths, attr_plan(["住ん"]), question="今どこに住んでいる？")

    assert "[long-term memory history]" not in llm.prompts[-1]


def test_r4_fires_when_only_the_wording_of_current_and_pending_differs(tmp_path, clock):
    """誤発動の例。現在値が「在住」、保留が「住んでいます」だと、「住ん」は保留にだけ一致して R4 が働く。

    渡すのはその属性の変更履歴だけで、件数は MVP13 の上限の範囲にとどまる（大量には取得しない）。
    """
    current = {**KAWASAKI, "evidence": "川崎在住です。"}
    files = make_paths(tmp_path, state=(current, EMPLOYER), pending=(pending_item(),))
    write_events(files, [
        event(1, "add", "home_location", "川崎", evidence="川崎在住です。"),
        event(2, "add", "employer", "A社", evidence=EMPLOYER_TEXT),
    ])

    llm, _ = turn(files, attr_plan(["住ん"]), question="今どこに住んでいる？")

    assert [e for e in history_entries(llm.prompts[-1]) if isinstance(e, str)] == [
        f"{HOME} (events: 1 of 1, complete)"
    ]


def test_r4_history_does_not_mix_unrelated_attributes(paths):
    """名古屋で R4 が働いても、選ばれた別の属性（employer）の変更履歴はプランナーが求めない限り渡さない。"""
    llm, _ = turn(paths, attr_plan(["名古屋"], ["user.employer"]), question="名古屋の件と勤務先は？")

    prompt = llm.prompts[-1]
    assert [e for e in history_entries(prompt) if isinstance(e, str)] == [f"{HOME} (events: 1 of 1, complete)"]
    assert state_lines(prompt) == ["- user.home_location: 川崎", "- user.employer: A社"]


def test_r4_does_not_block_history_requested_by_the_planner(paths):
    confirm_replace(paths)

    llm, outputs = turn(
        paths, attr_plan(["A社"], ["user.home_location"], history=["home_location"]), question="以前の住まいと勤務先は？"
    )

    headers = [e for e in history_entries(llm.prompts[-1]) if isinstance(e, str)]
    assert headers == [f"{HOME} (events: 2 of 2, complete)", "attribute: user.employer (events: 1 of 1, complete)"]
    # プランナーが求めたので、R4 で足したとは表示しない
    assert not any("変更履歴も渡します" in o for o in outputs)


def test_r4_keeps_the_mvp13_history_limits(tmp_path, clock):
    total = MAX_ATTRIBUTE_HISTORY_EVENTS + 4
    files = make_paths(tmp_path, state=({**KAWASAKI, "value": f"町{total - 1}"},), pending=())
    write_events(files, [
        event(n, "add" if n == 0 else "replace", "home_location", f"町{n}", f"町{n - 1}" if n else None,
              f"今は町{n}に住んでいます。")
        for n in range(total)
    ])

    llm, _ = turn(files, attr_plan(["町0"]), question="町0の件はどうなった？")

    assert history_entries(llm.prompts[-1])[0] == (
        f"{HOME} (events: {MAX_ATTRIBUTE_HISTORY_EVENTS} of {total}, 4 older events omitted)"
    )


def test_r4_history_failure_is_stated(paths):
    paths[5].write_text("壊れた行\n", encoding="utf-8")

    llm, outputs = turn(paths, attr_plan(["名古屋"]), question="名古屋の件はどうなった？")

    prompt = llm.prompts[-1]
    assert state_lines(prompt) == ["- user.home_location: 川崎"]
    assert pending_ids(prompt) == ["p001"]
    assert "[long-term memory history]" not in prompt
    assert "long-term memory history: unavailable" in status_lines(prompt)


def test_r4_history_only_hit_avoids_a_second_planner_call(paths):
    """検索語が変更履歴にだけ一致したとき（置き換え前の A社）は、その属性を見つけたものとして再検索しない。"""
    run_cli(
        ChatSession(FakeLLM(structured=[json.dumps([{
            "decision": "replace", "subject": "user", "target_key": "employer", "proposed_key": None,
            "proposed_value": "B社", "evidence": "今はB社で働いています。", "old_value_status": "ended",
            "old_value_evidence": "A社を辞めて", "temporal_scope": "current", "reason": "r", "confirmation_question": "q",
        }], ensure_ascii=False)]), *paths),
        ["A社を辞めて、今はB社で働いています。"],
    )

    llm, _ = turn(paths, attr_plan(["A社"]), attr_plan(["会社"]), question="A社の件はどうなった？")

    assert len(llm.plan_inputs) == 1
    assert state_lines(llm.prompts[-1]) == ["- user.employer: B社"]
    assert history_entries(llm.prompts[-1])[0] == "attribute: user.employer (events: 2 of 2, complete)"


# --- 指示 ---


@pytest.mark.parametrize(
    "instruction",
    [
        "[stored attributes]",
        "attributes",
        "一字一句そのまま",
        "代用したりしないでください",
        "not listed",
        "7つ",
        # 実際の Claude の評価で、関係の薄い属性まで選ばれたため加えた指示（過剰な選択を Python では防げない）
        "念のために選んだり",
        "迷う属性は選ばないでください",
        "属性を推測して選ばず",
        "記憶全体の変化などを尋ねる発言では、attributes は空配列",
    ],
)
def test_search_plan_prompt_explains_the_attribute_list(instruction):
    assert instruction in SEARCH_PLAN_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "instruction",
    ["[retrieval status]", "none_stored", "unavailable", "no_match", "not_searched", "範囲ごとに独立", "not_provided"],
)
def test_system_prompt_explains_the_retrieval_status(instruction):
    assert instruction in SYSTEM_PROMPT


def test_new_instructions_do_not_name_evaluation_values():
    start = SEARCH_PLAN_SYSTEM_PROMPT.index("入力に [stored attributes]")
    end = SEARCH_PLAN_SYSTEM_PROMPT.index("snake_case も検索語に入れてください。")
    new_text = SEARCH_PLAN_SYSTEM_PROMPT[start:end]
    new_text += SYSTEM_PROMPT[SYSTEM_PROMPT.index("会話の前に [retrieval status]"):]
    for word in ("川崎", "名古屋", "A社", "B社", "home_location", "residence", "employer", "住まい"):
        assert word not in new_text


def test_mvp13_planner_without_attribute_list_is_unchanged():
    assert format_search_request("私の名前は？") == "[question]\n私の名前は？"
    assert format_attribute_list({"attributes": [], "total": 0, "provided": True, "limit": 100}) == ""
    assert chat_module.MAX_CATALOG_ATTRIBUTES == MAX_CATALOG_ATTRIBUTES == 100
