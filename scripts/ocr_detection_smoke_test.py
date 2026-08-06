from __future__ import annotations

from pathlib import Path
import sys

import fitz
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.general_tolerance import (
    _local_ocr_general_candidates,
    detect_general_tolerance_candidates,
)
from drawing_assist.local_ocr import (
    analyze_page,
    analyze_scanned_page_tiles,
    enrich_scanned_ocr_page,
)
from drawing_assist.web_app import (
    _detect_local_dimension_markings,
    _detect_scanned_dimension_markings,
)


def main() -> None:
    source = ROOT / "output" / "pdf" / "画像PDF_範囲着色テスト.pdf"
    with fitz.open(source) as document:
        page = document[0]
        ocr_page = analyze_page(page, scanned=True)
        if "--no-tiles" not in sys.argv:
            tile_lines = analyze_scanned_page_tiles(page)
            ocr_page = enrich_scanned_ocr_page(ocr_page, tile_lines)
        if "--pipeline" in sys.argv:
            general = detect_general_tolerance_candidates(
                page,
                0,
                standard="jis_b_0405",
                grade="m",
                angle_shorter_side_length=10.0,
                ocr_script=ROOT / "src" / "drawing_assist" / "windows_ocr.ps1",
                local_ocr_page=ocr_page,
                scanned_tile_lines=(),
            )
        else:
            general = _local_ocr_general_candidates(
                page,
                0,
                standard="jis_b_0405",
                grade="m",
                angle_shorter_side_length=10.0,
                ocr_page=ocr_page,
                scanned_page=True,
            )
        markings = _detect_local_dimension_markings(
            page,
            ocr_page,
            include_plain_dimensions=True,
            scanned_page=True,
        )
        if "--pipeline" in sys.argv:
            markings.extend(
                _detect_scanned_dimension_markings(
                    page,
                    ROOT / "src" / "drawing_assist" / "windows_ocr.ps1",
                    include_plain_dimensions=False,
                )
            )
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        preview = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        draw = ImageDraw.Draw(preview)
        for index, candidate in enumerate(general, 1):
            rect = fitz.Rect(candidate.rect)
            bounds = tuple(int(value * 2) for value in rect)
            draw.rectangle(bounds, outline="#00a6ff", width=3)
            draw.text((bounds[0], max(0, bounds[1] - 14)), f"{index}:{candidate.source_text}", fill="#0055bb")
        diagnostics = ROOT / "tmp" / "pdfs"
        diagnostics.mkdir(parents=True, exist_ok=True)
        preview.save(diagnostics / "ocr-detection-overlay.png")
    print(f"ocr_lines={len(ocr_page.lines)}")
    print(f"general_candidates={len(general)}")
    print(f"color_candidates={len(markings)}")
    print("general=" + " | ".join(item.source_text for item in general))
    print("color=" + " | ".join(item.source_text for item in markings))
    assert all("(" not in item.source_text and ")" not in item.source_text for item in general)
    assert all("(" not in item.source_text and ")" not in item.source_text for item in markings)


if __name__ == "__main__":
    main()
