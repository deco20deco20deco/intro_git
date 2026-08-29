#!/usr/bin/env python3
"""案①の生死を決める1本：有価証券報告書のうち、広告宣伝費を開示している企業の割合を数える。

背景
----
広告宣伝費は EDINET 標準タクソノミの要素 `jppfs_cor:AdvertisingExpensesSGA` として
定義されているので、開示されてさえいれば機械的に取れる（表記ゆれの名寄せが不要）。
残る唯一の未知数が「そもそも何割の企業が販管費の内訳を開示しているか」。
IFRS・米国基準の提出会社や、内訳を注記に出さない会社があるため、実測しないと分からない。

判断の目安（本文の基準）
    網羅率 60% 以上 → 商品として成立する。作る
    30〜60%        → 業種を絞れば成立しうる。業種別の内訳を見て判断
    30% 以下       → 捨てる

使い方
------
    1. https://api.edinet-fsa.go.jp/ で Subscription-Key を取得（無料・即時）
    2. export EDINET_KEY=取得したキー
    3. python3 scripts/edinet_coverage.py --days 400 --sample 200

    追加パッケージ不要（標準ライブラリのみ）。--sample 200 でおよそ15〜25分。

注意
----
レート制限は公式に明示されていないため、既定で 1.2 秒間隔に抑えている。
429 や 403 が返り始めたら --interval を上げること。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter

API = "https://api.edinet-fsa.go.jp/api/v2"

# 有価証券報告書。訂正有報(130)は本体と重複するので除く。
DOC_TYPE_YUHO = "120"

# 数えたい要素。CSV内の「要素ID」列とこの文字列を突き合わせる。
TARGET = "jppfs_cor:AdvertisingExpensesSGA"

# 参考として同時に数える、販管費の内訳が開示されているかの傍証。
REFERENCE = [
    "jppfs_cor:SalariesAndAllowancesSGA",        # 給料及び手当
    "jppfs_cor:ProvisionForBonusesSGA",          # 賞与引当金繰入額
    "jppfs_cor:DepreciationSGA",                 # 減価償却費
    "jppfs_cor:SellingExpensesSGA",              # 販売費
]


def fetch(url: str, key: str, timeout: int = 60) -> bytes:
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}Subscription-Key={urllib.parse.quote(key)}"
    req = urllib.request.Request(full, headers={"User-Agent": "coverage-probe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def list_filings(day: dt.date, key: str) -> list[dict]:
    """その日に提出された有報のうち、CSV(XBRL)が取得できるものを返す。"""
    url = f"{API}/documents.json?date={day.isoformat()}&type=2"
    try:
        payload = json.loads(fetch(url, key).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit(f"認証エラー({e.code})。EDINET_KEY を確認してください。")
        print(f"  ! {day} 一覧取得に失敗 HTTP {e.code}", file=sys.stderr)
        return []
    except Exception as e:  # ネットワーク断など
        print(f"  ! {day} 一覧取得に失敗 {e}", file=sys.stderr)
        return []

    out = []
    for r in payload.get("results") or []:
        if r.get("docTypeCode") != DOC_TYPE_YUHO:
            continue
        # csvFlag が "1" のものだけが type=5 で取得できる
        if str(r.get("csvFlag")) != "1":
            continue
        out.append(
            {
                "docID": r.get("docID"),
                "edinetCode": r.get("edinetCode"),
                "filerName": r.get("filerName"),
                "submitDate": day.isoformat(),
            }
        )
    return out


def elements_in_document(doc_id: str, key: str) -> set[str] | None:
    """XBRL の CSV を取得し、含まれている要素IDの集合を返す。失敗時 None。"""
    url = f"{API}/documents/{doc_id}?type=5"
    try:
        blob = fetch(url, key, timeout=180)
    except Exception as e:
        print(f"  ! {doc_id} 本体取得に失敗 {e}", file=sys.stderr)
        return None

    found: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            members = [n for n in z.namelist() if n.lower().endswith(".csv")]
            for name in members:
                raw = z.read(name)
                # EDINET の XBRL CSV は UTF-16 のタブ区切り
                try:
                    text = raw.decode("utf-16")
                except UnicodeError:
                    text = raw.decode("utf-8", errors="replace")
                reader = csv.reader(io.StringIO(text), delimiter="\t")
                for row in reader:
                    if row:
                        found.add(row[0].strip())
    except zipfile.BadZipFile:
        print(f"  ! {doc_id} ZIPとして読めない（PDFのみの可能性）", file=sys.stderr)
        return None
    return found


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=400, help="遡る日数")
    p.add_argument("--sample", type=int, default=200, help="実際に中身を見る件数")
    p.add_argument("--interval", type=float, default=1.2, help="リクエスト間隔（秒）")
    p.add_argument("--out", default="edinet_coverage.csv", help="明細の出力先")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    key = os.environ.get("EDINET_KEY")
    if not key:
        sys.exit("環境変数 EDINET_KEY が未設定です。https://api.edinet-fsa.go.jp/ で取得してください。")

    today = dt.date.today()
    print(f"■ 直近 {args.days} 日の有価証券報告書を列挙します")

    filings: list[dict] = []
    for i in range(args.days):
        day = today - dt.timedelta(days=i)
        # 提出は平日に集中するので土日はスキップして時間を節約
        if day.weekday() >= 5:
            continue
        got = list_filings(day, key)
        if got:
            filings.extend(got)
            print(f"  {day} … {len(got)}件 (累計 {len(filings)})")
        time.sleep(args.interval)

    if not filings:
        sys.exit("有報が1件も取れませんでした。日付範囲かキーを確認してください。")

    # 同一企業が複数回出てくることがあるので EDINETコードで一意化
    uniq: dict[str, dict] = {}
    for f in filings:
        uniq.setdefault(f["edinetCode"] or f["docID"], f)
    pool = list(uniq.values())
    print(f"\n■ 有報 {len(filings)} 件 / 企業 {len(pool)} 社")

    random.seed(args.seed)
    sample = random.sample(pool, min(args.sample, len(pool)))
    print(f"■ うち {len(sample)} 社の中身を確認します\n")

    rows = []
    hit = 0
    checked = 0
    ref_counter: Counter[str] = Counter()

    for n, f in enumerate(sample, 1):
        els = elements_in_document(f["docID"], key)
        time.sleep(args.interval)
        if els is None:
            continue
        checked += 1
        has = TARGET in els
        hit += has
        for r in REFERENCE:
            if r in els:
                ref_counter[r] += 1
        rows.append(
            {
                "edinetCode": f["edinetCode"],
                "filerName": f["filerName"],
                "docID": f["docID"],
                "submitDate": f["submitDate"],
                "hasAdvertisingExpensesSGA": int(has),
            }
        )
        mark = "○" if has else "×"
        rate = hit / checked * 100
        print(f"  [{n:>4}/{len(sample)}] {mark} {(f['filerName'] or '')[:28]:<28} 途中経過 {rate:5.1f}%")

    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["edinetCode"])
        w.writeheader()
        w.writerows(rows)

    print("\n" + "=" * 56)
    print(f"確認できた社数        : {checked}")
    print(f"広告宣伝費あり        : {hit}")
    coverage = hit / checked * 100 if checked else 0.0
    print(f"網羅率                : {coverage:.1f}%")
    print("-" * 56)
    print("参考（販管費内訳の開示状況）")
    for r in REFERENCE:
        c = ref_counter[r]
        print(f"  {r:<44} {c/checked*100 if checked else 0:5.1f}%")
    print("=" * 56)

    if coverage >= 60:
        verdict = "作る。網羅率は商品として十分。"
    elif coverage >= 30:
        verdict = "業種を絞れば成立しうる。明細CSVを業種別に見てから判断。"
    else:
        verdict = "捨てる。この網羅率ではデータ商品にならない。"
    print(f"\n判定: {verdict}")
    print(f"明細: {args.out}")


if __name__ == "__main__":
    main()
