"""原図.pdf の検出パイプラインを段階別に診断する。"""
from __future__ import annotations

import re
import sys
import time
from collections import Counter
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from drawing_assist.drawing_text_normalizer import parse_dimension_token
from drawing_assist.general_tolerance import (
    _EXPLICIT_TOLERANCE,
    _NON_DIMENSION_CONTEXT,
    _has_dimension_line_support,
    _is_visual_parenthetical,
    _local_ocr_general_candidates,
    _merge_general_tolerance_candidates,
    _reject_unreliable_dimension,
    _supplemental_tiled_candidates,
    _tiled_dimension_candidates,
    _candidate_kind,
    extract_drawing_tolerance_notes,
    angle_tolerance,
    _explicit_tolerance_rects_from_ocr_page,
)
from drawing_assist.local_ocr import analyze_page

PDF = Path(r"c:\Users\SEIZOU20\Desktop\原図.pdf")
OCR_SCRIPT = ROOT / "src" / "drawing_assist" / "windows_ocr.ps1"


def diagnose_local(page: fitz.Page, ocr_page) -> None:
    reasons: Counter[str] = Counter()
    parsed_samples: list[str] = []
    for line in ocr_page.lines:
        text = line.text
        parsed = parse_dimension_token(text)
        if parsed is None:
            if _EXPLICIT_TOLERANCE.search(text) or _NON_DIMENSION_CONTEXT.search(text):
                reasons["non_dimension_context"] += 1
            else:
                reasons["unparsed"] += 1
            continue
        kind = _candidate_kind(parsed.prefix, parsed.degree)
        if kind is None:
            reasons["no_kind"] += 1
            continue
        compact = parsed.normalized_text.lstrip("△▲◆◇")
        if _reject_unreliable_dimension(
            kind=kind,
            prefix=parsed.prefix,
            degree=parsed.degree,
            nominal=parsed.nominal_value,
            compact=compact,
            score=line.score,
        ):
            reasons["reject_unreliable"] += 1
            parsed_samples.append(f"reject {text!r} -> {parsed.nominal_value}")
            continue
        rect = fitz.Rect(line.rect) & page.rect
        has_support = _has_dimension_line_support(
            ocr_page.image,
            rect,
            line.direction,
            kind,
            scale_x=ocr_page.scale_x,
            scale_y=ocr_page.scale_y,
        )
        if rect.y1 < page.rect.height * 0.08 or rect.y0 > page.rect.height * 0.86:
            reasons["edge_zone"] += 1
            parsed_samples.append(f"edge {text!r} y={rect.y0:.0f}-{rect.y1:.0f}")
            continue
        if (
            rect.x1 < page.rect.width * 0.22
            and rect.y1 < page.rect.height * 0.32
            and not has_support
        ):
            reasons["top_left"] += 1
            continue
        if not has_support:
            reasons["no_line_support"] += 1
            parsed_samples.append(f"no_support {text!r} val={parsed.nominal_value} score={line.score:.2f}")
            continue
        if _is_visual_parenthetical(
            ocr_page.image,
            rect,
            line.direction,
            scale_x=ocr_page.scale_x,
            scale_y=ocr_page.scale_y,
        ):
            reasons["parenthetical"] += 1
            continue
        reasons["would_pass"] += 1
        parsed_samples.append(f"PASS {text!r} val={parsed.nominal_value} kind={kind}")

    print("--- local OCR filter breakdown ---")
    for key, count in reasons.most_common():
        print(f"  {key}: {count}")
    print("--- samples ---")
    for sample in parsed_samples[:40]:
        print(f"  {sample}")
    if len(parsed_samples) > 40:
        print(f"  ... {len(parsed_samples) - 40} more")


def main() -> None:
    doc = fitz.open(PDF)
    page = doc[0]
    ocr_page = analyze_page(page)
    print(f"OCR lines={len(ocr_page.lines)}")

    local = _local_ocr_general_candidates(
        page, 0, standard="jis_b_0405", grade="m",
        angle_shorter_side_length=10, ocr_page=ocr_page,
    )
    print(f"local candidates={len(local)}")
    for c in local:
        print(f"  local {c.nominal_value} {c.source_text!r}")

    diagnose_local(page, ocr_page)

    notes = extract_drawing_tolerance_notes("\n".join(l.text for l in ocr_page.lines))
    angle_override = notes.angle_tolerance or angle_tolerance(10, standard="jis_b_0405", grade="m")
    t0 = time.perf_counter()
    tiled = _tiled_dimension_candidates(
        page, 0, standard="jis_b_0405", grade="m",
        ocr_script=OCR_SCRIPT, angle_override=angle_override, notes=notes,
    )
    print(f"\ntiled candidates={len(tiled)} time={time.perf_counter()-t0:.1f}s")
    for c in tiled[:30]:
        print(f"  tiled {c.nominal_value} {c.source_text!r}")
    if len(tiled) > 30:
        print(f"  ... {len(tiled)-30} more")

    merged = _merge_general_tolerance_candidates(local, tiled)
    supplemental = _supplemental_tiled_candidates(
        page, 0, standard="jis_b_0405", grade="m", ocr_script=OCR_SCRIPT,
        angle_override=angle_override, notes=notes,
        ocr_tolerance_rects=_explicit_tolerance_rects_from_ocr_page(ocr_page),
        existing=merged,
    )
    print(f"supplemental added={len(supplemental)} merged={len(merged)}")
    doc.close()


if __name__ == "__main__":
    main()
