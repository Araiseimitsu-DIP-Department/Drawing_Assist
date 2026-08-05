"""原図.pdf の寸法検出を診断する。"""
from __future__ import annotations

import sys
import time
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
    merge_ocr_lines,
)
from dataclasses import replace
from drawing_assist.web_app import _detect_local_dimension_markings

PDF = Path(r"c:\Users\SEIZOU20\Desktop\原図.pdf")
OCR_SCRIPT = ROOT / "src" / "drawing_assist" / "windows_ocr.ps1"


def main() -> None:
    doc = fitz.open(PDF)
    page = doc[0]
    ocr = analyze_page(page, scanned=True)
    print(f"page OCR lines={len(ocr.lines)}")

    t0 = time.perf_counter()
    tiles = analyze_scanned_page_tiles(page)
    print(f"tile lines={len(tiles)} time={time.perf_counter()-t0:.1f}s")

    merged = replace(ocr, lines=merge_ocr_lines(ocr.lines, tiles))
    print(f"merged lines={len(merged.lines)}")

    tol = detect_general_tolerance_candidates(
        page, 0, standard="jis_b_0405", grade="m",
        ocr_script=OCR_SCRIPT, local_ocr_page=ocr,
        scanned_tile_cache={},
    )
    selected = [c for c in tol if not c.manual_required]
    print(f"\n公差候補: total={len(tol)} selected={len(selected)} manual={len(tol)-len(selected)}")
    for c in selected:
        print(f"  {c.source_text!r} val={c.nominal_value} kind={c.kind}")

    page_mark = _detect_local_dimension_markings(page, ocr, include_plain_dimensions=True)
    merged_mark = _detect_local_dimension_markings(page, merged, include_plain_dimensions=True)
    tol_mark = _detect_local_dimension_markings(page, ocr, include_plain_dimensions=False)
    merged_tol_mark = _detect_local_dimension_markings(page, merged, include_plain_dimensions=False)

    print(f"\n色分け(ページOCR, plain=True): {len(page_mark)}")
    print(f"色分け(マージOCR, plain=True): {len(merged_mark)}")
    print(f"色分け(ページOCR, tolのみ): {len(tol_mark)}")
    print(f"色分け(マージOCR, tolのみ): {len(merged_tol_mark)}")
    for m in merged_tol_mark[:30]:
        print(f"  {m.source_text!r} val={m.nominal_value} tol={m.tolerance_range}")
    if len(merged_tol_mark) > 30:
        print(f"  ... {len(merged_tol_mark)-30} more")

    # ±を含むOCR行の数
    plusminus = [l for l in merged.lines if "±" in l.text or "+" in l.text and "-" in l.text]
    print(f"\n±含むOCR行: {len(plusminus)}")
    for line in plusminus[:25]:
        print(f"  {line.text!r} score={line.score:.2f}")
    doc.close()


if __name__ == "__main__":
    main()
