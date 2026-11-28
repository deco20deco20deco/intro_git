"""
投稿用の雰囲気画像を Cloudflare Workers AI（FLUX.1 schnell）で生成して images/ に保存する
作成日: 20261128

費用:
  - Workers Free プランの無料枠（1日 10,000 Neurons）内で動かす前提
  - Free プランは無料枠を超えるとエラーで止まるだけで課金されない
    → Paid プランに切り替えない限り「いくらでもかかる」は起きない
  - 念のため1回あたりの生成上限（--max、既定40枚）も設ける

方針:
  - 人物は生成しない。小道具・場面のみ
  - スマホ写真っぽい生活感のある質感に寄せる

準備:
  1. Cloudflare 無料アカウント作成（Workers Free のまま）
  2. ダッシュボード > AI > Workers AI > 「REST API を使用」で
     APIトークン（Workers AI 権限）を発行し、アカウントIDを控える
  3. export CLOUDFLARE_ACCOUNT_ID=<アカウントID>
     export CLOUDFLARE_API_TOKEN=<トークン>
     （Claude Code のクラウド環境では、トークンは環境のネットワークシークレットに
      保存し、ここにはダミー値を入れる。プロキシが本物に差し替える）

使い方:
  pip install requests
  python gen_images.py                 # 全シーン×1枚
  python gen_images.py --per-scene 2   # 全シーン×2枚（計20枚）
"""

import argparse
import base64
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

OUT_DIR = Path(__file__).parent / "images"
MODEL = "@cf/black-forest-labs/flux-1-schnell"
STEPS = 8          # schnell の上限。品質と無料枠消費のバランス
SLEEP_SEC = 1.0    # 連続リクエストの間隔

STYLE = (
    "candid smartphone photo, slightly grainy, natural low light, shallow depth of field, "
    "realistic everyday atmosphere, no text, no logos, no people, no faces, no hands"
)

# 修羅場・不倫系スレッドに合う「何かあった感」のある場面
SCENES = {
    "phone_notification": "a smartphone lying face up on a dark bedside table at night, screen glowing with a blurred notification",
    "two_wine_glasses": "two half-finished wine glasses on a hotel room table, one with a faint lipstick mark",
    "hotel_corridor": "a dim hotel corridor at night with a single door slightly open, warm light leaking out",
    "removed_ring": "a wedding ring left alone on a bathroom sink next to a running faucet",
    "receipt": "a crumpled restaurant receipt for two on a kitchen counter, unreadable text",
    "car_parking": "a parked car in a dark parking lot at night, wet asphalt reflecting neon signs",
    "empty_dining": "a family dining table with one untouched dinner plate and a cold cup of tea",
    "suitcase": "a half-packed suitcase on an unmade bed in early morning light",
    "rain_window": "rain on a window at night with blurred city lights behind it",
    "perfume": "an unfamiliar perfume bottle on a man's office desk next to a laptop",
}


def generate(account_id: str, token: str, prompt: str, seed: int) -> bytes:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{MODEL}"
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"prompt": prompt, "steps": STEPS, "seed": seed},
        timeout=120,
    )
    if r.status_code == 429:
        raise RuntimeError("無料枠の上限に達した可能性（429）。翌日（UTC 0時以降）に再実行")
    r.raise_for_status()
    body = r.json()
    if not body.get("success", True) or "result" not in body:
        raise RuntimeError(f"生成失敗: {body.get('errors')}")
    return base64.b64decode(body["result"]["image"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-scene", type=int, default=1, help="1シーンあたりの枚数")
    ap.add_argument("--max", type=int, default=40, help="1回の実行で生成する上限枚数")
    args = ap.parse_args()

    account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "dummy-replaced-by-proxy")
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")

    made = 0
    for name, scene in SCENES.items():
        for i in range(1, args.per_scene + 1):
            if made >= args.max:
                print(f"上限 {args.max} 枚に到達したので終了")
                return 0
            try:
                img = generate(account_id, token, f"{scene}, {STYLE}", seed=int(time.time()) + i)
            except Exception as e:  # 無料枠切れ等はここで止める（課金はされない）
                print(f"[stop] {name}_{i}: {e}", file=sys.stderr)
                return 1
            path = OUT_DIR / f"{stamp}_{name}_{i}.jpg"
            path.write_bytes(img)
            made += 1
            print(f"[ok] {path.name}")
            time.sleep(SLEEP_SEC)
    print(f"完了: {made} 枚")
    return 0


if __name__ == "__main__":
    sys.exit(main())
