from __future__ import annotations

import math
from pathlib import Path
import sys
import tempfile

import fitz


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.pdf_editor import (
    ReplacementMark,
    export_pdf,
    find_japanese_font,
)


def _text_spans(page: fitz.Page) -> dict[str, dict]:
    return {
        "".join(
            character.get("c", "")
            for character in span.get("chars", [])
        ).replace("\u2012", "-").replace("\u2212", "-"): span
        for block in page.get_text("rawdict").get("blocks", [])
        for line in block.get("lines", [])
        for span in line.get("spans", [])
    }


def main() -> None:
    html = (ROOT / "src" / "drawing_assist" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "src" / "drawing_assist" / "web" / "app.js").read_text(
        encoding="utf-8"
    )
    for removed_control in (
        "replacementValueX",
        "replacementValueY",
        "replacementToleranceX",
        "replacementToleranceY",
        "位置の微調整",
    ):
        if removed_control in html:
            raise RuntimeError(
                f"Obsolete numeric position control remains: {removed_control}"
            )
    for required_ui in (
        "青い枠をドラッグ",
        "寸法値と公差は別々に動かせます",
        'id="replacementSize" type="number" min="5" max="36" step="0.01"',
    ):
        if required_ui not in html:
            raise RuntimeError(f"Drag guidance is missing: {required_ui}")
    for required_logic in (
        "replacementDrag",
        "replacementOffsets.valueX",
        "replacementOffsets.toleranceX",
        'replacementDrag.part === "value"',
        "replacementSize.value = String(sourceSize)",
    ):
        if required_logic not in script:
            raise RuntimeError(f"Replacement drag logic is missing: {required_logic}")
    hit_test_start = script.index("function replacementPartAt")
    hit_test_end = script.index("function drawReplacementCanvasPreview")
    hit_test = script[hit_test_start:hit_test_end]
    if hit_test.index("replacementHitAreas.tolerance") > hit_test.index(
        "replacementHitAreas.value"
    ):
        raise RuntimeError(
            "The nominal hit area still prevents selecting an adjacent tolerance."
        )

    with tempfile.TemporaryDirectory(
        prefix="DrawingAssist-layout-",
        ignore_cleanup_errors=True,
    ) as directory:
        source_path = Path(directory, "source.pdf")
        output_path = Path(directory, "output.pdf")
        document = fitz.open()
        page = document.new_page(width=320, height=200)
        page.draw_line((20, 101), (300, 101), color=(0, 0, 0), width=0.8)
        page.insert_text((85, 105), "10", fontsize=9)
        document.save(source_path)
        document.close()

        mark = ReplacementMark(
            page_index=0,
            rect=(78, 88, 105, 109),
            direction=(1.0, 0.0),
            value="12.5",
            upper_tolerance="+0.03",
            lower_tolerance="-0.01",
            font_size=14.0,
            tolerance_font_size=6.0,
            value_offset=(5.0, -4.0),
            tolerance_offset=(3.0, 2.0),
            origin=(85.0, 105.0),
        )
        export_pdf(source_path, output_path, [mark])

        result = fitz.open(output_path)
        spans = _text_spans(result[0])
        for expected_text in ("12.5", "+0.03", "-0.01"):
            if expected_text not in spans:
                result.close()
                raise RuntimeError(
                    "Replacement text was not rendered: "
                    f"{expected_text}; found={tuple(spans)}"
                )
        if abs(float(spans["12.5"]["size"]) - 14.0) > 0.05:
            raise RuntimeError("The nominal size was not rendered independently.")
        if any(
            abs(float(spans[text]["size"]) - 6.0) > 0.05
            for text in ("+0.03", "-0.01")
        ):
            raise RuntimeError("The tolerance size was not rendered independently.")
        nominal_origin = tuple(float(value) for value in spans["12.5"]["origin"])
        if math.dist(nominal_origin, (90.0, 101.0)) > 0.05:
            raise RuntimeError(
                f"The nominal offset was not applied: {nominal_origin}"
            )

        font = fitz.Font(fontfile=str(find_japanese_font()))
        tolerance_x = 85.0 + font.text_length("12.5", fontsize=14.0) + 0.5 + 3.0
        expected_tolerance_origins = {
            "+0.03": (tolerance_x, 101.0),
            "-0.01": (tolerance_x, 107.0),
        }
        for text, expected_origin in expected_tolerance_origins.items():
            actual_origin = tuple(float(value) for value in spans[text]["origin"])
            if math.dist(actual_origin, expected_origin) > 0.08:
                raise RuntimeError(
                    f"The tolerance offset was not applied: {text} {actual_origin}"
                )
        rendered_pixmap = result[0].get_pixmap(
            matrix=fitz.Matrix(4, 4),
            alpha=False,
        )
        preserved_line_pixel = rendered_pixmap.pixel(80 * 4, 101 * 4)
        if max(preserved_line_pixel) > 120:
            raise RuntimeError(
                "The source dimension line was erased by the replacement "
                f"whiteout: {preserved_line_pixel}"
            )
        result.close()

    print(
        "PASS: nominal and tolerance sizes and drag positions were rendered independently"
    )


if __name__ == "__main__":
    main()
