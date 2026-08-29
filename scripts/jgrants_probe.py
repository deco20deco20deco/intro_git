#!/usr/bin/env python3
"""案②を生き返らせられるかを判定する1本：jGrants の中身を実測する。

背景
----
「全国の補助金を検索できる」はもう商品にならない（jGrants APIが認証不要で公開され、
既存の検索サイトも個人開発のSaaSも既にある）。生き返る余地があるとすれば次の2点だけ。

    仮説A: jGrants は国の施策が中心で、市区町村の独自補助金はほとんど載っていない
           → 載っていない領域＝手つかず。そこだけを取りに行けば商品になる
    仮説B: レコードに更新日時があり、差分検知（新着・条件変更の通知）が実装できる
           → 「検索」ではなく「プッシュ」に商品を寄せられる

このスクリプトは、その2つを実データで確かめる。
A が偽（自治体の補助金も網羅されている）なら、案②は完全に終わり。

使い方
------
    python3 scripts/jgrants_probe.py

    認証不要・追加パッケージ不要。数分で終わる。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

BASE = "https://api.jgrants-portal.go.jp/exp"

# 全件一括取得ができない仕様なので、広めのキーワードで引いて id で重複排除する。
KEYWORDS = [
    "事業", "補助", "支援", "促進", "整備", "導入", "開発", "改善",
    "設備", "雇用", "人材", "研究", "環境", "観光", "農業", "医療",
    "創業", "販路", "デジタル", "省エネ", "子育て", "移住",
]

INTERVAL = 1.0


def get(path: str, params: dict | None = None) -> dict | None:
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "jgrants-probe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  ! HTTP {e.code} {url}", file=sys.stderr)
    except Exception as e:
        print(f"  ! {e} {url}", file=sys.stderr)
    return None


def main() -> None:
    print("■ キーワードを回して全件を集めます（重複はidで排除）\n")

    records: dict[str, dict] = {}
    for kw in KEYWORDS:
        payload = get(
            "/v1/public/subsidies",
            {"keyword": kw, "sort": "created_date", "order": "DESC", "acceptance": "0"},
        )
        time.sleep(INTERVAL)
        if not payload:
            continue
        # 仕様変更に備えて、結果配列のキー名を決め打ちしない
        items = payload.get("result") or payload.get("results") or []
        new = 0
        for it in items:
            key = it.get("id") or it.get("subsidy_id") or json.dumps(it, sort_keys=True)[:64]
            if key not in records:
                records[key] = it
                new += 1
        print(f"  {kw:<6} 取得 {len(items):>4} 件 / 新規 {new:>4} 件 / 累計 {len(records)}")

    if not records:
        sys.exit("\n1件も取得できませんでした。API仕様が変わった可能性があります。")

    sample = next(iter(records.values()))
    print("\n" + "=" * 60)
    print("■ レコードのフィールド一覧（仮説Bの判定材料）")
    print("=" * 60)
    for k, v in sample.items():
        shown = str(v)
        if len(shown) > 54:
            shown = shown[:54] + "…"
        print(f"  {k:<32} {shown}")

    date_fields = [k for k in sample if any(t in k.lower() for t in ("date", "time", "updated", "created"))]
    print("\n  日付・更新らしきフィールド:", ", ".join(date_fields) or "なし")
    if date_fields:
        print("  → 差分検知は実装できる。仮説B は成立。")
    else:
        print("  → 更新日時がない。全件を毎回保存して自前で差分を取る必要がある。")

    # 仮説A: 実施主体・対象地域の分布
    area_key = None
    for cand in ("target_area_search", "target_area_detail", "prefecture", "area"):
        if cand in sample:
            area_key = cand
            break

    print("\n" + "=" * 60)
    print("■ 対象地域の分布（仮説Aの判定材料）")
    print("=" * 60)
    if area_key:
        c = Counter()
        for r in records.values():
            v = r.get(area_key)
            c[str(v) if v else "(空)"] += 1
        national = sum(n for k, n in c.items() if "全国" in k)
        print(f"  対象地域フィールド: {area_key}")
        for k, n in c.most_common(20):
            print(f"    {k:<28} {n:>5} 件")
        print(f"\n  「全国」扱い: {national} 件 / 全体 {len(records)} 件"
              f" （{national/len(records)*100:.1f}%）")
        if national / len(records) > 0.6:
            print("  → 国の施策が中心。市区町村の独自補助金は手つかずの可能性が高い＝仮説A成立")
        else:
            print("  → 地域単位の補助金もかなり載っている。案②の隙間は狭い＝仮説A不成立の疑い")
    else:
        print("  対象地域を表すフィールドが見つからない。詳細APIで確認する必要がある。")

    # 実施機関が取れるか（自治体独自かどうかの直接の判定材料）
    print("\n" + "=" * 60)
    print("■ 詳細APIのフィールド（実施機関が取れるか）")
    print("=" * 60)
    sid = sample.get("id")
    if sid:
        detail = get(f"/v2/public/subsidies/id/{sid}")
        if detail:
            items = detail.get("result") or detail.get("results") or []
            d = items[0] if isinstance(items, list) and items else detail
            for k in list(d.keys())[:40]:
                print(f"  {k}")
        else:
            print("  詳細APIの取得に失敗")
    else:
        print("  id が取れないため詳細APIを呼べない")

    print("\n" + "=" * 60)
    print(f"総ユニーク件数: {len(records)}")
    print("=" * 60)
    print("""
判定の読み方
  ・総件数が既存サービスの掲載数（1万件規模を謳うものがある）を大きく下回り、
    かつ「全国」比率が高い → jGrants に載らない自治体独自の領域が残っている＝案②は作り直せる
  ・地域単位の補助金も十分載っている → 差別化の余地がない＝案②は捨てる
""")


if __name__ == "__main__":
    main()
