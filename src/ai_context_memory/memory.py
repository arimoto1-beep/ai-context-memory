"""長期記憶。会話原文とは別のJSONLファイルに、1行1記憶で保存する。

意味判断（何を記憶するか）はAIに任せ、ここではAIが返した候補の検証と保存だけを行う。
"""

import json

# MVP3ではユーザー発言だけを記憶の元にする
ORIGIN_USER = "user"


class MemoryStoreError(Exception):
    """記憶の抽出結果の解析・記憶ファイルの読み書きの失敗。メッセージはそのままユーザーに表示できる。"""


def load_memories(path):
    """保存済みの記憶を読み込む。ファイルが無い・空の場合は空のリストを返す。"""
    try:
        # utf-8-sigはエディタで保存し直したときに付くBOMを取り除くため
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        raise MemoryStoreError(f"記憶ファイルを読み込めませんでした: {path} ({e})") from e

    memories = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            memory = json.loads(line)
        except json.JSONDecodeError as e:
            raise MemoryStoreError(
                f"記憶ファイルの {number} 行目がJSONとして読めません: {path} ({e.msg})"
            ) from e
        if (
            not isinstance(memory, dict)
            or not isinstance(memory.get("text"), str)
            or memory.get("origin") != ORIGIN_USER
            or not isinstance(memory.get("evidence"), str)
        ):
            raise MemoryStoreError(
                f"記憶ファイルの {number} 行目の形式が不正です: {path} "
                '（{"text": "...", "origin": "user", "evidence": "..."} が必要です）'
            )
        memories.append(
            {"text": memory["text"], "origin": memory["origin"], "evidence": memory["evidence"]}
        )
    return memories


def append_memories(path, memories):
    """記憶を記憶ファイルの末尾へ追記する。"""
    text = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in memories)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise MemoryStoreError(f"記憶を保存できませんでした: {path} ({e})") from e


def parse_candidates(raw):
    """抽出AIの出力を記憶候補のリストとして解析する。JSON配列でなければ失敗とする。"""
    text = raw.strip()
    # JSONだけを返すよう指示していても、コードブロックで囲んで返すことがある
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        candidates = json.loads(text)
    except json.JSONDecodeError as e:
        raise MemoryStoreError(f"抽出結果がJSONとして読めません ({e.msg})") from e
    if not isinstance(candidates, list):
        raise MemoryStoreError("抽出結果がJSON配列ではありません")
    return candidates


def rejection_reason(candidate, user_text):
    """候補を保存してよければ None、保存できなければその理由を返す。"""
    if not isinstance(candidate, dict):
        return "形式が不正です"
    text = candidate.get("text")
    evidence = candidate.get("evidence")
    if not isinstance(text, str) or not text.strip():
        return "text がありません"
    if not isinstance(evidence, str) or not evidence.strip():
        return "evidence がありません"
    # AIが根拠を作り出していないことを、元の発言との照合で確かめる
    if evidence not in user_text:
        return f"evidence が元の発言に含まれていません: {evidence}"
    return None
