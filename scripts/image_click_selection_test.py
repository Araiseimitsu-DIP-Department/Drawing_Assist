from __future__ import annotations

import base64
from io import BytesIO
import math
from pathlib import Path
import sys

import fitz
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.pdf_editor import Mark, export_pdf
from drawing_assist.web_app import DrawingApi


def make_scanned_drawing(
    path: Path,
) -> tuple[tuple[float, float], tuple[float, float]]:
    image = Image.new("RGB", (1600, 1000), "white")
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = (
        ImageFont.truetype(str(font_path), 72)
        if font_path.is_file()
        else ImageFont.load_default()
    )
    text = "C0.3 +0.05 -0.02"
    origin = (220, 280)
    draw.text(origin, text, font=font, fill="black")
    draw.line((150, 430, 1120, 430), fill="#777777", width=3)
    diagonal_layer = Image.new("RGBA", (460, 150), (255, 255, 255, 0))
    diagonal_draw = ImageDraw.Draw(diagonal_layer)
    diagonal_draw.text((20, 28), "C0.15", font=font, fill="black")
    diagonal = diagonal_layer.rotate(
        -55,
        expand=True,
        resample=Image.Resampling.BICUBIC,
    )
    diagonal = diagonal.crop(diagonal.getbbox())
    diagonal_origin = (1050, 420)
    image.paste(diagonal, diagonal_origin, diagonal)
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    document = fitz.open()
    page = document.new_page(width=800, height=500)
    page.insert_image(page.rect, stream=buffer.getvalue())
    document.save(path)
    document.close()
    text_box = draw.textbbox(origin, text, font=font)
    return (
        (
            (text_box[0] + text_box[2]) / 4,
            (text_box[1] + text_box[3]) / 4,
        ),
        (
            (diagonal_origin[0] + diagonal.width / 2) / 2,
            (diagonal_origin[1] + diagonal.height / 2) / 2,
        ),
    )


def main() -> None:
    temp_dir = ROOT / "tmp" / "pdfs"
    output_dir = ROOT / "output" / "pdf"
    temp_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    source = temp_dir / "scanned-click-source.pdf"
    result = output_dir / "scanned-click-selection.pdf"
    preview = temp_dir / "scanned-click-selection.png"
    horizontal_click, diagonal_click = make_scanned_drawing(source)

    api = DrawingApi()
    loaded = api.load_pdf(str(source))
    if not loaded.get("ok") or loaded.get("has_text"):
        raise RuntimeError("The generated source is not an image-only PDF.")
    candidate = api.apply_action(
        "word",
        {"x": horizontal_click[0], "y": horizontal_click[1]},
        {"color": "#fff24d", "opacity": 0.45},
    )
    if (
        not candidate.get("word_candidate")
        or candidate.get("page_item_count") != 0
        or not isinstance(api.word_candidate, Mark)
    ):
        raise RuntimeError(
            f"Image-only click did not create a pending candidate: {candidate}"
        )
    candidate_mark = api.word_candidate
    if (
        candidate_mark.rect[2] - candidate_mark.rect[0] < 80
        or candidate_mark.rect[3] - candidate_mark.rect[1] < 12
    ):
        raise RuntimeError(
            f"Detected candidate is unexpectedly small: {candidate_mark.rect}"
        )
    confirmed = api.confirm_word_candidate()
    if (
        confirmed.get("word_candidate")
        or confirmed.get("page_item_count") != 1
        or not isinstance(api.items[-1], Mark)
    ):
        raise RuntimeError(
            f"Pending candidate could not be confirmed: {confirmed}"
        )
    diagonal_candidate = api.apply_action(
        "word",
        {"x": diagonal_click[0], "y": diagonal_click[1]},
        {"color": "#ff76bf", "opacity": 0.45},
    )
    if (
        not diagonal_candidate.get("word_candidate")
        or not isinstance(api.word_candidate, Mark)
        or not api.word_candidate.quad
    ):
        raise RuntimeError(
            "A diagonal image-text candidate was not detected."
        )
    edge = fitz.Point(api.word_candidate.quad[1]) - fitz.Point(
        api.word_candidate.quad[0]
    )
    angle = abs(math.degrees(math.atan2(edge.y, edge.x)))
    angle = min(angle, abs(180 - angle))
    if angle < 25:
        raise RuntimeError(
            f"Diagonal image text was detected horizontally: {angle}"
        )
    confirmed = api.confirm_word_candidate()
    if confirmed.get("page_item_count") != 2:
        raise RuntimeError("The diagonal candidate was not confirmed.")
    cancel_candidate = api.apply_action(
        "word",
        {"x": horizontal_click[0], "y": horizontal_click[1]},
        {"color": "#72df78", "opacity": 0.45},
    )
    if not cancel_candidate.get("word_candidate"):
        raise RuntimeError("A replacement candidate was not shown.")
    cancelled = api.cancel_word_candidate()
    if (
        cancelled.get("word_candidate")
        or cancelled.get("page_item_count") != 2
    ):
        raise RuntimeError("Cancelling a candidate changed confirmed items.")
    export_pdf(source, result, api.items)
    image_value = str(confirmed.get("image") or "")
    prefix = "data:image/png;base64,"
    if not image_value.startswith(prefix):
        raise RuntimeError("Confirmed preview was not rendered.")
    preview.write_bytes(base64.b64decode(image_value[len(prefix) :]))
    api.close()

    outline_source = Path("C:/Users/SEIZOU20/Desktop/core.pdf")
    if outline_source.is_file():
        outline_api = DrawingApi()
        outline_loaded = outline_api.load_pdf(str(outline_source))
        if (
            not outline_loaded.get("ok")
            or outline_loaded.get("has_text")
        ):
            raise RuntimeError("core.pdf is not an outline-only PDF.")
        outline_candidate = outline_api.apply_action(
            "word",
            {"x": 323, "y": 396},
            {"color": "#fff24d", "opacity": 0.45},
        )
        outline_mark = outline_api.word_candidate
        if (
            not outline_candidate.get("word_candidate")
            or not isinstance(outline_mark, Mark)
            or outline_mark.rect[2] - outline_mark.rect[0] < 60
        ):
            raise RuntimeError(
                "Outline-PDF click selection failed: "
                f"{outline_candidate}"
            )
        outline_confirmed = outline_api.confirm_word_candidate()
        if outline_confirmed.get("page_item_count") != 1:
            raise RuntimeError("Outline-PDF candidate was not confirmed.")
        export_pdf(
            outline_source,
            output_dir / "outline-click-selection.pdf",
            outline_api.items,
        )
        outline_preview_value = str(
            outline_confirmed.get("image") or ""
        )
        (temp_dir / "outline-click-selection.png").write_bytes(
            base64.b64decode(outline_preview_value[len(prefix) :])
        )
        outline_api.close()
    print(
        "PASS: scanned and outline-only PDF click detection, "
        "pending confirmation, diagonal direction, and exported markers"
    )


if __name__ == "__main__":
    main()
