"""
投稿成績の集計（A/Bテスト判定用）
作成日: 20261128

posts_log.jsonl の実投稿分について X API から指標を取得し、
「画像あり/なし」「フックの型」「言語」別に集計する。

指標:
  - hook_imp   : 1本目のインプレッション
  - open_rate  : 2本目のインプ / 1本目のインプ（＝スレッドを開いた率の近似）
  - reply_rate : 1本目への返信数 / 1本目のインプ

使い方:
  python metrics.py                 # 投稿から24時間以上経ったものを集計
  python metrics.py --min-hours 48
"""

import argparse
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOG_PATH = Path(__file__).parent / "posts_log.jsonl"


def load_posts(min_hours: int) -> list[dict]:
    cutoff = datetime.now() - timedelta(hours=min_hours)
    posts = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("posted") and len(r.get("tweet_ids", [])) >= 2:
            if datetime.strptime(r["ts"], "%Y%m%d%H%M%S") <= cutoff:
                posts.append(r)
    return posts


def fetch_metrics(ids: list[str]) -> dict[str, dict]:
    import tweepy

    x = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )
    out = {}
    for i in range(0, len(ids), 100):
        resp = x.get_tweets(ids=ids[i:i + 100], tweet_fields=["public_metrics"], user_auth=True)
        for t in resp.data or []:
            out[str(t.id)] = t.public_metrics or {}
    return out


def summarize(rows: list[dict], key: str) -> None:
    groups = defaultdict(list)
    for r in rows:
        groups[str(r[key])].append(r)
    print(f"\n=== {key} 別 ===")
    print(f"{'区分':<40} {'n':>4} {'hook_imp中央値':>14} {'open_rate':>10} {'reply_rate':>10}")
    for g, rs in sorted(groups.items(), key=lambda kv: -statistics.median(x["hook_imp"] for x in kv[1])):
        print(
            f"{g[:40]:<40} {len(rs):>4} "
            f"{statistics.median(x['hook_imp'] for x in rs):>14,.0f} "
            f"{statistics.mean(x['open_rate'] for x in rs):>10.1%} "
            f"{statistics.mean(x['reply_rate'] for x in rs):>10.2%}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-hours", type=int, default=24)
    args = ap.parse_args()

    posts = load_posts(args.min_hours)
    if not posts:
        print("集計対象なし（実投稿から指定時間が経過したものがない）")
        return

    metrics = fetch_metrics([pid for p in posts for pid in p["tweet_ids"][:2]])
    rows = []
    for p in posts:
        h = metrics.get(p["tweet_ids"][0], {})
        s = metrics.get(p["tweet_ids"][1], {})
        hook_imp = h.get("impression_count", 0)
        rows.append({
            "image": "画像あり" if p.get("image") else "画像なし",
            "pattern": p["pattern"].split("：")[0],
            "lang": p.get("lang", "ja"),
            "hook_imp": hook_imp,
            "open_rate": s.get("impression_count", 0) / hook_imp if hook_imp else 0,
            "reply_rate": h.get("reply_count", 0) / hook_imp if hook_imp else 0,
        })

    print(f"集計対象: {len(rows)}スレッド")
    for key in ("image", "pattern", "lang"):
        summarize(rows, key)
    print("\n判定目安: 各区分 n>=15 になるまで勝敗を決めない（少数だとバズ1本で結果が歪む）")


if __name__ == "__main__":
    main()
