"""Build the small geometric Dahua ARC brand icon."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def main() -> None:
    size = 1024
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((28, 28, 996, 996), radius=224, fill="#102634")
    draw.ellipse((134, 134, 890, 890), outline="#46D5BD", width=42)
    draw.arc((222, 222, 802, 802), 210, 330, fill="#E9FBF7", width=48)
    draw.arc((222, 222, 802, 802), 30, 150, fill="#E9FBF7", width=48)
    draw.ellipse((426, 426, 598, 598), fill="#46D5BD")
    draw.rounded_rectangle((457, 170, 567, 310), radius=42, fill="#46D5BD")
    draw.rounded_rectangle((457, 714, 567, 854), radius=42, fill="#46D5BD")
    output = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "dahua_arc"
        / "brand"
        / "icon.png"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    icon.resize((256, 256), Image.Resampling.LANCZOS).save(output)


if __name__ == "__main__":
    main()
