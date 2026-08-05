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

    assert is_tolerance_fragment("05")
    assert is_tolerance_fragment("0.05")
    assert is_tolerance_fragment(".05")
    assert is_tolerance_fragment("±0.1")
    assert not is_tolerance_fragment("6.2")
    assert not is_tolerance_fragment("50.6±0.05")

    token = parse_dimension_token("45 MIN")
    assert token is not None and token.nominal_value == 45.0

    token = parse_dimension_token("(026.5)")
    assert token is not None and token.nominal_value == 26.5

    assert parse_dimension_token("05") is None
    assert parse_dimension_token("SCALE 1:1") is None

    print("drawing text normalizer: OK")


if __name__ == "__main__":
    main()
