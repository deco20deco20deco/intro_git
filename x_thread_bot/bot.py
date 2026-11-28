"""
X スレッド投稿bot（自アカウント専用）
作成日: 20261121

仕組み:
  1. Claude でテーマから「フック投稿 + 続きの自リプ(2〜4本)」を生成
  2. 1本目を投稿 → 続きを自分の投稿へのリプライとして連結（スレッド化）
  3. 結果を posts_log.jsonl に記録（後でインプ/返信クリック率を分析する用）

運用前提:
  - プロフィールに「創作」「自動投稿」を明記し、X設定で自動化ラベルを付ける
  - 設定 > プライバシー > 「センシティブな内容を含む可能性のあるものとして投稿をマーク」をON

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

# 下世話系テーマ（創作の修羅場・恋愛・不倫・夜の話）。実在人物は扱わない
THEMES = [
    "夫のスマホの通知で不倫に気づいた妻の、その後の静かな復讐",
    "結婚式のスピーチで新郎の浮気が全員にバレた話",
    "社内不倫が社内チャットの誤爆で全社にバレた瞬間",
    "義母が隠していた30年前の秘密",
    "マッチングアプリで会った相手が元カノの今の旦那だった",
    "W不倫の相手の配偶者から届いた一通のLINE",
    "親友の彼氏に口説かれ続けた結果、選んだ行動",
    "ラブホテルの駐車場で見覚えのある車を見つけた夜",
    "元カレの結婚式に呼ばれて行ったら新婦が知り合いだった",
    "夫婦のレス10年目、妻が出した結論",
    "隣の部屋から毎晩聞こえる声の正体",
    "托卵が発覚したきっかけは子どもの血液型ではなかった",
    "浮気相手の家に乗り込んだら、そこにいたのは別の女性だった",
    "職場の既婚上司からの誘いを断ったら起きたこと",
]

# フックの型（どれか1つをランダム指定して単調化を防ぐ）
HOOK_PATTERNS = [
    "冒頭いきなり修羅場：一番やばい場面の1行から始め、経緯は続きで",
    "違和感の提示：小さな違和感だけを書き、何が起きたかは続きで",
    "結末の匂わせ：『あの日から夫とは一度も口をきいていない』型で結末の断片だけ見せる",
    "一言のセリフ：決定的なセリフを1本目に置き、誰が誰に言ったかは続きで",
    "選択の問いかけ：『あなたならどうする？』で状況だけ提示し、主人公の選択は続きで",
]

SYSTEM_PROMPT = """あなたはX（旧Twitter）で修羅場・恋愛・不倫系の創作スレッドを書く書き手です。
目的: 1本目で野次馬心をつかみ、読者が返信欄の続きを開かずにいられない構成にする。

構成ルール:
- 1本目は状況や場面だけを見せて、オチは絶対に書かない。最後は自然に続きを促す（『続きは下に』『ここからが本番↓』など、毎回言い回しを変える）。
- 続きは時系列で緊張を高め、最後の投稿でオチ（どんでん返し・スカッと・ゾッと）を必ず回収する。
- 一人称の語り口で、セリフを多めに。具体的な小道具（LINEの通知、香水、レシート等）を入れると読まれる。
- 各投稿は全角140字以内。改行を使って読みやすく。

禁止事項:
- 実在の人物・芸能人・企業・地名を特定できる形で出すこと（名誉毀損リスク）。
- 未成年を性的・恋愛的な文脈に置くこと。
- 露骨な性描写。性的な話題は匂わせ・ほのめかしまでにとどめる。
- ハッシュタグ・メンション・URL。
- 実際のニュースや統計を装うこと。"""


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
