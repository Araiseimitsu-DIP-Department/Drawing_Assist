"""pdf.pdf / core.pdf の両検出を短時間で確認する。"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from drawing_assist.general_tolerance import detect_general_tolerance_candidates
from drawing_assist.local_ocr import (
    analyze_page,
    analyze_scanned_page_tiles,
    enrich_scanned_ocr_page,
)
from drawing_assist.web_app import (
    _detect_local_dimension_markings,
    _detect_scanned_dimension_markings,
    _merge_detected_markings,
)


def evaluate(pdf: Path) -> None:
    print("=" * 72, flush=True)
    print(pdf.name, flush=True)
    ocr_script = ROOT / "src" / "drawing_assist" / "windows_ocr.ps1"
    doc = fitz.open(pdf)
    page = doc[0]
    page_ocr = analyze_page(page, scanned=True)
    tiles = analyze_scanned_page_tiles(page)
    enriched = enrich_scanned_ocr_page(page_ocr, tiles)

    general = detect_general_tolerance_candidates(
        page,
        0,
        standard="jis_b_0405",
        grade="m",
        angle_shorter_side_length=10.0,
        ocr_script=ocr_script,
        local_ocr_page=page_ocr,
        scanned_tile_lines=tiles,
    )
    print(f"公差未記載: {len(general)}", flush=True)
    for c in general:
        print(f"  TOL {c.source_text!r} kind={c.kind}", flush=True)

    page_marks = _detect_local_dimension_markings(
        page, page_ocr, include_plain_dimensions=False, scanned_page=True
    )
    tile_marks = _detect_local_dimension_markings(
        page, enriched, include_plain_dimensions=False, scanned_page=True
    )
    windows = _detect_scanned_dimension_markings(
        page, ocr_script, include_plain_dimensions=False
    )
    color = _merge_detected_markings(
        _merge_detected_markings(page_marks, tile_marks, extra_score=0.7),
        windows,
        extra_score=0.6,
    )
    print(
        f"色分け: page={len(page_marks)} tile={len(tile_marks)} "
        f"win={len(windows)} merged={len(color)}",
        flush=True,
    )
    for m in sorted(color, key=lambda item: (item.rect[1], item.rect[0])):
        print(f"  CLR {m.source_text!r} tol={m.tolerance_range}", flush=True)
    doc.close()


def main() -> None:
    names = sys.argv[1:] or ["pdf.pdf"]
    for name in names:
        path = Path(r"c:\Users\SEIZOU20\Desktop") / name
        if path.exists():
            evaluate(path)
        else:
            print(f"missing: {path}", flush=True)


if __name__ == "__main__":
    main()
