from __future__ import annotations

from pathlib import Path
import sys

import fitz
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.pdf_editor import Mark, apply_item_to_page


def main() -> None:
    output_dir = ROOT / "tmp" / "pdfs"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = output_dir / "rotated-marker-regression.pdf"
    output_png = output_dir / "rotated-marker-regression.png"

    document = fitz.open()
    page = document.new_page(width=841.92, height=1190.52)
    page.set_rotation(90)
    apply_item_to_page(
        page,
        Mark(
            page_index=0,
            rect=(250.0, 250.0, 400.0, 320.0),
            color="#fff24d",
            opacity=0.55,
        ),
    )
    document.save(output_pdf)
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(2.0, 2.0),
        alpha=False,
        annots=True,
    )
    pixmap.save(output_png)
    document.close()

    image = Image.open(output_png).convert("RGB")
    expected = image.getpixel((650, 570))
    old_wrong_location = image.getpixel((1810, 650))
    if not (
        expected[0] > 230
        and expected[1] > 220
        and expected[2] < 230
    ):
        raise RuntimeError(
            f"Marker is not visible at the requested location: {expected}"
        )
    if old_wrong_location != (255, 255, 255):
        raise RuntimeError(
            "Marker was written to the pre-fix rotated location: "
            f"{old_wrong_location}"
        )

    verified = fitz.open(output_pdf)
    verified_page = verified[0]
    annotation = next(verified_page.annots() or [])
    if (
        annotation.border.get("width") != 0
        or annotation.colors.get("fill") is None
    ):
        raise RuntimeError("Marker editability or border settings changed.")
    verified.close()
    print("PASS: rotated PDF marker position and editable borderless annotation")


if __name__ == "__main__":
    main()
