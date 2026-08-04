from __future__ import annotations

from pathlib import Path
import sys
import tempfile

import fitz


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.pdf_editor import (
    DimensionMark,
    ProcedureNoteMark,
    ReplacementMark,
    StampMark,
    export_pdf,
)
from drawing_assist.web_app import DrawingApi


def main() -> None:
    with tempfile.TemporaryDirectory(
        prefix="DrawingAssist-editable-items-",
        ignore_cleanup_errors=True,
    ) as directory:
        source = Path(directory, "source.pdf")
        output = Path(directory, "output.pdf")
        document = fitz.open()
        document.new_page(width=500, height=300)
        document.save(source)
        document.close()

        api = DrawingApi()
        api.document = fitz.open(source)
        api.source_path = source
        api.display_name = source.name
        try:
            state = api.apply_action(
                "procedure_note",
                {"x": 60, "y": 45, "select_existing": True},
                {
                    "procedure_note_type": "phase",
                    "procedure_note_text": "位置関係注意",
                    "procedure_note_size": 10,
                },
            )
            selection = state["editable_item_selection"]
            assert selection and selection["mode"] == "procedure_note"
            original = fitz.Rect(selection["rect"])

            state = api.apply_action(
                "procedure_note",
                {"x": original.x0 + 5, "y": original.y0 + 5, "select_existing": True},
                {},
            )
            assert len(api.items) == 1
            assert state["editable_item_selection"]["index"] == 0

            state = api.update_editable_item(
                {
                    "x0": original.x0 + 35,
                    "y0": original.y0 + 20,
                    "x1": original.x1 + 70,
                    "y1": original.y1 + 35,
                }
            )
            moved = fitz.Rect(state["editable_item_selection"]["rect"])
            note = api.items[0]
            assert isinstance(note, ProcedureNoteMark)
            assert moved.x0 > original.x0 and moved.y0 > original.y0
            assert note.font_size > 10

            state = api.apply_action(
                "quality_stamp",
                {"x": 320, "y": 120, "select_existing": True},
                {"stamp_name": "担当者", "stamp_date": "26.08.03", "stamp_size": 60},
            )
            stamp_selection = state["editable_item_selection"]
            assert stamp_selection and stamp_selection["mode"] == "quality_stamp"
            stamp_rect = fitz.Rect(stamp_selection["rect"])
            state = api.update_editable_item(
                {
                    "x0": stamp_rect.x0 + 10,
                    "y0": stamp_rect.y0 + 5,
                    "x1": stamp_rect.x1 + 30,
                    "y1": stamp_rect.y1 + 25,
                }
            )
            stamp = api.items[1]
            assert isinstance(stamp, StampMark)
            assert stamp.size > 60

            state = api.apply_action(
                "dimension",
                {"x0": 120, "y0": 220, "x1": 190, "y1": 185},
                {
                    "dimension_text": "25",
                    "dimension_auto_style": True,
                    "color": "#fff24d",
                },
            )
            dimension_selection = state["editable_item_selection"]
            assert dimension_selection and dimension_selection["mode"] == "dimension"
            assert dimension_selection["target_movable"]
            dimension_rect = fitz.Rect(dimension_selection["rect"])
            original_target = tuple(dimension_selection["target"])
            state = api.update_editable_item(
                {
                    "x0": dimension_rect.x0 + 24,
                    "y0": dimension_rect.y0 - 18,
                    "x1": dimension_rect.x1 + 24,
                    "y1": dimension_rect.y1 - 18,
                }
            )
            dimension = api.items[2]
            assert isinstance(dimension, DimensionMark)
            assert dimension.target == original_target
            assert dimension.label[0] > dimension_rect.x0
            state = api.update_editable_item(
                {
                    "x0": state["editable_item_selection"]["rect"][0],
                    "y0": state["editable_item_selection"]["rect"][1],
                    "x1": state["editable_item_selection"]["rect"][2],
                    "y1": state["editable_item_selection"]["rect"][3],
                    "target_x": original_target[0] + 15,
                    "target_y": original_target[1] + 10,
                }
            )
            dimension = api.items[2]
            assert dimension.target != original_target
            resized_rect = fitz.Rect(state["editable_item_selection"]["rect"])
            state = api.update_editable_item(
                {
                    "x0": resized_rect.x0,
                    "y0": resized_rect.y0,
                    "x1": resized_rect.x0 + resized_rect.width * 1.5,
                    "y1": resized_rect.y0 + resized_rect.height * 1.5,
                }
            )
            assert api.items[2].font_size > dimension.font_size

            state = api.apply_action(
                "dimension",
                {"x0": 360, "y0": 210, "x1": 360, "y1": 210},
                {
                    "dimension_text": "φ12",
                    "dimension_auto_style": True,
                    "dimension_show_leader": False,
                    "color": "#fff24d",
                },
            )
            text_only = api.items[3]
            assert isinstance(text_only, DimensionMark)
            assert not text_only.show_leader
            assert not state["editable_item_selection"]["target_movable"]

            replacement = ReplacementMark(
                page_index=0,
                rect=(245, 170, 280, 187),
                direction=(1.0, 0.0),
                value="16",
                upper_tolerance="+0.1",
                lower_tolerance="0",
                font_size=9,
                tolerance_font_size=7,
                origin=(248, 184),
            )
            api.items.append(replacement)
            api.editable_item_index = len(api.items) - 1
            replacement_selection = api._state()["editable_item_selection"]
            assert replacement_selection["mode"] == "replace"
            replacement_rect = fitz.Rect(replacement_selection["rect"])
            state = api.update_editable_item(
                {
                    "x0": replacement_rect.x0 + 20,
                    "y0": replacement_rect.y0 + 10,
                    "x1": replacement_rect.x0 + 20 + replacement_rect.width * 1.4,
                    "y1": replacement_rect.y0 + 10 + replacement_rect.height * 1.4,
                }
            )
            edited_replacement = api.items[-1]
            assert isinstance(edited_replacement, ReplacementMark)
            assert edited_replacement.origin != replacement.origin
            assert edited_replacement.font_size > replacement.font_size
            assert state["editable_item_selection"]["mode"] == "replace"

            export_pdf(source, output, api.items)
            rendered = fitz.open(output)
            try:
                assert "位置関係注意" in rendered[0].get_text()
                assert "品質保証" in rendered[0].get_text()
            finally:
                rendered.close()
        finally:
            api.document.close()
            api.upload_directory.cleanup()

    print("editable stamp, notes, arrow/text-only/replaced dimensions and resize: OK")


if __name__ == "__main__":
    main()
