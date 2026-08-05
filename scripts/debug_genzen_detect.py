"""原図.pdf の公差候補検出を計測する。"""
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
from drawing_assist.local_ocr import analyze_page

PDF = Path(r"c:\Users\SEIZOU20\Desktop\原図.pdf")
OCR_SCRIPT = ROOT / "src" / "drawing_assist" / "windows_ocr.ps1"


def main() -> None:
    doc = fitz.open(PDF)
    page = doc[0]
    t0 = time.perf_counter()
    ocr_page = analyze_page(page, scanned=True)
    print(f"page OCR lines={len(ocr_page.lines)} time={time.perf_counter()-t0:.1f}s")
    t1 = time.perf_counter()
    candidates = detect_general_tolerance_candidates(
        page,
        0,
        standard="jis_b_0405",
        grade="m",
        ocr_script=OCR_SCRIPT,
        local_ocr_page=ocr_page,
    )
    print(f"candidates={len(candidates)} detect_time={time.perf_counter()-t1:.1f}s total={time.perf_counter()-t0:.1f}s")
    for candidate in candidates:
        rect = fitz.Rect(candidate.rect)
        print(
            f"  {candidate.source_text!r} val={candidate.nominal_value} "
            f"kind={candidate.kind} pos=({rect.x0/page.rect.width:.2f},{rect.y0/page.rect.height:.2f})"
        )
    doc.close()


if __name__ == "__main__":
    main()
