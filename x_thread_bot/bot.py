"""
X スレッド投稿bot（自アカウント専用）
作成日: 20261121

仕組み:
  1. Claude でテーマから「フック投稿 + 続きの自リプ(2〜4本)」を生成
  2. 1本目を投稿 → 続きを自分の投稿へのリプライとして連結（スレッド化）
  3. 結果を posts_log.jsonl に記録（後でインプ/返信クリック率を分析する用）

やらないこと（X自動化ルール違反・凍結リスク回避のため意図的に未実装）:
  - 他人の投稿へのリプライ / 引用 / メンション
  - 無関係なトレンドハッシュタグの付与
  - 複数アカウント運用、いいね・RT・フォローの自動化

使い方:
  pip install anthropic tweepy pydantic
  export ANTHROPIC_API_KEY=...
  export X_API_KEY=... X_API_SECRET=... X_ACCESS_TOKEN=... X_ACCESS_SECRET=...
  python bot.py              # ドライラン（生成のみ・投稿しない）
  python bot.py --post       # 実投稿
"""

import argparse
import json
import os
import random
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

MODEL = "claude-opus-5-5"
LOG_PATH = Path(__file__).parent / "posts_log.jsonl"
MAX_WEIGHT = 280          # X の文字数上限（全角=2, 半角=1 換算）
REPLY_INTERVAL_SEC = 8    # 連投判定を避けるためのリプ間隔

# 自分が語れる領域に絞る（嘘をつかずに書けるテーマだけ入れる）
THEMES = [
    "ポイント施策で売上が上がらない会社の共通点",
    "CRMのメール開封率が高いのに売上につながらない理由",
    "ゲーミフィケーションが3ヶ月で飽きられる構造",
    "KPIを増やすほど現場が動かなくなる理由",
    "ROAS試算で9割の人が見落とす1つの数字",
    "SQLが書けるマーケターと書けないマーケターの差が出る瞬間",
]

# フックの型（どれか1つをランダム指定して単調化を防ぐ）
HOOK_PATTERNS = [
    "常識の否定：『◯◯は逆効果です。』で始め、理由は1本目で言わない",
    "数字の違和感：具体的だが意外な比率を提示し、解釈は続きに回す",
    "失敗の告白：自分がやらかした事実→結末は続きで明かす",
    "リスト予告：『◯つあります。3つ目が一番やばい』型で、1本目は1つ目だけ",
    "問いかけ：読者が即答できそうで実は外す問いを置き、答えは続きで",
]

SYSTEM_PROMPT = """あなたはX（旧Twitter）のスレッド投稿を書くコピーライターです。
目的: 1本目で興味を引き、読者がスレッドの続きを開きたくなる構成にする。

厳守ルール:
- 1本目は『結論を言い切らない』。ただし釣りっぽい煽り語（衝撃・閲覧注意・知らないと損）は禁止。
- 1本目の最後は自然に続きを促す（例:『理由はこの下に』『3つ目が本題↓』）。毎回同じ言い回しにしない。
- 続きの投稿では1本目で張った謎を必ず回収する。回収しない釣りは禁止。
- 事実として断定できない統計・ニュース・実在人物/企業名は書かない。数字を使う場合は『体感』『自社事例では』など根拠の種類を明示する。
- ハッシュタグ・メンション・URLは使わない。
- 各投稿は全角140字以内。改行を使って読みやすく。
- 最後の投稿は具体的な一手（明日からできること）で締める。"""


class Thread(BaseModel):
    hook: str = Field(description="1本目。続きを読みたくなるフック")
    replies: list[str] = Field(description="続きの自リプ。2〜4本", min_length=2, max_length=4)


def x_weight(text: str) -> int:
    """X の文字数カウント近似（全角・CJK=2, それ以外=1）"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F", "A") else 1 for c in text)


def generate_thread(client: anthropic.Anthropic, theme: str, pattern: str) -> Thread:
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"テーマ: {theme}\nフックの型: {pattern}\nスレッドを1本作ってください。",
        }],
        output_format=Thread,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("生成が拒否されました（テーマを見直してください）")
    return resp.parsed_output


def validate(thread: Thread) -> list[str]:
    errors = []
    for i, t in enumerate([thread.hook, *thread.replies]):
        w = x_weight(t)
        if w > MAX_WEIGHT:
            errors.append(f"投稿{i + 1}が上限超過（{w}/{MAX_WEIGHT}）")
        if "#" in t or "@" in t or "http" in t:
            errors.append(f"投稿{i + 1}にハッシュタグ/メンション/URLが含まれる")
    return errors


def post_thread(thread: Thread) -> list[str]:
    import tweepy

    x = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )
    ids = []
    first = x.create_tweet(text=thread.hook)
    ids.append(first.data["id"])
    parent = ids[0]
    for reply in thread.replies:
        time.sleep(REPLY_INTERVAL_SEC)
        r = x.create_tweet(text=reply, in_reply_to_tweet_id=parent)
        parent = r.data["id"]
        ids.append(parent)
    return ids


def log(record: dict) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="実際に投稿する（未指定はドライラン）")
    ap.add_argument("--theme", help="テーマを直接指定")
    ap.add_argument("--retries", type=int, default=3)
    args = ap.parse_args()

    client = anthropic.Anthropic()
    theme = args.theme or random.choice(THEMES)
    pattern = random.choice(HOOK_PATTERNS)

    thread, errors = None, []
    for _ in range(args.retries):
        thread = generate_thread(client, theme, pattern)
        errors = validate(thread)
        if not errors:
            break
    if errors:
        print("検証NG:", errors, file=sys.stderr)
        return 1

    print(f"[テーマ] {theme}\n[型] {pattern}\n")
    for i, t in enumerate([thread.hook, *thread.replies], 1):
        print(f"--- {i} ({x_weight(t)}/{MAX_WEIGHT}) ---\n{t}\n")

    ids = post_thread(thread) if args.post else []
    log({
        "ts": datetime.now().strftime("%Y%m%d%H%M%S"),
        "theme": theme,
        "pattern": pattern,
        "posted": args.post,
        "tweet_ids": ids,
        "hook": thread.hook,
        "replies": thread.replies,
    })
    print("投稿完了:" if args.post else "ドライラン完了（--post で投稿）", ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
