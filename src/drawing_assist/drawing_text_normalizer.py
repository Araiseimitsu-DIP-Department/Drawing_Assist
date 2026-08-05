"""機械図面向けのOCR文字列正規化と寸法トークン解析。"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

# 寸法として解釈するトークンのパターン
DIMENSION_TOKEN_PATTERN = re.compile(
    r"^(?P<prefix>[φΦØ⌀CR]?)"
    r"(?P<number>\d{1,4}(?:[.,]\d{1,4})?)"
    r"(?P<degree>[°。]?)$"
)

_LEADING_ZERO_BARE = re.compile(r"^0\d+$")

# 注記・表面処理などの接尾辞（以下は C0.2以下 などの限度指示として残す）
_NOTE_SUFFIX = re.compile(r"(?:MIN|MAX)$", re.IGNORECASE)

_EXPLICIT_TOLERANCE_MARKER = re.compile(
    r"(?:±|士|亇|干|土)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NormalizedDimension:
    """正規化後の寸法トークン。"""

    raw_text: str
    normalized_text: str
    prefix: str
    number_text: str
    degree: str
    nominal_value: float
    reference: bool = False


def normalize_drawing_text(value: str) -> str:
    """図面寸法向けにOCR文字列を正規化する。

    一律の O→0 置換は行わず、R/φ/M や数字間など文脈に応じて補正する。
    """

    text = unicodedata.normalize("NFKC", value).strip()
    text = re.sub(r"\s+", "", text)
    text = text.replace("＠", "φ").replace("@", "φ").replace("Φ", "φ").replace("Ø", "φ")
    text = re.sub(r"^[の劣効](?=\d)", "φ", text)
    text = re.sub(r"^[ー・](?=\d)", "", text)
    text = text.replace("Ｃ", "C").replace("Ｒ", "R").replace("Ｍ", "M")
    text = text.replace("。", "°")
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    text = re.sub(r"(?<=\d),[OＯ](?=\d)", ".0", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)[、。・·](?=\d)", ".", text)
    text = re.sub(r"(?<=\d)[‐‑‒–—−－](?=\d)", ".", text)
    text = re.sub(r"(?<=[RφM])O(?=\d)", "0", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=[RφM])Ｏ(?=\d)", "0", text)
    text = re.sub(r"(?<=\d)O(?=\d)", "0", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)Ｏ(?=\d)", "0", text)
    text = re.sub(r"(?<=\d)O$", "0", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)Ｏ$", "0", text)
    prefix_match = re.match(r"^(?P<prefix>[φΦCR])(?P<body>.+)$", text, flags=re.IGNORECASE)
    if prefix_match is not None:
        body = (
            prefix_match.group("body")
            .replace("O", "0")
            .replace("Ｏ", "0")
        )
        text = prefix_match.group("prefix") + body
    text = re.sub(r"^(?P<prefix>[CR])O(?=\.)", r"\g<prefix>0", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?P<prefix>[CR])0(?=\d)", r"\g<prefix>0.", text, flags=re.IGNORECASE)
    paren = re.fullmatch(r"[（(]0*(\d{1,4}(?:[.,]\d+)?)[）)]", text)
    if paren is not None:
        text = f"φ{paren.group(1)}"
    text = _NOTE_SUFFIX.sub("", text)
    return text.upper()


def is_tolerance_fragment(text: str) -> bool:
    """公差表記の断片かどうかを判定する。"""

    compact = normalize_drawing_text(text)
    if re.match(r"^(?:[φΦØ⌀CR]?)?\d", compact) and _EXPLICIT_TOLERANCE_MARKER.search(
        compact
    ):
        return False
    if _EXPLICIT_TOLERANCE_MARKER.search(compact):
        return True
    if compact.startswith(("+", "-", "−", "±")):
        return True
    if _LEADING_ZERO_BARE.fullmatch(compact) and len(compact) <= 2:
        return True
    if compact.startswith(".") and len(compact) > 1 and compact[1:].replace(".", "").isdigit():
        return True
    if re.fullmatch(r"0+\.\d+", compact):
        return True
    try:
        value = float(compact.replace(",", "."))
    except ValueError:
        return False
    return value < 1.0 and "." in compact


def parse_dimension_token(value: str) -> NormalizedDimension | None:
    """寸法として成立する場合のみトークンを返す。"""

    raw = unicodedata.normalize("NFKC", value).strip()
    reference = bool(re.fullmatch(r"[（(].+[）)]", raw))
    working = raw[1:-1] if reference else raw
    if is_tolerance_fragment(working):
        return None
    normalized = normalize_drawing_text(working)
    if not normalized or is_tolerance_fragment(normalized):
        return None
    match = DIMENSION_TOKEN_PATTERN.fullmatch(normalized)
    if match is None:
        return None
    prefix = match.group("prefix")
    number_text = match.group("number").replace(",", ".")
    try:
        nominal = float(number_text)
    except ValueError:
        return None
    if nominal <= 0 or nominal > 4000:
        return None
    return NormalizedDimension(
        raw_text=raw,
        normalized_text=normalized,
        prefix=prefix,
        number_text=number_text,
        degree=match.group("degree"),
        nominal_value=nominal,
        reference=reference,
    )


def normalize_raster_dimension_text(value: str) -> str:
    """既存コード互換の正規化ラッパー。"""

    return normalize_drawing_text(value)
