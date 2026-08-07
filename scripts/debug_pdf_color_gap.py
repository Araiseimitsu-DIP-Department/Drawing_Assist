"""pdf.pdf の色分け検出ギャップを診断する。"""
from __future__ import annotations

import math
import re
import sys
import unicodedata
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from drawing_assist.drawing_text_normalizer import is_tolerance_fragment
from drawing_assist.local_ocr import (
    analyze_page,
    analyze_scanned_page_tiles,
    enrich_scanned_ocr_page,
)
from drawing_assist import web_app as w

PDF = Path(r"c:\Users\SEIZOU20\Desktop\pdf.pdf")
TARGETS = [
    "47.85±0.05",
    "40.15±0.1",
    "17.15",
    "17±0.1",
    "R85",
    "8.5±",
    "φ8.5",
    "Φ8.5",
    "12±0.1",
    "3±0.015",
    "3.8±",
    "4.4±",
]


def diagnose_line(page, ocr_page, line) -> list[str]:
    reasons: list[str] = []
    text = unicodedata.normalize("NFKC", line.text)
    if w._SURFACE_ROUGHNESS_PATTERN.search(text):
        reasons.append("surface")
    if re.search(r"[（(]", text) or re.search(r"[）)]", text):
        reasons.append("paren_char")
    if re.search(r"[ぁ-んァ-ヶ一-龯]", text):
        reasons.append("jp")
    if re.search(r"M\d", text, re.IGNORECASE):
        reasons.append("thread_m")
    compact = w._normalize_raster_dimension_text(text).lstrip("△▲◆◇")
    compact = re.sub(r"^A(?=\d)", "", compact)
    if w._PART_NUMBER_PATTERN.search(text) or w._PART_NUMBER_PATTERN.search(compact):
        reasons.append("part_no")
    if is_tolerance_fragment(text) or is_tolerance_fragment(compact):
        reasons.append("tol_frag")
    if w._MARKING_LIMIT_PATTERN.fullmatch(compact):
        reasons.append("limit")
    if not compact or (
        w._MARKING_CONTEXT_PATTERN.search(text)
        or w._MARKING_NOTE_CONTEXT_PATTERN.search(text)
    ):
        reasons.append("context")
    working = compact
    match = w._MARKING_NUMBER_PATTERN.search(working)
    if match is None:
        reasons.append("no_number")
        return reasons
    if working.endswith(("+", "-")):
        reasons.append("ends_sign")
    if working[:1] in {"+", "-", "−", "±"}:
        reasons.append("starts_sign")
    try:
        nominal = float(match.group("number").replace(",", "."))
    except ValueError:
        reasons.append("bad_float")
        return reasons
    kind = w._dimension_marking_kind(match.group("prefix"), match.group("degree"))
    tol = w._explicit_tolerance_range(working, nominal)
    if tol is not None and not w._is_plausible_tolerance_marking(text, nominal, tol):
        reasons.append(f"implausible({tol})")
    if tol is None:
        reasons.append("no_tol")
    rect = fitz.Rect(line.rect) & page.rect
    bottom_limit = 0.93 if tol is not None else 0.86
    edge_lengths = (
        math.dist(line.quad[0], line.quad[1]),
        math.dist(line.quad[0], line.quad[3]),
    )
    text_length = max(edge_lengths)
    text_thickness = min(edge_lengths)
    max_text_thickness = max(9.0, min(page.rect.width, page.rect.height) * 0.018)
    if rect.y1 < page.rect.height * 0.08 or rect.y0 > page.rect.height * bottom_limit:
        reasons.append("edge")
    if rect.width > page.rect.width * 0.34 or rect.height > page.rect.height * 0.25:
        reasons.append("size")
    if text_thickness > max_text_thickness:
        reasons.append(f"thick({text_thickness:.1f}>{max_text_thickness:.1f})")
    if text_length > max(page.rect.width, page.rect.height) * 0.18:
        reasons.append("long")
    if (
        rect.x1 < page.rect.width * 0.22
        and rect.y1 < page.rect.height * 0.32
        and tol is None
    ):
        reasons.append("top_left")
    paren = w._is_visual_parenthetical(
        ocr_page.image,
        rect,
        line.direction,
        scale_x=ocr_page.scale_x,
        scale_y=ocr_page.scale_y,
    )
    if paren:
        reasons.append("visual_paren")
    support = w._has_dimension_line_support(
        ocr_page.image,
        rect,
        line.direction,
        scale_x=ocr_page.scale_x,
        scale_y=ocr_page.scale_y,
        kind=kind,
        strict=False,
    )
    if not support:
        if not (tol is not None and line.score >= 0.85):
            reasons.append("no_line_support")
        else:
            reasons.append("no_line_support_but_tol_ok")
    if not reasons:
        reasons.append("PASS")
    reasons.append(f"nom={nominal}")
    reasons.append(f"tol={tol}")
    reasons.append(f"kind={kind}")
    reasons.append(f"score={line.score:.2f}")
    reasons.append(f"rect=({rect.x0:.0f},{rect.y0:.0f},{rect.x1:.0f},{rect.y1:.0f})")
    return reasons


def main() -> None:
    doc = fitz.open(PDF)
    page = doc[0]
    ocr = analyze_page(page, scanned=True)
    print(f"page OCR lines={len(ocr.lines)}")

    # 公差っぽいOCR行
    tol_lines = [
        line
        for line in ocr.lines
        if re.search(r"[±士土+\-]", line.text) and re.search(r"\d", line.text)
    ]
    print(f"tolerance-like OCR lines={len(tol_lines)}")
    for line in tol_lines:
        print(f"  OCR {line.text!r} score={line.score:.2f}")

    marks = w._detect_local_dimension_markings(
        page, ocr, include_plain_dimensions=False, scanned_page=True
    )
    print(f"\ncolor candidates(page)={len(marks)}")
    for m in marks:
        print(f"  DET {m.source_text!r} tol={m.tolerance_range}")

    print("\n--- filter breakdown for target-like lines ---")
    for line in ocr.lines:
        if not any(t.replace("φ", "").replace("Φ", "") in line.text.replace("φ", "").replace("Φ", "") for t in TARGETS):
            if not (re.search(r"[±士]", line.text) and re.search(r"\d", line.text)):
                continue
        reasons = diagnose_line(page, ocr, line)
        print(f"{line.text!r} -> {reasons}")

    # tiles enrich
    print("\n--- with tiles ---")
    tiles = analyze_scanned_page_tiles(page)
    enriched = enrich_scanned_ocr_page(ocr, tiles)
    marks2 = w._detect_local_dimension_markings(
        page, enriched, include_plain_dimensions=False, scanned_page=True
    )
    print(f"tiles={len(tiles)} enriched={len(enriched.lines)} color={len(marks2)}")
    for m in marks2:
        print(f"  DET {m.source_text!r} tol={m.tolerance_range}")
    doc.close()


if __name__ == "__main__":
    main()
