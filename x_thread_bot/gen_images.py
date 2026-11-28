"""
投稿用の雰囲気画像を Vertex AI Imagen 4 で一括生成して images/ に保存する
作成日: 20261128

方針:
  - 人物は生成しない（person_generation=DONT_ALLOW）。小道具・場面のみ
  - スマホ写真っぽい質感（生活感）に寄せる。広告っぽい完璧な写真は避ける

準備:
  pip install google-genai
  gcloud auth application-default login
  gcloud services enable aiplatform.googleapis.com --project=<PROJECT_ID>
  export GOOGLE_CLOUD_PROJECT=<PROJECT_ID>
  export GOOGLE_CLOUD_LOCATION=us-central1

使い方:
  python gen_images.py                  # 全シーン×1枚
  python gen_images.py --per-scene 3    # 全シーン×3枚
  python gen_images.py --model imagen-4.0-ultra-generate-001   # 高画質版
"""

import argparse
import os
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types

OUT_DIR = Path(__file__).parent / "images"
DEFAULT_MODEL = "imagen-4.0-fast-generate-001"   # 安価版。質が足りなければ generate / ultra へ

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--per-scene", type=int, default=1, help="1シーンあたりの枚数（最大4）")
    ap.add_argument("--aspect", default="1:1", choices=["1:1", "3:4", "4:3", "9:16", "16:9"])
    args = ap.parse_args()

    client = genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
    )
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")

    for name, scene in SCENES.items():
        resp = client.models.generate_images(
            model=args.model,
            prompt=f"{scene}, {STYLE}",
            config=types.GenerateImagesConfig(
                number_of_images=min(args.per_scene, 4),
                aspect_ratio=args.aspect,
                person_generation="DONT_ALLOW",
            ),
        )
        imgs = resp.generated_images or []
        if not imgs:
            print(f"[skip] {name}: 生成なし（安全フィルタの可能性）")
            continue
        for i, g in enumerate(imgs, 1):
            path = OUT_DIR / f"{stamp}_{name}_{i}.png"
            path.write_bytes(g.image.image_bytes)
            print(f"[ok] {path.name}")


if __name__ == "__main__":
    main()
