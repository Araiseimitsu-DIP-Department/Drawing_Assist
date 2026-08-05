"""選択中の公差候補とフィルタ判定を表示する。"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.general_tolerance import (
    _has_dimension_line_support,
    _is_visual_parenthetical,
    detect_general_tolerance_candidates,
)
from drawing_assist.local_ocr import (
    analyze_page,
    analyze_scanned_page_tiles,
    enrich_scanned_ocr_page,
)

PDF = Path(r"c:\Users\SEIZOU20\Desktop\原図.pdf")
OCR_SCRIPT = ROOT / "src" / "drawing_assist" / "windows_ocr.ps1"


def main() -> None:
    doc = fitz.open(PDF)
    page = doc[0]
    ocr = analyze_page(page, scanned=True)
    tiles = analyze_scanned_page_tiles(page)
    enriched = enrich_scanned_ocr_page(ocr, tiles)
    candidates = detect_general_tolerance_candidates(
        page,
        0,
        standard="jis_b_0405",
        grade="m",
        ocr_script=OCR_SCRIPT,
        local_ocr_page=ocr,
        scanned_tile_cache={0: tiles},
    )
    selected = [c for c in candidates if c.selected and not c.manual_required]
    print(f"selected={len(selected)} total={len(candidates)}")
    for candidate in selected:
        rect = fitz.Rect(candidate.rect)
        has_support = _has_dimension_line_support(
            enriched.image,
            rect,
            candidate.direction,
            candidate.kind,
            scale_x=enriched.scale_x,
            scale_y=enriched.scale_y,
        )
        parenthetical = _is_visual_parenthetical(
            enriched.image,
            rect,
            candidate.direction,
            scale_x=enriched.scale_x,
            scale_y=enriched.scale_y,
        )
        print(
            f"  {candidate.source_text!r} kind={candidate.kind} "
            f"line={has_support} paren={parenthetical} "
            f"pos=({rect.x0 / page.rect.width:.2f},{rect.y0 / page.rect.height:.2f})"
        )
    doc.close()


if __name__ == "__main__":
    main()
