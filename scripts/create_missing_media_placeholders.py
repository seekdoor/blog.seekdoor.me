#!/usr/bin/env python3
"""Create non-destructive fallback images for unrecoverable legacy media paths."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("."))
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def placeholder(path: Path) -> Image.Image:
    image = Image.new("RGB", (1600, 900), "#f1f4f5")
    draw = ImageDraw.Draw(image)
    draw.rectangle((96, 96, 1504, 804), outline="#cbd5d9", width=6)
    draw.rounded_rectangle((184, 192, 1416, 708), radius=24, fill="#ffffff")
    draw.line((350, 585, 650, 390, 865, 550, 1070, 320, 1250, 585), fill="#0f766e", width=22)
    draw.ellipse((330, 286, 450, 406), fill="#f59e0b")
    draw.rectangle((310, 270, 1270, 605), outline="#0f766e", width=18)
    title = "旧媒体文件无法恢复"
    subtitle = "原始 Typecho 备份与在线站点均未包含该文件"
    title_font = font(54)
    subtitle_font = font(30)
    title_box = draw.textbbox((0, 0), title, font=title_font)
    subtitle_box = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    draw.text(
        ((1600 - (title_box[2] - title_box[0])) / 2, 640),
        title,
        fill="#12313a",
        font=title_font,
    )
    draw.text(
        ((1600 - (subtitle_box[2] - subtitle_box[0])) / 2, 715),
        subtitle,
        fill="#527078",
        font=subtitle_font,
    )
    return image


def main() -> int:
    args = arguments()
    root = args.output.resolve()
    report_path = root / "reports" / "typecho-migration.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    missing = report["assets"]["unresolved_references"]
    generated: list[str] = []
    skipped: list[str] = []

    for asset_path in missing:
        target = root / "static" / asset_path.lstrip("/")
        if target.exists() and not args.force:
            skipped.append(asset_path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        image = placeholder(target)
        if target.suffix.lower() == ".png":
            image.save(target, format="PNG", optimize=True)
        else:
            image.save(target, format="JPEG", quality=88, optimize=True)
        generated.append(asset_path)

    manifest = {
        "reason": "Source assets were missing from both local backup and live Typecho host.",
        "generated": generated,
        "skipped_existing": skipped,
    }
    output_path = root / "reports" / "missing-media-placeholders.json"
    output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
