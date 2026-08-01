#!/usr/bin/env python3
"""Generate Android launcher and Fire TV store assets from the brand artwork."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "nostalgiabox.png"
FONT = ROOT / "nostalgiabox/assets/fonts/VT323-Regular.ttf"
RES = ROOT / "firetv/app/src/main/res"

ICON_SIZES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}


def cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    scale = max(size[0] / image.width, size[1] / image.height)
    resized = image.resize(
        (round(image.width * scale), round(image.height * scale)),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - size[0]) // 2
    top = (resized.height - size[1]) // 2
    return resized.crop((left, top, left + size[0], top + size[1]))


def generate_banner(source: Image.Image) -> Image.Image:
    size = (1280, 720)
    background = cover(source, size).filter(ImageFilter.GaussianBlur(38))
    background = ImageEnhance.Brightness(background).enhance(0.22)
    banner = background.convert("RGB")

    art_size = 620
    art = source.resize((art_size, art_size), Image.Resampling.LANCZOS)
    banner.paste(art, (50, 50))

    draw = ImageDraw.Draw(banner)
    font = ImageFont.truetype(str(FONT), 102)
    accent_font = ImageFont.truetype(str(FONT), 46)
    text_x = 712
    draw.text((text_x, 228), "NOSTALGIA", font=font, fill="#F3D6A0")
    draw.text((text_x, 318), "BOX", font=font, fill="#EF7D32")
    draw.text((text_x + 4, 440), "THE CHANNELS YOU REMEMBER", font=accent_font, fill="#78B9B4")
    return banner


def main() -> None:
    source = Image.open(SOURCE).convert("RGB")
    if source.width != source.height:
        raise ValueError(f"Expected square source artwork, got {source.size}")

    for density, size in ICON_SIZES.items():
        output_dir = RES / f"mipmap-{density}"
        output_dir.mkdir(parents=True, exist_ok=True)
        source.resize((size, size), Image.Resampling.LANCZOS).save(
            output_dir / "ic_launcher.png",
            optimize=True,
        )

    banner = generate_banner(source)
    drawable_dir = RES / "drawable-nodpi"
    drawable_dir.mkdir(parents=True, exist_ok=True)
    banner.save(drawable_dir / "tv_banner.png", optimize=True)

    store_dir = ROOT / "firetv/store-assets"
    store_dir.mkdir(parents=True, exist_ok=True)
    banner.save(store_dir / "fire-tv-app-icon-1280x720.png", optimize=True)


if __name__ == "__main__":
    main()
