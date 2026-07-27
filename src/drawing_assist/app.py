from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import sys
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

import fitz
from PIL import Image, ImageDraw, ImageFont, ImageTk

try:
    from drawing_assist.pdf_editor import (
        DimensionMark,
        DrawingItem,
        Mark,
        StampMark,
        StrikeMark,
        dimension_label_rect,
        export_pdf,
        find_japanese_font,
        find_text_group,
        strike_from_hit,
    )
except ImportError:
    from pdf_editor import (
        DimensionMark,
        DrawingItem,
        Mark,
        StampMark,
        StrikeMark,
        dimension_label_rect,
        export_pdf,
        find_japanese_font,
        find_text_group,
        strike_from_hit,
    )


APP_NAME = "図面寸法ハイライト"
PAGE_MARGIN = 24
PRESET_COLORS = [
    ("黄", "#fff24d"),
    ("桃", "#ff8fe5"),
    ("水", "#66e3ef"),
    ("緑", "#7ee787"),
    ("橙", "#ffb347"),
    ("赤", "#ff6b6b"),
]


class DrawingAssistApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1480x940")
        self.root.minsize(1080, 680)

        self.document: fitz.Document | None = None
        self.source_path: Path | None = None
        self.page_index = 0
        self.items: list[DrawingItem] = []
        self.zoom = tk.DoubleVar(value=1.6)
        self.mode = tk.StringVar(value="word")
        self.opacity = tk.DoubleVar(value=0.42)
        self.current_color = "#fff24d"
        self.dimension_text = tk.StringVar(value="R0.1以下")
        self.dimension_font_size = tk.DoubleVar(value=10.0)
        self.stamp_name = tk.StringVar(value="担当者")
        self.stamp_date = tk.StringVar(value=datetime.now().strftime("'%y.%m.%d"))
        self.stamp_size = tk.DoubleVar(value=62.0)
        self.photo: ImageTk.PhotoImage | None = None
        self.drag_start_pdf: fitz.Point | None = None
        self.drag_start_canvas: tuple[float, float] | None = None
        self.drag_item: int | None = None
        self._color_buttons: list[tk.Button] = []
        self._font_path = find_japanese_font()

        self._build_ui()
        self._set_controls_enabled(False)
        self._set_status("「PDFを開く」から製品図面を選択してください。")

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(10, 8))
        toolbar.pack(fill=tk.X)

        self.open_button = ttk.Button(toolbar, text="PDFを開く", command=self.open_pdf)
        self.open_button.pack(side=tk.LEFT)
        self.save_button = ttk.Button(toolbar, text="別名で保存", command=self.save_pdf)
        self.save_button.pack(side=tk.LEFT, padx=(6, 18))

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=4)
        self.prev_button = ttk.Button(toolbar, text="◀ 前", width=7, command=self.previous_page)
        self.prev_button.pack(side=tk.LEFT, padx=(8, 2))
        self.page_label = ttk.Label(toolbar, text="- / -", width=9, anchor=tk.CENTER)
        self.page_label.pack(side=tk.LEFT)
        self.next_button = ttk.Button(toolbar, text="次 ▶", width=7, command=self.next_page)
        self.next_button.pack(side=tk.LEFT, padx=(2, 14))

        ttk.Label(toolbar, text="表示").pack(side=tk.LEFT)
        self.zoom_box = ttk.Combobox(
            toolbar,
            width=6,
            state="readonly",
            values=("80%", "100%", "125%", "160%", "200%", "250%"),
        )
        self.zoom_box.set("160%")
        self.zoom_box.bind("<<ComboboxSelected>>", self._zoom_changed)
        self.zoom_box.pack(side=tk.LEFT, padx=(4, 16))

        self.undo_button = ttk.Button(toolbar, text="元に戻す", command=self.undo)
        self.undo_button.pack(side=tk.LEFT, padx=2)
        self.clear_page_button = ttk.Button(
            toolbar, text="このページをクリア", command=self.clear_page
        )
        self.clear_page_button.pack(side=tk.LEFT, padx=2)
        self.clear_all_button = ttk.Button(toolbar, text="すべてクリア", command=self.clear_all)
        self.clear_all_button.pack(side=tk.LEFT, padx=2)

        options = ttk.Frame(self.root, padding=(10, 3))
        options.pack(fill=tk.X)

        ttk.Label(options, text="着色:").pack(side=tk.LEFT)
        ttk.Radiobutton(
            options,
            text="クリック（文字グループ）",
            variable=self.mode,
            value="word",
            command=self._mode_changed,
        ).pack(side=tk.LEFT, padx=(5, 8))
        ttk.Radiobutton(
            options,
            text="ドラッグ（範囲）",
            variable=self.mode,
            value="rect",
            command=self._mode_changed,
        ).pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(options, text="色:").pack(side=tk.LEFT)
        for label, color in PRESET_COLORS:
            button = tk.Button(
                options,
                text=label,
                bg=color,
                activebackground=color,
                width=3,
                relief=tk.SUNKEN if color == self.current_color else tk.RAISED,
                command=lambda selected=color: self.select_color(selected),
            )
            button.pack(side=tk.LEFT, padx=2)
            self._color_buttons.append(button)
        self.custom_color_button = ttk.Button(options, text="その他…", command=self.choose_color)
        self.custom_color_button.pack(side=tk.LEFT, padx=(4, 18))

        ttk.Label(options, text="濃さ:").pack(side=tk.LEFT)
        self.opacity_scale = ttk.Scale(
            options,
            from_=0.15,
            to=0.85,
            variable=self.opacity,
            orient=tk.HORIZONTAL,
            length=120,
        )
        self.opacity_scale.pack(side=tk.LEFT, padx=(4, 6))
        ttk.Label(options, text="薄 ← → 濃").pack(side=tk.LEFT)

        additions = ttk.Frame(self.root, padding=(10, 3))
        additions.pack(fill=tk.X)
        ttk.Label(additions, text="追加:").pack(side=tk.LEFT)
        ttk.Radiobutton(
            additions,
            text="二重取消線",
            variable=self.mode,
            value="strike",
            command=self._mode_changed,
        ).pack(side=tk.LEFT, padx=(5, 12))
        ttk.Radiobutton(
            additions,
            text="寸法＋引出線",
            variable=self.mode,
            value="dimension",
            command=self._mode_changed,
        ).pack(side=tk.LEFT)
        ttk.Entry(additions, textvariable=self.dimension_text, width=14).pack(
            side=tk.LEFT, padx=(4, 3)
        )
        ttk.Label(additions, text="文字").pack(side=tk.LEFT)
        ttk.Spinbox(
            additions,
            textvariable=self.dimension_font_size,
            from_=6,
            to=24,
            increment=1,
            width=4,
        ).pack(side=tk.LEFT, padx=(2, 12))

        ttk.Radiobutton(
            additions,
            text="品質保証印",
            variable=self.mode,
            value="stamp_quality",
            command=self._mode_changed,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            additions,
            text="加工図印",
            variable=self.mode,
            value="stamp_process",
            command=self._mode_changed,
        ).pack(side=tk.LEFT, padx=(2, 8))
        ttk.Label(additions, text="名前").pack(side=tk.LEFT)
        ttk.Entry(additions, textvariable=self.stamp_name, width=10).pack(
            side=tk.LEFT, padx=(3, 8)
        )
        ttk.Label(additions, text="日付").pack(side=tk.LEFT)
        ttk.Entry(additions, textvariable=self.stamp_date, width=11).pack(
            side=tk.LEFT, padx=(3, 8)
        )
        ttk.Label(additions, text="印径").pack(side=tk.LEFT)
        ttk.Spinbox(
            additions,
            textvariable=self.stamp_size,
            from_=40,
            to=100,
            increment=2,
            width=4,
        ).pack(side=tk.LEFT, padx=(3, 0))

        help_text = (
            "着色・取消線・スタンプはクリックで配置　｜　"
            "寸法＋引出線は、矢印の先端から文字位置までドラッグ　｜　原本PDFは変更されません"
        )
        ttk.Label(self.root, text=help_text, padding=(12, 5), foreground="#334155").pack(
            fill=tk.X
        )

        canvas_frame = ttk.Frame(self.root)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        self.canvas = tk.Canvas(
            canvas_frame,
            background="#3b424b",
            highlightthickness=0,
            cursor="crosshair",
        )
        h_scroll = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL, command=self.canvas.xview)
        v_scroll = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.root.bind("<Control-o>", lambda _event: self.open_pdf())
        self.root.bind("<Control-s>", lambda _event: self.save_pdf())
        self.root.bind("<Control-z>", lambda _event: self.undo())
        self.root.bind("<Escape>", lambda _event: self._cancel_drag())

        self.status = ttk.Label(self.root, relief=tk.SUNKEN, anchor=tk.W, padding=(8, 4))
        self.status.pack(fill=tk.X, side=tk.BOTTOM)

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in (
            self.save_button,
            self.prev_button,
            self.next_button,
            self.undo_button,
            self.clear_page_button,
            self.clear_all_button,
        ):
            widget.configure(state=state)
        self.zoom_box.configure(state="readonly" if enabled else "disabled")

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _mode_changed(self) -> None:
        messages = {
            "word": "寸法値をクリックすると、φ・＋・－・公差を含む文字グループを着色します。",
            "rect": "図面上をドラッグして、任意範囲を着色します。",
            "strike": "取り消したい文字をクリックすると、二重取消線を追加します。",
            "dimension": "矢印の先端から寸法文字を置く位置までドラッグします。",
            "stamp_quality": "図面上をクリックして、品質保証印を配置します。",
            "stamp_process": "図面上をクリックして、加工図印を配置します。",
        }
        self._set_status(messages.get(self.mode.get(), ""))

    def open_pdf(self, path: str | Path | None = None) -> None:
        if path is None:
            selected = filedialog.askopenfilename(
                title="製品図面PDFを開く",
                filetypes=[("PDFファイル", "*.pdf"), ("すべてのファイル", "*.*")],
            )
            if not selected:
                return
            path = selected

        candidate = Path(path)
        try:
            document = fitz.open(candidate)
            if document.needs_pass:
                document.close()
                raise ValueError("パスワード付きPDFには対応していません。")
            if document.page_count == 0:
                document.close()
                raise ValueError("ページがないPDFです。")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"PDFを開けませんでした。\n\n{exc}")
            return

        if self.document is not None:
            self.document.close()
        self.document = document
        self.source_path = candidate
        self.page_index = 0
        self.items.clear()
        self.root.title(f"{APP_NAME} - {candidate.name}")
        self._set_controls_enabled(True)
        self.render_page(reset_view=True)
        self._show_page_selection_hint(opened_name=candidate.name)

    def save_pdf(self) -> None:
        if self.document is None or self.source_path is None:
            return
        if not self.items:
            messagebox.showinfo(APP_NAME, "まだ編集内容がありません。")
            return

        suggested = f"{self.source_path.stem}_編集済み.pdf"
        selected = filedialog.asksaveasfilename(
            title="編集したPDFを保存",
            defaultextension=".pdf",
            initialdir=str(self.source_path.parent),
            initialfile=suggested,
            filetypes=[("PDFファイル", "*.pdf")],
        )
        if not selected:
            return

        try:
            export_pdf(self.source_path, selected, self.items)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"PDFを保存できませんでした。\n\n{exc}")
            return

        self._set_status(f"保存しました: {selected}")
        messagebox.showinfo(
            APP_NAME,
            f"編集したPDFを保存しました。\n\n{selected}\n\n原本PDFは変更していません。",
        )

    def _preview_font(self, point_size: float) -> ImageFont.FreeTypeFont:
        pixels = max(8, round(point_size * self.zoom.get()))
        return ImageFont.truetype(str(self._font_path), pixels)

    @staticmethod
    def _hex_bytes(color: str) -> tuple[int, int, int]:
        value = color.lstrip("#")
        return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))

    def _scaled_point(self, point: tuple[float, float]) -> tuple[float, float]:
        zoom = self.zoom.get()
        return point[0] * zoom, point[1] * zoom

    def _draw_dimension_preview(self, draw: ImageDraw.ImageDraw, item: DimensionMark) -> None:
        zoom = self.zoom.get()
        target = self._scaled_point(item.target)
        label_rect_pdf = dimension_label_rect(item)
        label_rect = tuple(value * zoom for value in label_rect_pdf)
        x0, y0, x1, y1 = label_rect
        if target[0] < x0:
            anchor = (x0, (y0 + y1) / 2)
        elif target[0] > x1:
            anchor = (x1, (y0 + y1) / 2)
        elif target[1] < y0:
            anchor = ((x0 + x1) / 2, y0)
        else:
            anchor = ((x0 + x1) / 2, y1)

        dx, dy = anchor[0] - target[0], anchor[1] - target[1]
        length = math.hypot(dx, dy)
        if length > 1:
            ux, uy = dx / length, dy / length
            nx, ny = -uy, ux
            arrow_length = max(6, item.font_size * zoom * 0.7)
            arrow_width = max(3, item.font_size * zoom * 0.25)
            base = (target[0] + ux * arrow_length, target[1] + uy * arrow_length)
            draw.line((base, anchor), fill=(0, 0, 0, 255), width=max(1, round(zoom)))
            draw.polygon(
                [
                    target,
                    (base[0] + nx * arrow_width, base[1] + ny * arrow_width),
                    (base[0] - nx * arrow_width, base[1] - ny * arrow_width),
                ],
                fill=(0, 0, 0, 255),
            )

        rgb = self._hex_bytes(item.color)
        alpha = int(max(0.05, min(1.0, item.opacity)) * 255)
        draw.rectangle(label_rect, fill=(*rgb, alpha), outline=(*rgb, 255), width=1)
        draw.text(
            (x0 + 2 * zoom, y0),
            item.text,
            font=self._preview_font(item.font_size),
            fill=(0, 0, 0, 255),
        )

    def _draw_stamp_preview(self, draw: ImageDraw.ImageDraw, item: StampMark) -> None:
        zoom = self.zoom.get()
        center = self._scaled_point(item.center)
        size = item.size * zoom
        radius = size / 2
        box = (
            center[0] - radius,
            center[1] - radius,
            center[0] + radius,
            center[1] + radius,
        )
        if item.kind == "quality":
            title, color = "品質保証", (227, 27, 35, 255)
        else:
            title, color = "加工図", (31, 42, 122, 255)
        line_width = max(2, round(size * 0.022))
        draw.ellipse(box, outline=color, width=line_width)
        first_y = box[1] + size * 0.34
        second_y = box[1] + size * 0.67
        draw.line((box[0] + 2, first_y, box[2] - 2, first_y), fill=color, width=line_width)
        draw.line((box[0] + 2, second_y, box[2] - 2, second_y), fill=color, width=line_width)
        rows = [
            (title, (box[1] + first_y) / 2, item.size * 0.17),
            (item.date, (first_y + second_y) / 2, item.size * 0.18),
            (item.name, (second_y + box[3]) / 2, item.size * 0.17),
        ]
        for text, y, point_size in rows:
            draw.text(
                (center[0], y),
                text,
                font=self._preview_font(point_size),
                fill=color,
                anchor="mm",
            )

    def render_page(self, *, reset_view: bool = False) -> None:
        if self.document is None:
            return

        page = self.document[self.page_index]
        zoom = self.zoom.get()
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, annots=True)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples).convert(
            "RGBA"
        )

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for item in self.items:
            if item.page_index != self.page_index:
                continue
            if isinstance(item, Mark):
                x0, y0, x1, y1 = item.rect
                rgb = self._hex_bytes(item.color)
                alpha = int(max(0.05, min(1.0, item.opacity)) * 255)
                scaled = (x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom)
                draw.rectangle(scaled, fill=(*rgb, alpha), outline=(*rgb, 255), width=1)
            elif isinstance(item, StrikeMark):
                start = self._scaled_point(item.start)
                end = self._scaled_point(item.end)
                normal = item.normal
                for offset in (-item.gap, item.gap):
                    shifted_start = (
                        start[0] + normal[0] * offset * zoom,
                        start[1] + normal[1] * offset * zoom,
                    )
                    shifted_end = (
                        end[0] + normal[0] * offset * zoom,
                        end[1] + normal[1] * offset * zoom,
                    )
                    draw.line(
                        (shifted_start, shifted_end),
                        fill=(0, 0, 0, 255),
                        width=max(1, round(item.width * zoom)),
                    )
            elif isinstance(item, DimensionMark):
                self._draw_dimension_preview(draw, item)
            elif isinstance(item, StampMark):
                self._draw_stamp_preview(draw, item)

        image = Image.alpha_composite(image, overlay).convert("RGB")
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        self.canvas.create_rectangle(
            PAGE_MARGIN - 1,
            PAGE_MARGIN - 1,
            PAGE_MARGIN + image.width + 1,
            PAGE_MARGIN + image.height + 1,
            fill="white",
            outline="#111827",
        )
        self.canvas.create_image(PAGE_MARGIN, PAGE_MARGIN, anchor=tk.NW, image=self.photo)
        self.canvas.configure(
            scrollregion=(
                0,
                0,
                image.width + PAGE_MARGIN * 2,
                image.height + PAGE_MARGIN * 2,
            )
        )
        self.page_label.configure(text=f"{self.page_index + 1} / {self.document.page_count}")
        self.prev_button.configure(state=tk.NORMAL if self.page_index > 0 else tk.DISABLED)
        self.next_button.configure(
            state=tk.NORMAL if self.page_index < self.document.page_count - 1 else tk.DISABLED
        )
        if reset_view:
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)

    def _event_to_pdf(self, event: tk.Event) -> fitz.Point | None:
        if self.document is None:
            return None
        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)
        x = (canvas_x - PAGE_MARGIN) / self.zoom.get()
        y = (canvas_y - PAGE_MARGIN) / self.zoom.get()
        page_rect = self.document[self.page_index].rect
        point = fitz.Point(x, y)
        return point if point in page_rect else None

    def _event_to_canvas(self, event: tk.Event) -> tuple[float, float]:
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def _find_hit(self, point: fitz.Point):
        if self.document is None:
            return None
        return find_text_group(self.document[self.page_index], point)

    def _on_press(self, event: tk.Event) -> None:
        if self.document is None:
            return
        point = self._event_to_pdf(event)
        if point is None:
            return
        mode = self.mode.get()

        if mode == "word":
            hit = self._find_hit(point)
            if hit is None:
                self._set_status(
                    "この位置では文字を検出できませんでした。ドラッグ（範囲）をお試しください。"
                )
                return
            self.items.append(
                Mark(
                    page_index=self.page_index,
                    rect=hit.rect,
                    color=self.current_color,
                    opacity=self.opacity.get(),
                )
            )
            self.render_page()
            self._set_status(f"「{hit.text}」を記号・公差を含めて着色しました。")
            return

        if mode == "strike":
            hit = self._find_hit(point)
            if hit is None:
                self._set_status("この位置では取消線を入れる文字を検出できませんでした。")
                return
            self.items.append(strike_from_hit(self.page_index, hit))
            self.render_page()
            self._set_status(f"「{hit.text}」に二重取消線を追加しました。")
            return

        if mode in ("stamp_quality", "stamp_process"):
            kind = "quality" if mode == "stamp_quality" else "process"
            self.items.append(
                StampMark(
                    page_index=self.page_index,
                    center=(point.x, point.y),
                    kind=kind,
                    name=self.stamp_name.get().strip(),
                    date=self.stamp_date.get().strip(),
                    size=max(40.0, min(100.0, self.stamp_size.get())),
                )
            )
            self.render_page()
            self._set_status("スタンプを配置しました。名前と日付は配置前に編集できます。")
            return

        if mode in ("rect", "dimension"):
            self.drag_start_pdf = point
            self.drag_start_canvas = self._event_to_canvas(event)
            self._cancel_drag_item()

    def _on_drag(self, event: tk.Event) -> None:
        if self.drag_start_canvas is None:
            return
        current = self._event_to_canvas(event)
        self._cancel_drag_item()
        if self.mode.get() == "rect":
            self.drag_item = self.canvas.create_rectangle(
                self.drag_start_canvas[0],
                self.drag_start_canvas[1],
                current[0],
                current[1],
                outline=self.current_color,
                width=2,
                dash=(5, 3),
            )
        elif self.mode.get() == "dimension":
            self.drag_item = self.canvas.create_line(
                self.drag_start_canvas[0],
                self.drag_start_canvas[1],
                current[0],
                current[1],
                fill="#111111",
                width=2,
                arrow=tk.FIRST,
                dash=(5, 3),
            )

    def _on_release(self, event: tk.Event) -> None:
        mode = self.mode.get()
        if mode not in ("rect", "dimension") or self.drag_start_pdf is None:
            return
        end = self._event_to_pdf(event)
        start = self.drag_start_pdf
        self._cancel_drag()
        if end is None:
            self._set_status("図面の内側でドラッグを終了してください。")
            return

        if mode == "rect":
            rect = fitz.Rect(start, end).normalize()
            if rect.width < 2 or rect.height < 2:
                self._set_status("もう少し大きい範囲をドラッグしてください。")
                return
            self.items.append(
                Mark(
                    page_index=self.page_index,
                    rect=(rect.x0, rect.y0, rect.x1, rect.y1),
                    color=self.current_color,
                    opacity=self.opacity.get(),
                )
            )
            self.render_page()
            self._set_status("選択範囲を着色しました。")
            return

        text = self.dimension_text.get().strip()
        if not text:
            self._set_status("追加する寸法文字を入力してください。")
            return
        if math.hypot(end.x - start.x, end.y - start.y) < 8:
            self._set_status("矢印の先端から文字位置まで、もう少し長くドラッグしてください。")
            return
        self.items.append(
            DimensionMark(
                page_index=self.page_index,
                target=(start.x, start.y),
                label=(end.x, end.y),
                text=text,
                color=self.current_color,
                opacity=self.opacity.get(),
                font_size=max(6.0, min(24.0, self.dimension_font_size.get())),
            )
        )
        self.render_page()
        self._set_status(f"寸法「{text}」と引出線を追加しました。")

    def _cancel_drag_item(self) -> None:
        if self.drag_item is not None:
            self.canvas.delete(self.drag_item)
            self.drag_item = None

    def _cancel_drag(self) -> None:
        self._cancel_drag_item()
        self.drag_start_pdf = None
        self.drag_start_canvas = None

    def _on_mousewheel(self, event: tk.Event) -> None:
        if event.state & 0x0004:
            direction = 1 if event.delta > 0 else -1
            values = [0.8, 1.0, 1.25, 1.6, 2.0, 2.5]
            current = min(range(len(values)), key=lambda index: abs(values[index] - self.zoom.get()))
            current = max(0, min(len(values) - 1, current + direction))
            self.zoom.set(values[current])
            self.zoom_box.set(f"{round(values[current] * 100)}%")
            self.render_page()
        else:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def select_color(self, color: str) -> None:
        self.current_color = color
        for button, (_, preset) in zip(self._color_buttons, PRESET_COLORS):
            button.configure(relief=tk.SUNKEN if preset == color else tk.RAISED)
        self._set_status(f"色を {color} に変更しました。次に追加する着色・寸法へ適用します。")

    def choose_color(self) -> None:
        _, color = colorchooser.askcolor(color=self.current_color, title="塗りつぶし色を選択")
        if color:
            self.select_color(color)

    def _zoom_changed(self, _event: tk.Event) -> None:
        value = self.zoom_box.get().rstrip("%")
        self.zoom.set(float(value) / 100)
        self.render_page()

    def previous_page(self) -> None:
        if self.document is not None and self.page_index > 0:
            self.page_index -= 1
            self.render_page(reset_view=True)
            self._show_page_selection_hint()

    def next_page(self) -> None:
        if self.document is not None and self.page_index < self.document.page_count - 1:
            self.page_index += 1
            self.render_page(reset_view=True)
            self._show_page_selection_hint()

    def _show_page_selection_hint(self, *, opened_name: str | None = None) -> None:
        if self.document is None:
            return
        page = self.document[self.page_index]
        prefix = f"{opened_name} を開きました。" if opened_name else ""
        if page.get_text("words"):
            self._set_status(
                f"{prefix} 寸法値をクリックすると、φ・＋・－・公差を含めて着色できます。"
            )
        else:
            self.mode.set("rect")
            self._set_status(
                f"{prefix} このページは画像PDFです。ドラッグで着色範囲を選択してください。"
            )

    def undo(self) -> None:
        if self.items:
            removed = self.items.pop()
            self.page_index = removed.page_index
            self.render_page()
            self._set_status("最後の編集を元に戻しました。")

    def clear_page(self) -> None:
        before = len(self.items)
        self.items = [item for item in self.items if item.page_index != self.page_index]
        if len(self.items) != before:
            self.render_page()
            self._set_status("このページの編集内容をすべて消しました。")

    def clear_all(self) -> None:
        if self.items:
            self.items.clear()
            self.render_page()
            self._set_status("すべての編集内容を消しました。")


def main() -> None:
    root = tk.Tk()
    app = DrawingAssistApp(root)
    if len(sys.argv) > 1:
        initial = Path(sys.argv[1])
        root.after(100, lambda: app.open_pdf(initial))
    root.mainloop()


if __name__ == "__main__":
    main()
