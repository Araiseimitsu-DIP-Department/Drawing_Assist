from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.drawing_text_normalizer import (
    is_tolerance_fragment,
    normalize_drawing_text,
    parse_dimension_token,
)


def main() -> None:
    assert normalize_drawing_text("RO.5") == "R0.5"
    phi_token = parse_dimension_token("φ1O")
    assert phi_token is not None and phi_token.nominal_value == 10.0
    assert normalize_drawing_text("12,O5") == "12.05"
    assert normalize_drawing_text("45 MIN") == "45"
    # RapidOCR の φ 誤読・先頭ゴミ補正
    assert normalize_drawing_text("Ω13.7±0.02") == "Φ13.7±0.02"
    assert normalize_drawing_text(".013.7±0.02") == "13.7±0.02"
    assert normalize_drawing_text("R0.2±01") == "R0.2±0.1"
    assert normalize_drawing_text("2.8+01") == "2.8+0.1"
    assert normalize_drawing_text("φ16H7+0018") == "Φ16H7+0.018"
    assert normalize_drawing_text("16H7+0.018") == "16H7+0.018"
    assert normalize_drawing_text("5.7+0.05") == "5.7+0.05"
    assert normalize_drawing_text("0)12士0.05") == "Φ12±0.05"
    assert normalize_drawing_text("$7.9±0.05") == "7.9±0.05"

    assert is_tolerance_fragment("05")
    assert is_tolerance_fragment("0.05")
    assert is_tolerance_fragment(".05")
    assert is_tolerance_fragment("±0.1")
    assert not is_tolerance_fragment("6.2")
    assert not is_tolerance_fragment("50.6±0.05")

    token = parse_dimension_token("45 MIN")
    assert token is not None and token.nominal_value == 45.0

    token = parse_dimension_token("(026.5)")
    assert token is not None and token.nominal_value == 26.5 and token.reference

    assert parse_dimension_token("05") is None
    assert parse_dimension_token("SCALE 1:1") is None

    print("drawing text normalizer: OK")


if __name__ == "__main__":
    main()
