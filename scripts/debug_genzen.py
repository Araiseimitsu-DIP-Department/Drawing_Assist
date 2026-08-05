"""原図.pdf の検出状況を診断する。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows コンソールの文字化け回避
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from drawing_assist.general_tolerance import detect_general_tolerance_candidates
from drawing_assist.local_ocr import analyze_page, local_ocr_available
from drawing_assist.web_app import _is_scanned_page

PDF = Path(r"c:\Users\SEIZOU20\Desktop\原図.pdf")
OCR_SCRIPT = ROOT / "src" / "drawing_assist" / "windows_ocr.ps1"


def main() -> None:
    doc = fitz.open(PDF)
    print(f"pages={doc.page_count} local_ocr={local_ocr_available()}")
    for page_index in range(doc.page_count):
        page = doc[page_index]
        words = page.get_text("words")
        print(
            f"\n=== page {page_index} words={len(words)} "
            f"scanned={_is_scanned_page(page)} "
            f"size={page.rect.width:.0f}x{page.rect.height:.0f} ==="
        )
        t0 = time.perf_counter()
        ocr = analyze_page(page)
        print(f"OCR lines={len(ocr.lines)} time={time.perf_counter() - t0:.1f}s")
        for line in ocr.lines[:25]:
            print(f"  {line.score:.2f} {line.text!r}")
        if len(ocr.lines) > 25:
            print(f"  ... and {len(ocr.lines) - 25} more")
        t0 = time.perf_counter()
        candidates = detect_general_tolerance_candidates(
            page,
            page_index,
            standard="jis_b_0405",
            grade="m",
            angle_shorter_side_length=10,
            ocr_script=OCR_SCRIPT,
        )
        elapsed = time.perf_counter() - t0
        print(f"candidates={len(candidates)} time={elapsed:.1f}s")
        for candidate in candidates:
            print(
                f"  {candidate.kind:8} {candidate.nominal_value:10.3g} "
                f"{candidate.source_text!r} manual={candidate.manual_required}"
            )
    doc.close()


if __name__ == "__main__":
    main()
