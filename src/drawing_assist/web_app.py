from __future__ import annotations

import base64
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
from multiprocessing import freeze_support
from pathlib import Path
import re
import secrets
import sys
import tempfile
from threading import RLock, Thread
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import fitz
import webview

from drawing_assist.pdf_editor import (
    DimensionMark,
    DrawingItem,
    Mark,
    ReplacementMark,
    StampMark,
    TextHit,
    WorkRegionMark,
    WorkShapeMark,
    detect_enclosed_region,
    export_pdf,
    find_text_group,
    render_page_preview,
    strike_from_hit,
)


PDF_FILE_TYPES = ("PDFファイル (*.pdf)",)
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_UPLOAD_BYTES = 250 * 1024 * 1024


def _resource_path(*parts: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS, "drawing_assist", *parts)
    return Path(__file__).resolve().parent.joinpath(*parts)


def _number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _color(value: Any, default: str = "#fff24d") -> str:
    text = str(value or "")
    return text.lower() if COLOR_PATTERN.fullmatch(text) else default


def _point_in_polygon(
    point: tuple[float, float],
    polygon: tuple[tuple[float, float], ...],
) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current[1] > point[1]) != (previous[1] > point[1]):
            intersection_x = (
                (previous[0] - current[0])
                * (point[1] - current[1])
                / (previous[1] - current[1])
                + current[0]
            )
            if point[0] < intersection_x:
                inside = not inside
        previous = current
    return inside


class DrawingApi:
    def __init__(self) -> None:
        self.window: webview.Window | None = None
        self.document: fitz.Document | None = None
        self.source_path: Path | None = None
        self.display_name: str | None = None
        self.page_index = 0
        self.items: list[DrawingItem] = []
        self.replacement_selection: TextHit | None = None
        self.work_region_candidates: list[
            tuple[tuple[float, float], ...]
        ] = []
        self.work_region_color = "#fff24d"
        self.work_region_opacity = 0.32
        self.lock = RLock()
        self.upload_directory = tempfile.TemporaryDirectory(
            prefix="DrawingAssist-"
        )

    def set_window(self, window: webview.Window) -> None:
        self.window = window

    def drawing_assist_command(
        self,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a command received by the local HTTP API."""

        request = request or {}
        action = str(request.get("action") or "")
        arguments = request.get("arguments") or []
        handlers = {
            "get_initial_state": self.get_initial_state,
            "open_pdf": self.open_pdf,
            "previous_page": self.previous_page,
            "next_page": self.next_page,
            "undo": self.undo,
            "clear_page": self.clear_page,
            "clear_all": self.clear_all,
            "apply_action": self.apply_action,
            "detect_work_region": self.detect_work_region,
            "confirm_work_region": self.confirm_work_region,
            "cancel_work_region": self.cancel_work_region,
            "select_replacement": self.select_replacement,
            "confirm_replacement": self.confirm_replacement,
            "cancel_replacement": self.cancel_replacement,
            "save_pdf": self.save_pdf,
        }
        handler = handlers.get(action)
        if handler is None:
            return self._error("未対応の操作です。")
        if not isinstance(arguments, list):
            return self._error("操作パラメーターが不正です。")
        try:
            return handler(*arguments)
        except TypeError as exc:
            return self._error(f"操作パラメーターが不正です: {exc}")

    def _empty_state(self, message: str = "PDFを開くか、画面へドロップしてください。") -> dict[str, Any]:
        return {
            "ok": True,
            "loaded": False,
            "message": message,
            "today": date.today().strftime("%y.%m.%d"),
            "replacement_selection": None,
        }

    def get_initial_state(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._empty_state()
            return self._state()

    def _state(self, message: str = "") -> dict[str, Any]:
        if self.document is None or self.source_path is None:
            return self._empty_state(message or "PDFを開いてください。")

        page = self.document[self.page_index]
        preview_items = list(self.items)
        if self.work_region_candidates:
            preview_items.append(
                WorkRegionMark(
                    self.page_index,
                    tuple(self.work_region_candidates),
                    self.work_region_color,
                    self.work_region_opacity,
                )
            )
        image = render_page_preview(
            self.document,
            self.page_index,
            preview_items,
            zoom=1.8,
        )
        current_items = sum(
            item.page_index == self.page_index for item in self.items
        )
        has_text = bool(page.get_text("words"))
        replacement_selection = None
        if self.replacement_selection is not None:
            selected = self.replacement_selection
            replacement_selection = {
                "original_text": (
                    (
                        selected.preserved_prefix
                        + selected.nominal_text.strip()
                    )
                    or selected.text.strip()
                    or "画像範囲（文字情報なし）"
                ),
                "original_value": selected.nominal_text.strip(),
                "font_size": round(selected.font_size, 2),
                "has_text": bool(selected.nominal_text.strip()),
            }
        return {
            "ok": True,
            "loaded": True,
            "message": message,
            "file_name": self.display_name or self.source_path.name,
            "page_index": self.page_index,
            "page_number": self.page_index + 1,
            "page_count": self.document.page_count,
            "pdf_width": page.rect.width,
            "pdf_height": page.rect.height,
            "image": "data:image/png;base64,"
            + base64.b64encode(image).decode("ascii"),
            "item_count": len(self.items),
            "page_item_count": current_items,
            "has_text": has_text,
            "today": date.today().strftime("%y.%m.%d"),
            "replacement_selection": replacement_selection,
            "work_region_candidate_count": len(
                self.work_region_candidates
            ),
        }

    def _error(self, message: str) -> dict[str, Any]:
        return {"ok": False, "message": message}

    def open_pdf(self) -> dict[str, Any]:
        if self.window is None:
            return self._error("ウィンドウの準備ができていません。")
        paths = self.window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=PDF_FILE_TYPES,
        )
        if not paths:
            return {"ok": False, "cancelled": True, "message": ""}
        return self.load_pdf(paths[0])

    def load_pdf(self, path_value: str) -> dict[str, Any]:
        with self.lock:
            try:
                path = Path(path_value).expanduser().resolve()
                if path.suffix.lower() != ".pdf":
                    return self._error("PDFファイルを指定してください。")
                if not path.is_file():
                    return self._error("指定したPDFが見つかりません。")
                new_document = fitz.open(path)
                if new_document.needs_pass:
                    new_document.close()
                    return self._error("パスワード付きPDFには対応していません。")
                if new_document.page_count < 1:
                    new_document.close()
                    return self._error("ページのないPDFは開けません。")
            except Exception as exc:
                return self._error(f"PDFを開けませんでした: {exc}")

            if self.document is not None:
                self.document.close()
            self.document = new_document
            self.source_path = path
            self.display_name = path.name
            self.page_index = 0
            self.items.clear()
            self.replacement_selection = None
            self.work_region_candidates.clear()
            return self._state(
                "PDFを読み込みました。使いたいツールを選んで図面をクリックしてください。"
            )

    def load_pdf_bytes(
        self,
        file_name: str,
        content: bytes,
    ) -> dict[str, Any]:
        safe_name = Path(file_name or "document.pdf").name
        if not safe_name.lower().endswith(".pdf"):
            return self._error("PDFファイルを指定してください。")
        if not content:
            return self._error("PDFファイルが空です。")
        upload_path = Path(self.upload_directory.name, "current.pdf")
        try:
            upload_path.write_bytes(content)
        except OSError as exc:
            return self._error(f"PDFを読み込めませんでした: {exc}")
        result = self.load_pdf(str(upload_path))
        if result.get("ok") and result.get("loaded"):
            self.display_name = safe_name
            result["file_name"] = safe_name
        return result

    def previous_page(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if self.page_index > 0:
                self.page_index -= 1
                self.replacement_selection = None
                self.work_region_candidates.clear()
            return self._state()

    def next_page(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if self.page_index < self.document.page_count - 1:
                self.page_index += 1
                self.replacement_selection = None
                self.work_region_candidates.clear()
            return self._state()

    def undo(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if self.work_region_candidates:
                self.work_region_candidates.clear()
                return self._state("ワークの候補選択を取り消しました。")
            if self.items:
                self.items.pop()
                return self._state("直前の操作を取り消しました。")
            return self._state("取り消せる操作はありません。")

    def clear_page(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            before = len(self.items)
            self.items = [
                item for item in self.items if item.page_index != self.page_index
            ]
            self.work_region_candidates.clear()
            removed = before - len(self.items)
            return self._state(f"このページの追加内容を{removed}件消去しました。")

    def clear_all(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            removed = len(self.items)
            self.items.clear()
            self.work_region_candidates.clear()
            return self._state(f"すべての追加内容を{removed}件消去しました。")

    def detect_work_region(
        self,
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            page = self.document[self.page_index]
            point = fitz.Point(
                _number(
                    payload.get("x"),
                    0,
                    page.rect.x0,
                    page.rect.x1,
                ),
                _number(
                    payload.get("y"),
                    0,
                    page.rect.y0,
                    page.rect.y1,
                ),
            )
            operation = str(payload.get("operation") or "replace")
            if operation == "remove":
                before = len(self.work_region_candidates)
                self.work_region_candidates = [
                    polygon
                    for polygon in self.work_region_candidates
                    if not _point_in_polygon(
                        (point.x, point.y),
                        polygon,
                    )
                ]
                removed = before - len(self.work_region_candidates)
                return self._state(
                    "候補範囲を除外しました。"
                    if removed
                    else "クリック位置に除外できる候補がありません。"
                )
            try:
                polygon = detect_enclosed_region(page, point)
            except ValueError as exc:
                return self._state(str(exc))
            if operation != "add":
                self.work_region_candidates.clear()
            duplicate = any(
                len(existing) == len(polygon)
                and all(
                    math.dist(first, second) < 0.5
                    for first, second in zip(existing, polygon)
                )
                for existing in self.work_region_candidates
            )
            if not duplicate:
                self.work_region_candidates.append(polygon)
            self.work_region_color = _color(settings.get("color"))
            self.work_region_opacity = _number(
                settings.get("opacity"),
                0.32,
                0.08,
                1.0,
            )
            return self._state(
                "候補を表示しました。範囲を確認して確定してください。"
            )

    def confirm_work_region(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            if not self.work_region_candidates:
                return self._state("確定する候補範囲がありません。")
            self.items.append(
                WorkRegionMark(
                    self.page_index,
                    tuple(self.work_region_candidates),
                    self.work_region_color,
                    self.work_region_opacity,
                )
            )
            count = len(self.work_region_candidates)
            self.work_region_candidates.clear()
            return self._state(
                f"半自動で選択したワーク範囲を{count}か所マーキングしました。"
            )

    def cancel_work_region(self) -> dict[str, Any]:
        with self.lock:
            self.work_region_candidates.clear()
            return self._state("ワークの候補選択を解除しました。")

    def select_replacement(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            page = self.document[self.page_index]

            if all(key in payload for key in ("x0", "y0", "x1", "y1")):
                x0 = _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1)
                y0 = _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1)
                x1 = _number(payload.get("x1"), 0, page.rect.x0, page.rect.x1)
                y1 = _number(payload.get("y1"), 0, page.rect.y0, page.rect.y1)
                rect = fitz.Rect(
                    min(x0, x1),
                    min(y0, y1),
                    max(x0, x1),
                    max(y0, y1),
                )
                if rect.width < 2 or rect.height < 2:
                    return self._state(
                        "修正する元の寸法値を囲んでください。"
                    )
                self.replacement_selection = TextHit(
                    rect=(rect.x0, rect.y0, rect.x1, rect.y1),
                    text="画像範囲（文字情報なし）",
                    direction=(1.0, 0.0),
                    font_size=min(
                        36.0,
                        max(5.0, rect.height * 0.72),
                    ),
                    origin=(rect.x0 + 1.0, rect.y1 - 1.0),
                )
            else:
                point = fitz.Point(
                    _number(
                        payload.get("x"),
                        0,
                        page.rect.x0,
                        page.rect.x1,
                    ),
                    _number(
                        payload.get("y"),
                        0,
                        page.rect.y0,
                        page.rect.y1,
                    ),
                )
                hit = find_text_group(page, point)
                if hit is None:
                    return self._state(
                        "修正する寸法値の中央をクリックしてください。"
                    )
                self.replacement_selection = hit
            return self._state(
                "元の寸法値を選択しました。修正後の値を入力してください。"
            )

    def confirm_replacement(
        self,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")
            selection = self.replacement_selection
            if selection is None:
                return self._state(
                    "先に修正する元の寸法値を選択してください。"
                )
            value = str(
                settings.get("replacement_value") or ""
            ).strip()
            if not value:
                return self._state("修正後の寸法値を入力してください。")

            has_source_text = bool(selection.nominal_text.strip())
            font_size = (
                selection.font_size
                if has_source_text
                else _number(
                    settings.get("replacement_size"),
                    selection.font_size,
                    5.0,
                    36.0,
                )
            )
            self.items.append(
                ReplacementMark(
                    self.page_index,
                    selection.replacement_rect or selection.rect,
                    selection.direction,
                    value,
                    str(
                        settings.get("upper_tolerance") or ""
                    ).strip(),
                    str(
                        settings.get("lower_tolerance") or ""
                    ).strip(),
                    font_size,
                    origin=selection.origin,
                    font_name=selection.font_name,
                    font_color=selection.font_color,
                )
            )
            self.replacement_selection = None
            return self._state(
                f"寸法値を「{value}」に修正しました。"
            )

    def cancel_replacement(self) -> dict[str, Any]:
        with self.lock:
            self.replacement_selection = None
            return self._state("寸法値の選択を解除しました。")

    def apply_action(
        self,
        mode: str,
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            if self.document is None:
                return self._error("PDFを開いてください。")

            page = self.document[self.page_index]
            color = _color(settings.get("color"))
            opacity = _number(settings.get("opacity"), 0.42, 0.08, 1.0)
            message = ""

            if mode in {"word", "strike"}:
                if mode == "word" and all(
                    key in payload
                    for key in ("x0", "y0", "x1", "y1")
                ):
                    x0 = _number(
                        payload.get("x0"),
                        0,
                        page.rect.x0,
                        page.rect.x1,
                    )
                    y0 = _number(
                        payload.get("y0"),
                        0,
                        page.rect.y0,
                        page.rect.y1,
                    )
                    x1 = _number(
                        payload.get("x1"),
                        0,
                        page.rect.x0,
                        page.rect.x1,
                    )
                    y1 = _number(
                        payload.get("y1"),
                        0,
                        page.rect.y0,
                        page.rect.y1,
                    )
                    mark_style = str(
                        settings.get("mark_style") or "box"
                    )
                    if mark_style == "angled":
                        start = fitz.Point(x0, y0)
                        end = fitz.Point(x1, y1)
                        vector = end - start
                        length = math.hypot(vector.x, vector.y)
                        if length < 2.0:
                            return self._state(
                                "斜め文字に沿ってドラッグしてください。"
                            )
                        direction = vector / length
                        normal = fitz.Point(-direction.y, direction.x)
                        half_width = _number(
                            settings.get("highlight_width"),
                            11.0,
                            3.0,
                            40.0,
                        ) / 2
                        quad_points = (
                            start + normal * half_width,
                            end + normal * half_width,
                            end - normal * half_width,
                            start - normal * half_width,
                        )
                        rect = fitz.Rect(
                            quad_points[0],
                            quad_points[0],
                        )
                        for quad_point in quad_points[1:]:
                            rect.include_point(quad_point)
                        self.items.append(
                            Mark(
                                self.page_index,
                                (
                                    rect.x0,
                                    rect.y0,
                                    rect.x1,
                                    rect.y1,
                                ),
                                color,
                                opacity,
                                tuple(
                                    (point.x, point.y)
                                    for point in quad_points
                                ),
                            )
                        )
                        return self._state(
                            "斜め文字に沿ってマーキングしました。"
                        )
                    rect = fitz.Rect(
                        min(x0, x1),
                        min(y0, y1),
                        max(x0, x1),
                        max(y0, y1),
                    )
                    if rect.width < 1.5 or rect.height < 1.5:
                        return self._state(
                            "マークする文字・記号・範囲をドラッグで囲んでください。"
                        )
                    self.items.append(
                        Mark(
                            self.page_index,
                            (rect.x0, rect.y0, rect.x1, rect.y1),
                            color,
                            opacity,
                        )
                    )
                    return self._state(
                        "選択した範囲をマーキングしました。"
                    )
                point = fitz.Point(
                    _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
                )
                hit = find_text_group(page, point)
                if hit is None:
                    if mode == "word" and not page.get_text("words"):
                        return self._state(
                            "画像化PDFではクリック自動選択ができません。同じツールで文字・記号をドラッグして囲んでください。"
                        )
                    return self._state(
                        "文字が見つかりません。中央をクリックするか、必要な文字・記号をドラッグして囲んでください。"
                    )
                if mode == "word":
                    self.items.append(
                        Mark(
                            self.page_index,
                            hit.rect,
                            color,
                            opacity,
                            hit.quad,
                        )
                    )
                    message = f"「{hit.text.strip() or '寸法値'}」をマーキングしました。"
                else:
                    self.items.append(strike_from_hit(self.page_index, hit))
                    message = "選択した寸法に二重取消線を追加しました。"

            elif mode == "angled_rect":
                start = fitz.Point(
                    _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1),
                )
                end = fitz.Point(
                    _number(payload.get("x1"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y1"), 0, page.rect.y0, page.rect.y1),
                )
                vector = end - start
                length = math.hypot(vector.x, vector.y)
                if length < 2.0:
                    return self._state(
                        "寸法文字に沿ってドラッグしてください。"
                    )
                direction = vector / length
                normal = fitz.Point(-direction.y, direction.x)
                half_width = _number(
                    settings.get("highlight_width"),
                    11.0,
                    3.0,
                    40.0,
                ) / 2
                quad_points = (
                    start + normal * half_width,
                    end + normal * half_width,
                    end - normal * half_width,
                    start - normal * half_width,
                )
                rect = fitz.Rect(quad_points[0], quad_points[0])
                for quad_point in quad_points[1:]:
                    rect.include_point(quad_point)
                self.items.append(
                    Mark(
                        self.page_index,
                        (rect.x0, rect.y0, rect.x1, rect.y1),
                        color,
                        opacity,
                        tuple(
                            (point.x, point.y)
                            for point in quad_points
                        ),
                    )
                )
                message = "斜めの範囲をマーキングしました。"

            elif mode == "rect":
                x0 = _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1)
                y0 = _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1)
                x1 = _number(payload.get("x1"), 0, page.rect.x0, page.rect.x1)
                y1 = _number(payload.get("y1"), 0, page.rect.y0, page.rect.y1)
                rect = fitz.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
                if rect.width < 1.5 or rect.height < 1.5:
                    return self._state("マークする範囲をドラッグしてください。")
                self.items.append(
                    Mark(
                        self.page_index,
                        (rect.x0, rect.y0, rect.x1, rect.y1),
                        color,
                        opacity,
                    )
                )
                message = "指定した範囲をマーキングしました。"

            elif mode == "work_shape":
                raw_points = payload.get("points")
                if not isinstance(raw_points, list):
                    return self._state(
                        "ワーク形状に沿って点を指定してください。"
                    )
                points: list[tuple[float, float]] = []
                for raw_point in raw_points[:400]:
                    if (
                        not isinstance(raw_point, dict)
                        or "x" not in raw_point
                        or "y" not in raw_point
                    ):
                        continue
                    points.append(
                        (
                            _number(
                                raw_point.get("x"),
                                0,
                                page.rect.x0,
                                page.rect.x1,
                            ),
                            _number(
                                raw_point.get("y"),
                                0,
                                page.rect.y0,
                                page.rect.y1,
                            ),
                        )
                    )
                style = str(settings.get("work_shape_style") or "fill")
                if style not in {"fill", "line"}:
                    style = "fill"
                minimum_points = 3 if style == "fill" else 2
                if len(points) < minimum_points:
                    return self._state(
                        "面のマーキングは3点以上、実線のマーキングは2点以上を指定してください。"
                    )
                self.items.append(
                    WorkShapeMark(
                        self.page_index,
                        tuple(points),
                        color,
                        opacity,
                        style,
                        _number(
                            settings.get("work_line_width"),
                            6.0,
                            1.0,
                            30.0,
                        ),
                    )
                )
                message = (
                    "ワークの範囲をマーキングしました。"
                    if style == "fill"
                    else "ワークの実線をマーキングしました。"
                )

            elif mode == "dimension":
                text = str(settings.get("dimension_text") or "").strip()
                if not text:
                    return self._state("追加する寸法値を入力してください。")
                target = (
                    _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1),
                )
                label = (
                    _number(payload.get("x1"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y1"), 0, page.rect.y0, page.rect.y1),
                )
                self.items.append(
                    DimensionMark(
                        self.page_index,
                        target,
                        label,
                        text,
                        color,
                        opacity,
                        _number(settings.get("font_size"), 10.0, 5.0, 36.0),
                    )
                )
                message = f"寸法「{text}」と引出線を追加しました。"

            elif mode == "replace":
                value = str(settings.get("replacement_value") or "").strip()
                if not value:
                    return self._state("新しい寸法値を入力してください。")
                upper = str(settings.get("upper_tolerance") or "").strip()
                lower = str(settings.get("lower_tolerance") or "").strip()
                replacement_origin: tuple[float, float] | None = None
                replacement_font_name = ""
                replacement_font_color = (0.0, 0.0, 0.0)
                if all(key in payload for key in ("x0", "y0", "x1", "y1")):
                    x0 = _number(payload.get("x0"), 0, page.rect.x0, page.rect.x1)
                    y0 = _number(payload.get("y0"), 0, page.rect.y0, page.rect.y1)
                    x1 = _number(payload.get("x1"), 0, page.rect.x0, page.rect.x1)
                    y1 = _number(payload.get("y1"), 0, page.rect.y0, page.rect.y1)
                    rect = fitz.Rect(
                        min(x0, x1),
                        min(y0, y1),
                        max(x0, x1),
                        max(y0, y1),
                    )
                    if rect.width < 2 or rect.height < 2:
                        return self._state("置き換える元の寸法を囲んでください。")
                    direction = (1.0, 0.0)
                    inferred_size = min(14.0, max(5.0, rect.height * 0.72))
                    replacement_origin = (
                        rect.x0 + 1.0,
                        rect.y1 - 1.0,
                    )
                else:
                    point = fitz.Point(
                        _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                        _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
                    )
                    hit = find_text_group(page, point)
                    if hit is None:
                        return self._state(
                            "元の寸法が見つかりません。文字の中央付近をクリックしてください。"
                        )
                    rect = fitz.Rect(hit.rect)
                    if hit.replacement_rect is not None:
                        rect = fitz.Rect(hit.replacement_rect)
                    direction = hit.direction
                    inferred_size = hit.font_size
                    replacement_origin = hit.origin
                    replacement_font_name = hit.font_name
                    replacement_font_color = hit.font_color
                requested_size = settings.get("replacement_size")
                font_size = (
                    inferred_size
                    if requested_size in (None, "", 0, "0")
                    else _number(requested_size, inferred_size, 5.0, 36.0)
                )
                self.items.append(
                    ReplacementMark(
                        self.page_index,
                        (rect.x0, rect.y0, rect.x1, rect.y1),
                        direction,
                        value,
                        upper,
                        lower,
                        font_size,
                        origin=replacement_origin,
                        font_name=replacement_font_name,
                        font_color=replacement_font_color,
                    )
                )
                tolerance_text = ""
                if upper or lower:
                    tolerance_text = f"（上:{upper or 'なし'} / 下:{lower or 'なし'}）"
                message = f"寸法を「{value}」{tolerance_text}に修正しました。"

            elif mode in {"quality_stamp", "process_stamp"}:
                center = (
                    _number(payload.get("x"), 0, page.rect.x0, page.rect.x1),
                    _number(payload.get("y"), 0, page.rect.y0, page.rect.y1),
                )
                kind = "quality" if mode == "quality_stamp" else "process"
                self.items.append(
                    StampMark(
                        self.page_index,
                        center,
                        kind,
                        str(settings.get("stamp_name") or "担当者").strip() or "担当者",
                        str(settings.get("stamp_date") or date.today().strftime("%y.%m.%d")).strip(),
                        _number(settings.get("stamp_size"), 62.0, 30.0, 150.0),
                    )
                )
                message = "スタンプを追加しました。"
            else:
                return self._error("未対応のツールです。")

            return self._state(message)

    def save_pdf(self) -> dict[str, Any]:
        with self.lock:
            if self.document is None or self.source_path is None:
                return self._error("PDFを開いてください。")
            if self.window is None:
                return self._error("ウィンドウの準備ができていません。")

            display_stem = Path(
                self.display_name or self.source_path.name
            ).stem
            suggested = f"{display_stem}_編集済.pdf"
            desktop = Path.home() / "Desktop"
            save_directory = desktop if desktop.is_dir() else Path.home()
            paths = self.window.create_file_dialog(
                webview.FileDialog.SAVE,
                directory=str(save_directory),
                save_filename=suggested,
                file_types=PDF_FILE_TYPES,
            )
            if not paths:
                return {"ok": False, "cancelled": True, "message": ""}
            output = Path(paths[0])
            if output.suffix.lower() != ".pdf":
                output = output.with_suffix(".pdf")
            try:
                export_pdf(self.source_path, output, self.items)
            except ValueError:
                return self._error("原本とは別のファイル名で保存してください。")
            except Exception as exc:
                return self._error(f"PDFを保存できませんでした: {exc}")
            state = self._state(f"保存しました: {output}")
            state["saved_path"] = str(output)
            return state

    def close(self) -> None:
        with self.lock:
            if self.document is not None:
                self.document.close()
                self.document = None
            self.upload_directory.cleanup()


class DrawingHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        api: DrawingApi,
        token: str,
        web_root: Path,
    ) -> None:
        self.api = api
        self.token = token
        self.web_root = web_root
        super().__init__(("127.0.0.1", 0), DrawingRequestHandler)


class DrawingRequestHandler(BaseHTTPRequestHandler):
    server: DrawingHttpServer

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_bytes(
        self,
        status: int,
        content: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        content = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_bytes(status, content, "application/json; charset=utf-8")

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Drawing-Assist-Token", "")
        return secrets.compare_digest(supplied, self.server.token)

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 1 or length > MAX_UPLOAD_BYTES:
            return None
        return self.rfile.read(length)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        file_names = {
            "/": "index.html",
            "/index.html": "index.html",
            "/styles.css": "styles.css",
            "/app.js": "app.js",
        }
        file_name = file_names.get(path)
        if file_name is None:
            self._send_json(404, {"ok": False, "message": "Not found"})
            return
        try:
            content = (self.server.web_root / file_name).read_bytes()
        except OSError as exc:
            self._send_json(
                500,
                {"ok": False, "message": f"画面を読み込めませんでした: {exc}"},
            )
            return
        content_type = mimetypes.guess_type(file_name)[0]
        if file_name.endswith(".js"):
            content_type = "text/javascript"
        self._send_bytes(
            200,
            content,
            f"{content_type or 'application/octet-stream'}; charset=utf-8",
        )

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(403, {"ok": False, "message": "Forbidden"})
            return
        parsed = urlparse(self.path)
        body = self._read_body()
        if body is None:
            self._send_json(
                400,
                {"ok": False, "message": "ファイルまたは要求を読み込めませんでした。"},
            )
            return

        if parsed.path == "/api":
            try:
                request = json.loads(body.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request is not an object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._send_json(
                    400,
                    {"ok": False, "message": "操作要求が不正です。"},
                )
                return
            result = self.server.api.drawing_assist_command(request)
            self._send_json(200, result)
            return

        if parsed.path == "/upload":
            query = parse_qs(parsed.query)
            file_name = unquote((query.get("name") or ["document.pdf"])[0])
            result = self.server.api.load_pdf_bytes(file_name, body)
            self._send_json(200, result)
            return

        self._send_json(404, {"ok": False, "message": "Not found"})


def start_local_server(
    api: DrawingApi,
) -> tuple[DrawingHttpServer, Thread, str]:
    token = secrets.token_urlsafe(32)
    server = DrawingHttpServer(api, token, _resource_path("web"))
    thread = Thread(
        target=server.serve_forever,
        name="DrawingAssistHttp",
        daemon=True,
    )
    thread.start()
    return server, thread, token


def _self_test(
    pdf_path: Path,
    result_path: Path,
    preview_path: Path,
) -> None:
    from urllib.request import Request, urlopen

    api = DrawingApi()
    server, thread, token = start_local_server(api)
    base_url = f"http://127.0.0.1:{server.server_port}"
    annotation_check_path = result_path.with_name(
        f"{result_path.stem}-annotation-check.pdf"
    )
    result: dict[str, Any]
    try:
        with urlopen(f"{base_url}/app.js", timeout=20) as response:
            script = response.read().decode("utf-8")
        with urlopen(f"{base_url}/", timeout=20) as response:
            html = response.read().decode("utf-8")
        if "window.pywebview.api" in script:
            raise RuntimeError("packaged app.js still uses pywebview js_api")
        if 'fetch("/api"' not in script:
            raise RuntimeError("packaged app.js does not use the local HTTP API")
        for required_marker in (
            "select_replacement",
            "confirm_replacement",
            "originalReplacementValue",
            "work_shape",
            "detect_work_region",
            "confirm_work_region",
            "work_region_candidate_count",
            "markMethod",
            "mark_style",
        ):
            if required_marker not in script:
                raise RuntimeError(
                    f"packaged app.js is missing {required_marker}"
                )
        for obsolete_marker in (
            'data-mode="geometric_tolerance"',
            'data-mode="surface_finish"',
            'data-mode="detail_pair"',
            'data-mode="rect"',
            'data-mode="angled_rect"',
            "geometricSymbol1",
            "surfaceValue",
            "detailPairStep",
        ):
            if obsolete_marker in html or obsolete_marker in script:
                raise RuntimeError(
                    f"obsolete separate tool remains: {obsolete_marker}"
                )
        tool_buttons = re.findall(
            r'<button class="tool-card[\s\S]*?</button>',
            html,
        )
        if any("<kbd>" in button for button in tool_buttons):
            raise RuntimeError("tool shortcut badges are still displayed")
        if (
            "selectTool(modes" in script
            or 'event.key.toLowerCase() === "w"' in script
            or 'event.key.toLowerCase() === "d"' in script
            or "/^[0-9]$/.test(event.key)" in script
        ):
            raise RuntimeError("tool keyboard shortcuts are still enabled")

        request = Request(
            f"{base_url}/upload?name={pdf_path.name}",
            data=pdf_path.read_bytes(),
            method="POST",
            headers={
                "Content-Type": "application/pdf",
                "X-Drawing-Assist-Token": token,
            },
        )
        with urlopen(request, timeout=90) as response:
            state = json.loads(response.read().decode("utf-8"))
        diagonal_angle: float | None = None
        diagonal_target: fitz.Rect | None = None
        fallback_target: fitz.Rect | None = None
        manual_line: dict[str, Any] | None = None
        replacement_line: dict[str, Any] | None = None
        if api.document is not None:
            page = api.document[0]
            for block in page.get_text("rawdict").get("blocks", []):
                for line in block.get("lines", []):
                    text = "".join(
                        character.get("c", "")
                        for span in line.get("spans", [])
                        for character in span.get("chars", [])
                    ).strip()
                    direction = line.get("dir", (1.0, 0.0))
                    if (
                        text == "17.1"
                        and float(line["bbox"][0]) > 400
                    ):
                        replacement_line = line
                    if (
                        text
                        and abs(float(direction[0])) > 0.1
                        and abs(float(direction[1])) > 0.1
                    ):
                        candidate = fitz.Rect(line["bbox"])
                        fallback_target = fallback_target or candidate
                        if text == "C0.3":
                            manual_line = line
                        if text == "C0.15":
                            diagonal_target = candidate
        diagonal_target = diagonal_target or fallback_target
        if diagonal_target is not None:
            marked_state = api.apply_action(
                "word",
                {
                    "x": (
                        diagonal_target.x0 + diagonal_target.x1
                    ) / 2,
                    "y": (
                        diagonal_target.y0 + diagonal_target.y1
                    ) / 2,
                },
                {"color": "#fff24d", "opacity": 0.55},
            )
            if not marked_state.get("ok") or not api.items:
                raise RuntimeError("Diagonal highlight could not be applied")
            mark = api.items[-1]
            if not isinstance(mark, Mark) or not mark.quad:
                raise RuntimeError("Diagonal highlight has no oriented quad")
            edge = fitz.Point(mark.quad[1]) - fitz.Point(mark.quad[0])
            diagonal_angle = math.degrees(math.atan2(edge.y, edge.x))
            if abs(diagonal_angle) < 2.0:
                raise RuntimeError("Diagonal highlight was rendered horizontally")
            state = marked_state
        manual_diagonal_verified = False
        if manual_line is not None:
            recovered = fitz.recover_line_quad(manual_line)
            start = (recovered.ul + recovered.ll) / 2
            end = (recovered.ur + recovered.lr) / 2
            cross = recovered.ll - recovered.ul
            manual_state = api.apply_action(
                "word",
                {
                    "x0": start.x,
                    "y0": start.y,
                    "x1": end.x,
                    "y1": end.y,
                },
                {
                    "color": "#ff76bf",
                    "opacity": 0.55,
                    "mark_style": "angled",
                    "highlight_width": math.hypot(cross.x, cross.y),
                },
            )
            if not manual_state.get("ok") or len(api.items) < 2:
                raise RuntimeError("Manual diagonal highlight could not be applied")
            manual_mark = api.items[-1]
            if not isinstance(manual_mark, Mark) or not manual_mark.quad:
                raise RuntimeError("Manual diagonal highlight has no oriented quad")
            manual_diagonal_verified = True
            state = manual_state
        replacement_workflow_verified = False
        blank_tolerances_omitted = False
        if replacement_line is not None:
            target_rect = fitz.Rect(replacement_line["bbox"])
            selected_state = api.select_replacement(
                {
                    "x": (target_rect.x0 + target_rect.x1) / 2,
                    "y": (target_rect.y0 + target_rect.y1) / 2,
                }
            )
            selection_state = selected_state.get(
                "replacement_selection"
            )
            if (
                not selection_state
                or selection_state.get("original_value") != "17.1"
            ):
                raise RuntimeError(
                    "Original replacement value was not returned"
                )
            replacement_state = api.confirm_replacement(
                {
                    "replacement_value": "17.2",
                    "upper_tolerance": "",
                    "lower_tolerance": "",
                    "replacement_size": 30,
                }
            )
            replacement_mark = api.items[-1]
            if not isinstance(replacement_mark, ReplacementMark):
                raise RuntimeError("Replacement mark was not created")
            expected_span = replacement_line["spans"][0]
            expected_origin = tuple(
                float(value)
                for value in expected_span["origin"]
            )
            expected_direction = tuple(
                float(value)
                for value in replacement_line["dir"]
            )
            expected_size = float(expected_span["size"])
            if (
                replacement_mark.origin is None
                or math.dist(
                    replacement_mark.origin,
                    expected_origin,
                ) > 0.01
                or math.dist(
                    replacement_mark.direction,
                    expected_direction,
                ) > 0.001
                or abs(
                    replacement_mark.font_size - expected_size
                ) > 0.01
            ):
                raise RuntimeError(
                    "Replacement display format was not preserved"
                )
            blank_tolerances_omitted = (
                not replacement_mark.upper_tolerance
                and not replacement_mark.lower_tolerance
            )
            if not blank_tolerances_omitted:
                raise RuntimeError("Blank tolerances were retained")
            replacement_workflow_verified = True
            state = replacement_state
        unified_symbol_highlight_verified = False
        unified_detail_highlight_verified = False
        work_shape_verified = False
        work_line_verified = False
        work_auto_verified = False
        work_hatched_verified = False
        symbol_states = [
            api.apply_action(
                "word",
                {"x0": 485, "y0": 505, "x1": 535, "y1": 525},
                {"color": "#fff24d", "opacity": 0.50},
            ),
            api.apply_action(
                "word",
                {"x0": 70, "y0": 75, "x1": 125, "y1": 108},
                {"color": "#ff76bf", "opacity": 0.50},
            ),
        ]
        if (
            not all(item.get("ok") for item in symbol_states)
            or not all(
                isinstance(item, Mark)
                for item in api.items[-2:]
            )
        ):
            raise RuntimeError(
                "Unified geometric/surface symbol highlight failed"
            )
        unified_symbol_highlight_verified = True
        detail_states = [
            api.apply_action(
                "word",
                {"x0": 485, "y0": 475, "x1": 565, "y1": 492},
                {"color": "#ffb347", "opacity": 0.34},
            ),
            api.apply_action(
                "word",
                {"x0": 375, "y0": 330, "x1": 397, "y1": 348},
                {"color": "#ffb347", "opacity": 0.34},
            ),
        ]
        if (
            not all(item.get("ok") for item in detail_states)
            or not all(
                isinstance(item, Mark)
                for item in api.items[-2:]
            )
        ):
            raise RuntimeError("Unified detail highlight failed")
        unified_detail_highlight_verified = True
        candidate_state = api.detect_work_region(
            {"x": 210, "y": 410, "operation": "replace"},
            {"color": "#fff24d", "opacity": 0.32},
        )
        if (
            not candidate_state.get("ok")
            or candidate_state.get(
                "work_region_candidate_count"
            ) != 1
        ):
            raise RuntimeError("Semi-automatic work selection failed")
        confirmed_state = api.confirm_work_region()
        if (
            not confirmed_state.get("ok")
            or confirmed_state.get(
                "work_region_candidate_count"
            ) != 0
            or not isinstance(api.items[-1], WorkRegionMark)
        ):
            raise RuntimeError(
                "Semi-automatic work selection confirmation failed"
            )
        work_auto_verified = True
        hatched_candidate_state = api.detect_work_region(
            {"x": 520, "y": 420, "operation": "replace"},
            {"color": "#fff24d", "opacity": 0.32},
        )
        if (
            not hatched_candidate_state.get("ok")
            or hatched_candidate_state.get(
                "work_region_candidate_count"
            ) != 1
        ):
            raise RuntimeError(
                "Hatched work region selection failed"
            )
        hatched_confirmed_state = api.confirm_work_region()
        if (
            not hatched_confirmed_state.get("ok")
            or not isinstance(api.items[-1], WorkRegionMark)
        ):
            raise RuntimeError(
                "Hatched work region confirmation failed"
            )
        work_hatched_verified = True
        work_state = api.apply_action(
            "work_shape",
            {
                "points": [
                    {"x": 190, "y": 285},
                    {"x": 250, "y": 285},
                    {"x": 265, "y": 330},
                    {"x": 205, "y": 345},
                    {"x": 180, "y": 315},
                ]
            },
            {
                "color": "#fff24d",
                "opacity": 0.32,
                "work_shape_style": "fill",
                "work_line_width": 6,
            },
        )
        if (
            not work_state.get("ok")
            or not isinstance(api.items[-1], WorkShapeMark)
            or api.items[-1].style != "fill"
        ):
            raise RuntimeError("Workpiece area highlight failed")
        work_shape_verified = True
        work_line_state = api.apply_action(
            "work_shape",
            {
                "points": [
                    {"x": 310, "y": 270},
                    {"x": 330, "y": 290},
                    {"x": 350, "y": 270},
                    {"x": 370, "y": 300},
                ]
            },
            {
                "color": "#72df78",
                "opacity": 0.40,
                "work_shape_style": "line",
                "work_line_width": 5,
            },
        )
        if (
            not work_line_state.get("ok")
            or not isinstance(api.items[-1], WorkShapeMark)
            or api.items[-1].style != "line"
        ):
            raise RuntimeError("Workpiece solid-line highlight failed")
        work_line_verified = True
        state = work_line_state
        result_path.parent.mkdir(parents=True, exist_ok=True)
        export_pdf(pdf_path, annotation_check_path, api.items)
        annotation_document = fitz.open(annotation_check_path)
        editable_marker_count = 0
        transparent_annotation_borders = True
        for annotation_page in annotation_document:
            for annotation in annotation_page.annots() or []:
                if not annotation.colors.get("fill"):
                    continue
                editable_marker_count += 1
                raw_annotation = annotation_document.xref_object(
                    annotation.xref,
                    compressed=False,
                )
                if (
                    not re.search(
                        r"/C\s*\[\s*\]",
                        raw_annotation,
                    )
                    or annotation.border.get("width") != 0
                    or "/AP" not in raw_annotation
                ):
                    transparent_annotation_borders = False
        annotation_document.close()
        if (
            editable_marker_count == 0
            or not transparent_annotation_borders
        ):
            raise RuntimeError(
                "Marker annotations retained a visible fallback border"
            )
        image_value = str(state.get("image") or "")
        prefix = "data:image/png;base64,"
        if not state.get("ok") or not state.get("loaded"):
            raise RuntimeError(state.get("message") or "PDF did not load")
        if not image_value.startswith(prefix):
            raise RuntimeError("PDF preview was not rendered")
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(base64.b64decode(image_value[len(prefix):]))
        result = {
            "ok": True,
            "transport": "local-http",
            "packaged_assets": True,
            "tool_shortcuts_removed": True,
            "file_name": state.get("file_name"),
            "page_count": state.get("page_count"),
            "preview_bytes": preview_path.stat().st_size,
            "diagonal_highlight": diagonal_angle is not None,
            "manual_diagonal_highlight": manual_diagonal_verified,
            "replacement_workflow": replacement_workflow_verified,
            "blank_tolerances_omitted": blank_tolerances_omitted,
            "unified_symbol_highlight": (
                unified_symbol_highlight_verified
            ),
            "unified_detail_highlight": (
                unified_detail_highlight_verified
            ),
            "work_shape_auto": work_auto_verified,
            "work_shape_hatched": work_hatched_verified,
            "work_shape_fill": work_shape_verified,
            "work_shape_line": work_line_verified,
            "transparent_annotation_borders": (
                transparent_annotation_borders
            ),
            "editable_marker_annotations": editable_marker_count > 0,
            "diagonal_angle": (
                round(diagonal_angle, 2)
                if diagonal_angle is not None
                else None
            ),
        }
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        raise
    finally:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        api.close()
        annotation_check_path.unlink(missing_ok=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> None:
    api = DrawingApi()
    server, server_thread, token = start_local_server(api)
    window = webview.create_window(
        "図面寸法ハイライト",
        url=f"http://127.0.0.1:{server.server_port}/?token={token}",
        width=1480,
        height=940,
        min_size=(1080, 680),
        background_color="#111827",
        text_select=False,
    )
    if window is None:
        raise RuntimeError("ウィンドウを作成できませんでした。")
    api.set_window(window)

    def on_closed() -> None:
        api.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    window.events.closed += on_closed
    webview.start(gui="edgechromium", private_mode=False)


if __name__ == "__main__":
    freeze_support()
    if "--self-test" in sys.argv:
        test_index = sys.argv.index("--self-test")
        result_index = sys.argv.index("--result")
        preview_index = sys.argv.index("--preview")
        _self_test(
            Path(sys.argv[test_index + 1]).resolve(),
            Path(sys.argv[result_index + 1]).resolve(),
            Path(sys.argv[preview_index + 1]).resolve(),
        )
    else:
        main()
