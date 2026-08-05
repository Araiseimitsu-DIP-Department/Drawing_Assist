from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import fitz
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drawing_assist.general_tolerance import (
    GeneralToleranceCandidate,
    _run_windows_ocr,
    angle_tolerance,
    detect_general_tolerance_candidates,
    extract_drawing_tolerance_notes,
    jis_b_0405_tolerance,
    pisco_tolerance,
    toggle_candidate,
)
from drawing_assist.pdf_editor import (
    DimensionMark,
    DimensionMarkingBatch,
    DimensionMarkingEntry,
    GeneralToleranceBatchMark,
    ProcedureNoteMark,
    ReplacementMark,
    ToleranceAddition,
    WorkRegionMark,
    dimension_label_rect,
    export_pdf,
)
from drawing_assist.web_app import (
    _detect_dimension_markings,
    _detect_scanned_dimension_markings,
)


def _scan_and_apply_markings(api) -> dict:
    scan = api.scan_dimension_markings()
    assert scan["ok"], scan
    return api.apply_dimension_markings()


def _verify_tables() -> None:
    assert jis_b_0405_tolerance(0.03, "m") == 0.03
    assert jis_b_0405_tolerance(0.05, "m", "chamfer") == 0.05
    assert jis_b_0405_tolerance(0.099, "m", "radius") == 0.099
    assert jis_b_0405_tolerance(0.1, "m") == 0.1
    assert jis_b_0405_tolerance(0.1001, "m", "chamfer") == 0.1
    assert jis_b_0405_tolerance(0.49, "m") == 0.1
    assert jis_b_0405_tolerance(3, "f") == 0.05
    assert jis_b_0405_tolerance(3.01, "f") == 0.05
    assert jis_b_0405_tolerance(6.01, "m") == 0.2
    assert jis_b_0405_tolerance(2000.01, "m") == 2.0
    assert jis_b_0405_tolerance(3, "m", "chamfer") == 0.2
    assert jis_b_0405_tolerance(0.49, "m", "chamfer") == 0.1
    assert jis_b_0405_tolerance(0.5, "m", "chamfer") == 0.2
    assert jis_b_0405_tolerance(3.01, "f", "radius") == 0.5

    assert pisco_tolerance(0.09, "linear") is None
    assert pisco_tolerance(0.5, "diameter") == 0.025
    assert pisco_tolerance(0.51, "diameter") == 0.05
    assert pisco_tolerance(6.01, "linear") == 0.2
    assert pisco_tolerance(0.15, "chamfer") == 0.05
    assert pisco_tolerance(0.05, "chamfer") == 0.05
    assert pisco_tolerance(0.16, "radius") == 0.1
    assert angle_tolerance(10) == (1.0, "±1°")
    assert angle_tolerance(10.01) == (0.5, "±30′")
    assert angle_tolerance(50.01) == (20 / 60, "±20′")
    assert angle_tolerance(120.01) == (10 / 60, "±10′")
    assert angle_tolerance(400.01) == (5 / 60, "±5′")
    assert angle_tolerance(10, standard="pisco") == (1.0, "±1°")

    notes = extract_drawing_tolerance_notes(
        """注記)
        1. 指示無き角部は、C0.1以下とする
        2. 指示無き隅のRは、0.1以下とする
        3. 指示無き角度公差は、±1°とする
        """
    )
    assert notes.angle_tolerance == (1.0, "±1°")
    assert notes.unindicated_chamfer_maximum == 0.1
    assert notes.unindicated_radius_maximum == 0.1
    assert notes.chamfer_tolerance is None
    assert notes.radius_tolerance is None

    explicit_notes = extract_drawing_tolerance_notes(
        "指示無きC/R公差は±0.05とする"
    )
    assert explicit_notes.chamfer_tolerance == 0.05
    assert explicit_notes.radius_tolerance == 0.05


def _verify_toggle() -> None:
    candidate = GeneralToleranceCandidate(
        page_index=0,
        rect=(10, 10, 30, 20),
        direction=(1, 0),
        source_text="14.7",
        nominal_value=14.7,
        kind="linear",
        tolerance=0.2,
        tolerance_text="±0.2",
    )
    toggled = toggle_candidate([candidate], fitz.Point(20, 15))
    assert not toggled[0].selected
    restored = toggle_candidate(toggled, fitz.Point(20, 15))
    assert restored[0].selected

    manual = GeneralToleranceCandidate(
        page_index=0,
        rect=(10, 10, 30, 20),
        direction=(1, 0),
        source_text="C0.1",
        nominal_value=0.1,
        kind="chamfer",
        tolerance=0.0,
        tolerance_text="",
        selected=False,
        manual_required=True,
    )
    assert not toggle_candidate([manual], fitz.Point(20, 15))[0].selected


def _verify_rendering() -> None:
    with tempfile.TemporaryDirectory(
        prefix="DrawingAssist-general-tolerance-",
        ignore_cleanup_errors=True,
    ) as directory:
        source = Path(directory, "source.pdf")
        output = Path(directory, "output.pdf")
        document = fitz.open()
        page = document.new_page(width=260, height=160)
        page.insert_text((60, 80), "14.7", fontsize=10)
        document.save(source)
        document.close()

        tolerance_batch = GeneralToleranceBatchMark(
            0,
            (
                ToleranceAddition((86, 80), (1, 0), "±0.2", 8),
                ToleranceAddition((180, 105), (0, -1), "±0.1", 7),
            ),
        )
        marking_batch = DimensionMarkingBatch(
            0,
            (
                DimensionMarkingEntry((58, 68, 88, 83), "#ff33cc"),
                DimensionMarkingEntry((215, 68, 240, 83), "#ffff00"),
            ),
        )
        work_region = WorkRegionMark(
            0,
            (((35, 45), (190, 45), (190, 125), (35, 125)),),
        )
        procedure_note = ProcedureNoteMark(
            0,
            (20, 28),
            "phase",
            "位置関係注意",
            9,
        )
        measurement = ProcedureNoteMark(
            0,
            (220, 28),
            "measurement",
            "M",
            9,
        )
        export_pdf(
            source,
            output,
            [
                tolerance_batch,
                marking_batch,
                procedure_note,
                measurement,
                work_region,
            ],
        )
        rendered = fitz.open(output)
        assert rendered.page_count == 1
        # The ± sign is intentionally drawn as vector strokes because some
        # rotated Windows PDFs retain the character but render no visible
        # glyph. The numeric suffix remains searchable text.
        assert "0.2" in rendered[0].get_text()
        rendered_text = rendered[0].get_text()
        assert "位置関係注意" in rendered_text
        assert "M" in rendered_text
        white_fill_paths = [
            drawing
            for drawing in rendered[0].get_drawings()
            if drawing.get("fill") == (1.0, 1.0, 1.0)
        ]
        assert not white_fill_paths, (
            "General-tolerance text must not blank dimension lines with "
            "a white rectangle."
        )
        annotations = [
            (annotation.type, annotation.colors, annotation.border)
            for annotation in (rendered[0].annots() or [])
        ]
        assert len(annotations) == 4
        # Product fill is rendered below dimensions even though the user
        # confirms it last. The middle annotation is the intentional white
        # separation stroke around the final dimension marker.
        assert annotations[0][0][1] == "Polygon"
        assert annotations[1][1].get("stroke") == [1.0, 1.0, 1.0]
        assert annotations[1][2].get("width", 0) >= 1.6
        assert annotations[2][0][1] == "Square"
        # The marker outside the product fill has no unnecessary white frame.
        assert annotations[3][1].get("stroke") != [1.0, 1.0, 1.0]
        rendered.close()


def _verify_separate_ui_flow() -> None:
    html = (ROOT / "src" / "drawing_assist" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "src" / "drawing_assist" / "web" / "app.js").read_text(
        encoding="utf-8"
    )
    tolerance_menu = html.index('data-mode="general_tolerance"')
    marking_menu = html.index('data-mode="word"')
    assert tolerance_menu < marking_menu
    assert "1. 公差をまとめて入れる" in html
    assert "2. 品質保証・加工図の印" in html
    assert "3. 寸法・公差を色分けする" in html
    marking_panel = html.index('class="setting-group hidden marking-flow-panel"')
    marking_button = html.index('id="applyDimensionMarkingsButton"')
    tolerance_panel = html.index('data-for="general_tolerance"')
    assert marking_panel < marking_button < tolerance_panel
    assert "寸法値と追加公差を基準色で塗る" in html
    assert "公差レンジ0.03以内／角度1°以内" in html
    assert "測定具記号と測定順番号" in html
    assert 'selectTool("general_tolerance")' in script
    assert html.index('<option value="jis_b_0405">') < html.index(
        '<option value="pisco">'
    )
    assert 'id="generalToleranceAngleLength"' in html
    for value in ("10", "50", "120", "400", "401"):
        assert f'<option value="{value}">' in html
    assert "general_tolerance_angle_length" in script


def _verify_angle_range_wiring() -> None:
    from unittest.mock import patch

    from drawing_assist.web_app import DrawingApi

    api = DrawingApi()
    api.document = fitz.open()
    api.document.new_page()
    try:
        with patch(
            "drawing_assist.web_app.detect_general_tolerance_candidates",
            return_value=[],
        ) as detector:
            api.scan_general_tolerances(
                {
                    "general_tolerance_standard": "jis_b_0405",
                    "general_tolerance_grade": "m",
                    "general_tolerance_angle_length": 120,
                }
            )
        assert api.general_tolerance_angle_length == 120
        assert detector.call_args.kwargs[
            "angle_shorter_side_length"
        ] == 120
    finally:
        api.document.close()
        api.upload_directory.cleanup()


def _verify_current_ui_flow() -> None:
    html = (ROOT / "src" / "drawing_assist" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "src" / "drawing_assist" / "web" / "app.js").read_text(
        encoding="utf-8"
    )
    styles = (ROOT / "src" / "drawing_assist" / "web" / "styles.css").read_text(
        encoding="utf-8"
    )
    assert html.index('data-mode="general_tolerance"') < html.index(
        'data-mode="word"'
    )
    for text in (
        "① 公差を入れる",
        "③ 寸法を色分け",
        "④ 製品を塗る",
        "その他の機能",
        "二重線で消す",
        "寸法と矢印を追加",
        "必要な寸法を書き直す",
        "印・必要な注記を入れる",
        "測定具・測定順を入れる",
        "一括反映",
        "scanDimensionMarkingsButton",
        "detect-button",
        "厳しい公差（0.03以内・角度1°以内）",
        "角度公差の設定",
        "operation-steps",
    ):
        assert text in html
    for marker in (
        'class="dimension-fix-tools"',
        'id="dimensionFixStatus"',
        'data-mode="procedure_note"',
        'data-mode="measurement"',
        'id="procedureNoteType"',
        'id="measurementInstrument"',
    ):
        assert marker in html
    assert html.index('<option value="jis_b_0405">') < html.index(
        '<option value="pisco">'
    )
    assert 'selectTool("general_tolerance")' in script
    assert "general_tolerance_angle_length" in script
    assert "updateWorkflowNavigation" in script
    assert "added_dimension_count" in script
    assert 'max="500"' in html
    assert "zoomWithMouseWheel" in script
    assert 'addEventListener("wheel"' in script
    assert "clamp(220px, 14vw, 250px)" in styles
    assert ".flow-tool { padding-right: 34px; }" in styles
    assert "grid-template-columns: 196px minmax(280px, 1fr) 250px" in styles
    assert "@media (max-height: 760px)" in styles


def _verify_edited_dimensions_join_final_marking() -> None:
    from drawing_assist.web_app import DrawingApi

    api = DrawingApi()
    api.document = fitz.open()
    page = api.document.new_page(width=260, height=180)
    page.insert_text((25, 35), "10", fontsize=10)
    source_rect = page.search_for("10")[0]
    candidate = GeneralToleranceCandidate(
        page_index=0,
        rect=tuple(source_rect),
        direction=(1.0, 0.0),
        source_text="10",
        nominal_value=10.0,
        kind="linear",
        tolerance=0.1,
        tolerance_text="±0.1",
    )
    addition = api._tolerance_addition(candidate)
    added = DimensionMark(
        page_index=0,
        target=(65, 125),
        label=(75, 110),
        text="φ10±0.01",
        opacity=0.0,
        font_size=10,
    )
    replacement = ReplacementMark(
        page_index=0,
        rect=(140, 85, 175, 105),
        direction=(1.0, 0.0),
        value="12",
        upper_tolerance="+0.1",
        lower_tolerance="-0.1",
        font_size=10,
        tolerance_font_size=8,
        origin=(145, 100),
    )
    api.last_general_tolerance_batch = [candidate]
    api.last_general_tolerance_additions = [addition]
    api.items.extend((added, replacement))
    try:
        _scan_and_apply_markings(api)
        batch = api.items[-1]
        assert isinstance(batch, DimensionMarkingBatch)
        added_rect = dimension_label_rect(added)
        assert any(
            entry.color == "#ff33cc"
            and not (fitz.Rect(entry.rect) & added_rect).is_empty
            for entry in batch.entries
        ), [(entry.color, entry.rect) for entry in batch.entries]
        replacement_rect = fitz.Rect(140, 80, 205, 110)
        assert any(
            entry.color == "#ffff00"
            and not (fitz.Rect(entry.rect) & replacement_rect).is_empty
            for entry in batch.entries
        ), [(entry.color, entry.rect) for entry in batch.entries]

        api._invalidate_dimension_markings()
        assert not any(
            isinstance(item, DimensionMarkingBatch) for item in api.items
        )
        assert not api.last_general_tolerance_marked
    finally:
        api.document.close()
        api.upload_directory.cleanup()


def _verify_collision_aware_layout() -> None:
    from drawing_assist.web_app import DrawingApi

    api = DrawingApi()
    api.document = fitz.open()
    page = api.document.new_page(width=260, height=160)
    page.insert_text((90, 82), "10", fontsize=10)
    page.draw_line((45, 82), (205, 82), width=0.5)
    source_rect = page.search_for("10")[0]
    candidate = GeneralToleranceCandidate(
        page_index=0,
        rect=tuple(source_rect),
        direction=(1.0, 0.0),
        source_text="10",
        nominal_value=10.0,
        kind="linear",
        tolerance=0.1,
        tolerance_text="±0.1",
    )
    try:
        addition = api._tolerance_addition(candidate)
        # A crossing dimension line causes a nearby parallel placement rather
        # than allowing the added tolerance to cover the line or nominal.
        assert abs(addition.origin[1] - source_rect.y1) > source_rect.height * 0.35
        addition_rect = api._full_tolerance_addition_rect(addition)
        assert not addition_rect.contains(fitz.Point(120, 82))
        assert addition.font_size >= 5.0
        assert addition.font_size >= source_rect.height * 0.65
    finally:
        api.document.close()
        api.upload_directory.cleanup()


def _verify_marking_includes_added_tolerance() -> None:
    from drawing_assist.web_app import DrawingApi

    api = DrawingApi()
    document = fitz.open()
    page = document.new_page(width=240, height=140)
    page.insert_text((60, 80), "14.7", fontsize=10)
    page.insert_text((120, 80), "(9.8)", fontsize=10)
    reference_rect = page.search_for("(9.8)")[0]
    api.document = document
    candidate = GeneralToleranceCandidate(
        page_index=0,
        rect=(60, 69, 79, 82),
        direction=(1, 0),
        source_text="14.7",
        nominal_value=14.7,
        kind="linear",
        tolerance=0.2,
        tolerance_text="±0.2",
    )
    api.last_general_tolerance_batch = [candidate]
    try:
        state = _scan_and_apply_markings(api)
        assert state["ok"]
        batch = api.items[-1]
        assert isinstance(batch, DimensionMarkingBatch)
        assert len(batch.entries) == 1
        joined_rect = fitz.Rect(batch.entries[0].rect)
        assert not (joined_rect & fitz.Rect(candidate.rect)).is_empty
        assert joined_rect.x1 > fitz.Rect(candidate.rect).x1
        assert (joined_rect & reference_rect).is_empty
    finally:
        document.close()
        api.upload_directory.cleanup()


def _verify_general_tolerance_drag_updates_marking() -> None:
    from drawing_assist.web_app import DrawingApi

    api = DrawingApi()
    api.document = fitz.open()
    api.document.new_page(width=260, height=160)
    api.source_path = Path("drag-test.pdf")
    candidate = GeneralToleranceCandidate(
        page_index=0,
        rect=(60, 65, 82, 80),
        direction=(1.0, 0.0),
        source_text="14.7",
        nominal_value=14.7,
        kind="linear",
        tolerance=0.1,
        tolerance_text="±0.1",
        quad=((60, 65), (82, 65), (82, 80), (60, 80)),
    )
    addition = ToleranceAddition((83, 78), (1.0, 0.0), "±0.1", 7.0)
    batch = GeneralToleranceBatchMark(0, (addition,))
    api.items = [batch]
    api.last_general_tolerance_batch = [candidate]
    api.last_general_tolerance_additions = [addition]
    try:
        old_rect = api._full_tolerance_addition_rect(addition)
        api.select_general_tolerance_addition(
            {"x": (old_rect.x0 + old_rect.x1) / 2, "y": (old_rect.y0 + old_rect.y1) / 2}
        )
        assert api.editable_tolerance_index == 0
        api.move_general_tolerance_addition(
            {"x0": old_rect.x0 + 18, "y0": old_rect.y0 + 12}
        )
        moved = api.last_general_tolerance_additions[0]
        assert abs(moved.origin[0] - addition.origin[0] - 18) < 0.01
        assert abs(moved.origin[1] - addition.origin[1] - 12) < 0.01
        assert api.items[0].additions[0] == moved
        _scan_and_apply_markings(api)
        marking = api.items[-1]
        assert isinstance(marking, DimensionMarkingBatch)
        moved_rect = fitz.Rect(api._added_tolerance_mark_rect(moved))
        assert any(
            not (fitz.Rect(entry.rect) & moved_rect).is_empty
            for entry in marking.entries
        )
    finally:
        api.document.close()
        api.upload_directory.cleanup()


def _verify_tolerance_resize_shrink() -> None:
    from drawing_assist.web_app import DrawingApi

    api = DrawingApi()
    api.document = fitz.open()
    api.document.new_page(width=260, height=160)
    addition = ToleranceAddition((80, 78), (1.0, 0.0), "±0.1", 10.0)
    batch = GeneralToleranceBatchMark(0, (addition,))
    api.items = [batch]
    api.last_general_tolerance_additions = [addition]
    api.editable_item_index = 0
    api.editable_tolerance_index = 0
    try:
        old_rect = api._full_tolerance_addition_rect(addition)
        enlarged = fitz.Rect(
            old_rect.x0,
            old_rect.y0,
            old_rect.x0 + old_rect.width * 1.6,
            old_rect.y0 + old_rect.height * 1.6,
        )
        api.move_general_tolerance_addition(
            {
                "x0": enlarged.x0,
                "y0": enlarged.y0,
                "x1": enlarged.x1,
                "y1": enlarged.y1,
            }
        )
        enlarged_size = api.last_general_tolerance_additions[0].font_size
        assert enlarged_size > addition.font_size

        enlarged_rect = api._full_tolerance_addition_rect(
            api.last_general_tolerance_additions[0]
        )
        shrunk = fitz.Rect(
            enlarged_rect.x0,
            enlarged_rect.y0,
            enlarged_rect.x0 + enlarged_rect.width * 0.55,
            enlarged_rect.y0 + enlarged_rect.height * 0.55,
        )
        api.move_general_tolerance_addition(
            {
                "x0": shrunk.x0,
                "y0": shrunk.y0,
                "x1": shrunk.x1,
                "y1": shrunk.y1,
            }
        )
        shrunk_size = api.last_general_tolerance_additions[0].font_size
        assert shrunk_size < enlarged_size
        assert shrunk_size >= 4.0
    finally:
        api.document.close()
        api.upload_directory.cleanup()


def _verify_hidden_ocr_window() -> None:
    completed = __import__("subprocess").CompletedProcess(
        args=[],
        returncode=0,
        stdout=b'{"lines":[]}',
        stderr=b"",
    )
    with patch("subprocess.run", return_value=completed) as mocked:
        _run_windows_ocr(Path("dummy.png"), Path("ocr.ps1"))
    options = mocked.call_args.kwargs
    if hasattr(__import__("subprocess"), "CREATE_NO_WINDOW"):
        assert options.get("creationflags")
        assert options.get("startupinfo") is not None


def _verify_native_drawing_detection() -> None:
    source = ROOT / "tmp" / "outline-clean-source.pdf"
    if not source.is_file():
        return
    document = fitz.open(source)
    try:
        candidates = detect_general_tolerance_candidates(
            document[0],
            0,
            standard="jis_b_0405",
            grade="m",
            ocr_script=ROOT / "src" / "drawing_assist" / "windows_ocr.ps1",
        )
    finally:
        document.close()
    automatic_values = {
        candidate.nominal_value
        for candidate in candidates
        if candidate.kind != "angle" and not candidate.manual_required
    }
    assert {
        0.12, 2.7, 3.0, 3.5, 4.8, 6.9, 15.5, 16.0, 16.1, 17.1, 18.0
    }.issubset(automatic_values)
    assert {0.05, 0.15, 0.2, 0.3}.issubset(automatic_values)
    assert not [
        candidate for candidate in candidates if candidate.manual_required
    ]
    # Existing tolerances and reference dimensions in parentheses.
    assert automatic_values.isdisjoint(
        {0.61, 0.95, 1.0, 1.37, 1.5, 2.9, 2.95, 3.9, 9.8,
         14.7, 15.4, 18.9, 19.4}
    )
    angle_candidates = [
        candidate for candidate in candidates if candidate.kind == "angle"
    ]
    assert {candidate.nominal_value for candidate in angle_candidates} == {
        20.0,
        30.0,
        45.0,
        60.0,
    }
    assert all(
        candidate.tolerance_text == "±1°" and candidate.quad
        for candidate in angle_candidates
    )
    assert any(
        abs(candidate.direction[0]) > 0.1
        and abs(candidate.direction[1]) > 0.1
        for candidate in angle_candidates
    )

    # The real drawing writes ``φ15.5（溝）`` as one vertical text line. Move
    # that descriptor after the inserted tolerance and use the requested
    # width label: ``φ15.5±0.2(幅)``.
    from drawing_assist.web_app import DrawingApi

    api = DrawingApi()
    api.document = fitz.open(source)
    try:
        original_markings = _detect_dimension_markings(api.document[0])
        diameter_marking = next(
            marking
            for marking in original_markings
            if marking.kind == "diameter"
            and abs(marking.nominal_value - 18.9) < 1e-9
        )
        # The source CAD maps φ to a blank glyph.  Its marker must still extend
        # past the numeric glyphs and cover the visible diameter sign.
        assert fitz.Rect(diameter_marking.rect).height > 24
        assert any(
            abs(marking.nominal_value - 1.0) < 1e-9
            and abs((marking.tolerance_range or 0) - 0.05) < 1e-9
            for marking in original_markings
        )
        # The lower 60-degree callout already has an explicit tolerance.  The
        # upper one is covered later through its general-tolerance addition.
        assert sum(
            marking.kind == "angle"
            and abs(marking.nominal_value - 60.0) < 1e-9
            for marking in original_markings
        ) == 1
        assert any(
            marking.kind == "limit"
            and marking.source_text == "C0.05以下"
            for marking in original_markings
        )
        groove_candidate = next(
            candidate
            for candidate in candidates
            if abs(candidate.nominal_value - 15.5) < 1e-9
        )
        addition = api._tolerance_addition(groove_candidate)
        assert addition.suffix_text == "(幅)"
        assert addition.suffix_rect is not None
        assert addition.suffix_font_size is not None
        assert addition.suffix_font_size >= addition.font_size
        assert addition.origin[1] < groove_candidate.rect[1]
        assert addition.origin[1] > addition.suffix_rect[1]

        # Retain the visually detected φ when joining a newly added general
        # tolerance to a CAD nominal whose diameter glyph has no Unicode map.
        diameter_candidate = next(
            candidate
            for candidate in candidates
            if candidate.kind == "diameter"
            and abs(candidate.nominal_value - 18.0) < 1e-9
        )
        diameter_addition = api._tolerance_addition(diameter_candidate)
        api.last_general_tolerance_batch = [diameter_candidate]
        api.last_general_tolerance_additions = [diameter_addition]
        api.last_general_tolerance_marked = False
        _scan_and_apply_markings(api)
        diameter_batch = api.items[-1]
        assert isinstance(diameter_batch, DimensionMarkingBatch)
        diameter_entry = next(
            entry
            for entry in diameter_batch.entries
            if not (
                fitz.Rect(entry.rect) & fitz.Rect(diameter_candidate.rect)
            ).is_empty
        )
        assert fitz.Rect(diameter_entry.rect).height > 32

        face_candidate = next(
            candidate
            for candidate in candidates
            if abs(candidate.nominal_value - 16.0) < 1e-9
        )
        face_addition = api._tolerance_addition(face_candidate)
        assert face_addition.suffix_text == "(二面幅)"
        assert face_addition.suffix_rect is not None
        assert face_addition.suffix_font_size is not None
        assert face_addition.suffix_font_size >= face_addition.font_size
        api.last_general_tolerance_batch = [face_candidate]
        api.last_general_tolerance_additions = [face_addition]
        api.last_general_tolerance_marked = False
        _scan_and_apply_markings(api)
        descriptor_batch = api.items[-1]
        assert isinstance(descriptor_batch, DimensionMarkingBatch)
        descriptor_rect = fitz.Rect(face_addition.suffix_rect)
        assert any(
            not (fitz.Rect(entry.rect) & descriptor_rect).is_empty
            for entry in descriptor_batch.entries
        )

        diagonal_angle = next(
            candidate
            for candidate in angle_candidates
            if abs(candidate.nominal_value - 20.0) < 1e-9
        )
        angle_addition = api._tolerance_addition(diagonal_angle)
        assert angle_addition.text == "±1°"
        angle_axis = fitz.Point(diagonal_angle.direction)
        source_along_max = max(
            fitz.Point(point).x * angle_axis.x
            + fitz.Point(point).y * angle_axis.y
            for point in diagonal_angle.quad or ()
        )
        addition_along = (
            angle_addition.origin[0] * angle_axis.x
            + angle_addition.origin[1] * angle_axis.y
        )
        assert 0 <= addition_along - source_along_max < 0.5
        api.last_general_tolerance_batch = [diagonal_angle]
        api.last_general_tolerance_additions = [angle_addition]
        api.last_general_tolerance_marked = False
        marking_state = _scan_and_apply_markings(api)
        assert marking_state["ok"]
        marking_batch = api.items[-1]
        assert isinstance(marking_batch, DimensionMarkingBatch)
        def follows_angle(entry: DimensionMarkingEntry) -> bool:
            if not entry.quad:
                return False
            edge = fitz.Point(entry.quad[1]) - fitz.Point(entry.quad[0])
            edge_length = max(1e-9, abs(edge))
            return (
                abs(edge.x / edge_length - diagonal_angle.direction[0]) < 0.03
                and abs(edge.y / edge_length - diagonal_angle.direction[1]) < 0.03
            )

        angle_entries = [
            entry
            for entry in marking_batch.entries
            if follows_angle(entry)
            and (
                not (fitz.Rect(entry.rect) & fitz.Rect(diagonal_angle.rect)).is_empty
                or not (
                    fitz.Rect(entry.rect)
                    & fitz.Rect(api._added_tolerance_mark_rect(angle_addition))
                ).is_empty
            )
        ]
        assert len(angle_entries) == 1, [
            (entry.rect, entry.quad)
            for entry in marking_batch.entries
        ]
        assert all(entry.quad for entry in angle_entries)
        for entry in angle_entries:
            edge = fitz.Point(entry.quad[1]) - fitz.Point(entry.quad[0])
            edge_length = max(1e-9, abs(edge))
            assert abs(edge.x / edge_length - diagonal_angle.direction[0]) < 0.03
            assert abs(edge.y / edge_length - diagonal_angle.direction[1]) < 0.03

        # The procedure sheet defines angular tolerance by total range:
        # 1 degree or less is pink, larger is yellow.
        small_angle = GeneralToleranceCandidate(
            page_index=diagonal_angle.page_index,
            rect=diagonal_angle.rect,
            direction=diagonal_angle.direction,
            source_text=diagonal_angle.source_text,
            nominal_value=diagonal_angle.nominal_value,
            kind="angle",
            tolerance=0.5,
            tolerance_text="±30′",
            quad=diagonal_angle.quad,
        )
        small_addition = api._tolerance_addition(small_angle)
        api.last_general_tolerance_batch = [small_angle]
        api.last_general_tolerance_additions = [small_addition]
        api.last_general_tolerance_marked = False
        _scan_and_apply_markings(api)
        small_marking_batch = api.items[-1]
        assert isinstance(small_marking_batch, DimensionMarkingBatch)
        small_angle_entries = [
            entry
            for entry in small_marking_batch.entries
            if follows_angle(entry)
            and (
                not (fitz.Rect(entry.rect) & fitz.Rect(small_angle.rect)).is_empty
                or not (
                    fitz.Rect(entry.rect)
                    & fitz.Rect(api._added_tolerance_mark_rect(small_addition))
                ).is_empty
            )
        ]
        assert small_angle_entries
        assert {entry.color for entry in small_angle_entries} == {"#ff33cc"}
    finally:
        api.document.close()
        api.upload_directory.cleanup()


def _verify_scanned_drawing_detection() -> None:
    font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 48)
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)

    def draw_dimension(y: int, text: str) -> None:
        draw.line((120, y, 1100, y), fill="black", width=3)
        box = draw.textbbox((0, 0), text, font=font)
        width = box[2] - box[0]
        draw.rectangle(
            (600 - width / 2 - 12, y - 42, 600 + width / 2 + 12, y + 14),
            fill="white",
        )
        draw.text((600 - width / 2, y - 40), text, font=font, fill="black")

    draw_dimension(180, "14.7")
    draw_dimension(360, "3.9 +0.1")
    draw_dimension(540, "(9.8)")
    # Geometric-tolerance value: it is a control-frame cell, not a dimension.
    draw.rectangle((1160, 215, 1510, 305), outline="black", width=4)
    draw.line((1300, 215, 1300, 305), fill="black", width=4)
    draw.polygon(
        ((1190, 260), (1225, 232), (1270, 232), (1235, 260)),
        outline="black",
    )
    draw.text((1330, 225), "0.03", font=font, fill="black")
    # Horizontal C/R text with diagonal leaders, matching scanned detail views.
    draw.text((220, 660), "C0.2", font=font, fill="black")
    draw.line((350, 710, 430, 790), fill="black", width=4)
    draw.text((930, 660), "C0.2", font=font, fill="black")
    draw.line((1060, 710, 1140, 630), fill="black", width=4)
    # A bare diagonal stroke must never become a phantom C/R candidate.
    draw.line((1350, 620, 1450, 720), fill="black", width=5)
    with tempfile.TemporaryDirectory(
        prefix="DrawingAssist-scanned-tolerance-",
        ignore_cleanup_errors=True,
    ) as directory:
        image_path = Path(directory, "drawing.png")
        source_path = Path(directory, "drawing.pdf")
        image.save(image_path)
        document = fitz.open()
        page = document.new_page(width=800, height=450)
        page.insert_image(page.rect, filename=str(image_path))
        document.save(source_path)
        document.close()
        document = fitz.open(source_path)
        try:
            candidates = detect_general_tolerance_candidates(
                document[0],
                0,
                standard="jis_b_0405",
                grade="m",
                ocr_script=ROOT / "src" / "drawing_assist" / "windows_ocr.ps1",
            )
            markings = _detect_scanned_dimension_markings(
                document[0],
                ROOT / "src" / "drawing_assist" / "windows_ocr.ps1",
            )
        finally:
            document.close()
    values = [candidate.nominal_value for candidate in candidates]
    assert values.count(0.2) == 2, values
    assert values.count(0.03) == 0, values
    assert values.count(14.7) == 1, values
    detected_rect = fitz.Rect(
        next(candidate.rect for candidate in candidates if candidate.nominal_value == 14.7)
    )
    assert abs((detected_rect.x0 + detected_rect.x1) / 2 - 300) < 14
    assert abs((detected_rect.y0 + detected_rect.y1) / 2 - 86) < 14
    explicit_markings = [
        marking for marking in markings if marking.tolerance_range is not None
    ]
    assert any(
        abs(marking.nominal_value - 3.9) < 1e-9
        for marking in explicit_markings
    ), [(marking.source_text, marking.nominal_value) for marking in markings]
    assert not any(
        abs(marking.nominal_value - 0.03) < 1e-9
        for marking in markings
    )


def _verify_applied_tolerance_removal() -> None:
    from drawing_assist.web_app import DrawingApi

    api = DrawingApi()
    api.document = fitz.open()
    api.document.new_page(width=320, height=160)
    api.source_path = Path("remove-test.pdf")
    candidates = [
        GeneralToleranceCandidate(
            page_index=0,
            rect=(40, 65, 62, 80),
            direction=(1.0, 0.0),
            source_text="10",
            nominal_value=10.0,
            kind="linear",
            tolerance=0.1,
            tolerance_text="±0.1",
        ),
        GeneralToleranceCandidate(
            page_index=0,
            rect=(120, 65, 142, 80),
            direction=(1.0, 0.0),
            source_text="20",
            nominal_value=20.0,
            kind="linear",
            tolerance=0.1,
            tolerance_text="±0.1",
        ),
    ]
    additions = (
        ToleranceAddition((63, 78), (1.0, 0.0), "±0.1", 7.0),
        ToleranceAddition((143, 78), (1.0, 0.0), "±0.1", 7.0),
    )
    api.items = [GeneralToleranceBatchMark(0, additions)]
    api.last_general_tolerance_batch = list(candidates)
    api.last_general_tolerance_additions = list(additions)
    try:
        state = api.remove_applied_general_tolerance({"x": 51, "y": 72})
        assert state["ok"]
        assert len(api.last_general_tolerance_batch) == 1
        assert api.last_general_tolerance_batch[0].nominal_value == 20.0
        state = api.remove_applied_general_tolerance({"x": 131, "y": 72})
        assert state["ok"]
        assert len(api.last_general_tolerance_batch) == 0
        assert not api.items
    finally:
        api.document.close()
        api.upload_directory.cleanup()


def _verify_dimension_marking_removal() -> None:
    from drawing_assist.web_app import DrawingApi

    api = DrawingApi()
    api.document = fitz.open()
    api.document.new_page(width=320, height=160)
    api.source_path = Path("marking-remove-test.pdf")
    entries = (
        DimensionMarkingEntry(
            rect=(40, 65, 90, 82),
            color="#f472b6",
            quad=((40, 65), (90, 65), (90, 82), (40, 82)),
        ),
        DimensionMarkingEntry(
            rect=(120, 65, 170, 82),
            color="#facc15",
            quad=((120, 65), (170, 65), (170, 82), (120, 82)),
        ),
    )
    api.items = [DimensionMarkingBatch(0, entries)]
    api.last_general_tolerance_marked = True
    try:
        state = api.remove_dimension_marking({"x": 65, "y": 72})
        assert state["ok"]
        batch = api.items[0]
        assert isinstance(batch, DimensionMarkingBatch)
        assert len(batch.entries) == 1
        assert batch.entries[0].color == "#facc15"
    finally:
        api.document.close()
        api.upload_directory.cleanup()


def main() -> None:
    _verify_tables()
    _verify_toggle()
    _verify_rendering()
    _verify_current_ui_flow()
    _verify_edited_dimensions_join_final_marking()
    _verify_angle_range_wiring()
    _verify_collision_aware_layout()
    _verify_marking_includes_added_tolerance()
    _verify_general_tolerance_drag_updates_marking()
    _verify_tolerance_resize_shrink()
    _verify_applied_tolerance_removal()
    _verify_dimension_marking_removal()
    _verify_hidden_ocr_window()
    _verify_native_drawing_detection()
    _verify_scanned_drawing_detection()
    print(
        "general tolerance tables, hidden OCR, native/image filtering, "
        "selection, and rendering: OK"
    )


if __name__ == "__main__":
    main()
