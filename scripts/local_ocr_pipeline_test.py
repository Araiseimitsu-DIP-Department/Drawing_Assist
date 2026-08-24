from __future__ import annotations

from pathlib import Path
import sys
import tempfile

import fitz
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.general_tolerance import detect_general_tolerance_candidates
from drawing_assist.local_ocr import LocalOcrLine, LocalOcrPage
from drawing_assist.web_app import (
    _detect_local_dimension_markings,
    _explicit_tolerance_range,
    _is_scanned_page,
)


def main() -> None:
    fit_range = _explicit_tolerance_range("φ26g6(-0.007 -0.020)", 26.0)
    assert fit_range is not None and abs(fit_range - 0.013) < 1e-9
    image = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.line((80, 200, 720, 200), fill="black", width=3)
    draw.line((80, 340, 720, 340), fill="black", width=3)

    def line(text: str, rect: tuple[float, float, float, float], score: float = 0.99):
        x0, y0, x1, y1 = rect
        return LocalOcrLine(
            text,
            score,
            ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
        )

    # 矩形サイズは実図面の寸法文字相当（過大な枠は幾何フィルタで除外される）
    # 画像PDFの素の整数は 8 未満をラベル扱いするため、候補確認は 12 を使う。
    ocr_page = LocalOcrPage(
        800,
        500,
        2.0,
        2.0,
        image,
        (
            line("12", (190, 92, 210, 100)),
            line("810", (190, 148, 218, 156)),
            line("50.6±0.05", (120, 100, 175, 108)),
            line("C0.2以下", (250, 100, 298, 108)),
            line("2. 指示なき角部はC0.2またはR0.2とする", (80, 190, 340, 208)),
            line("φ0.02", (330, 100, 362, 108)),
        ),
    )

    with tempfile.TemporaryDirectory(prefix="DrawingAssist-local-ocr-") as directory:
        left = Path(directory, "left.png")
        right = Path(directory, "right.png")
        image.crop((0, 0, 400, 500)).save(left)
        image.crop((400, 0, 800, 500)).save(right)
        pdf_path = Path(directory, "tiled.pdf")
        document = fitz.open()
        page = document.new_page(width=400, height=250)
        page.insert_image(fitz.Rect(0, 0, 200, 250), filename=str(left))
        page.insert_image(fitz.Rect(200, 0, 400, 250), filename=str(right))
        document.save(pdf_path)
        document.close()

        document = fitz.open(pdf_path)
        try:
            page = document[0]
            assert _is_scanned_page(page)
            markings = _detect_local_dimension_markings(page, ocr_page)
            marking_text = {item.source_text for item in markings}
            assert "50.6±0.05" in marking_text
            # 個別のR/C上限指示は一般注記ではなく、色分け対象の独立寸法。
            assert "C0.2以下" in marking_text
            assert not any("指示なき" in text for text in marking_text)
            assert "φ0.02" not in marking_text

            candidates = detect_general_tolerance_candidates(
                page,
                0,
                standard="jis_b_0405",
                grade="m",
                ocr_script=Path("unused"),
                local_ocr_page=ocr_page,
            )
            values = [item.nominal_value for item in candidates]
            assert 12.0 in values
            assert 810.0 not in values
        finally:
            document.close()

    print("local ONNX OCR parsing, tiled scan recognition, and safe filtering: OK")


if __name__ == "__main__":
    main()
