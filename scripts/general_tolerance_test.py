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
from drawing_assist.drawing_text_normalizer import normalize_drawing_text
from drawing_assist.local_ocr import (
    LocalOcrLine,
    LocalOcrPage,
    join_nearby_tolerance_ocr_lines,
    join_split_dimension_ocr_lines,
    select_vertical_dimension_clips,
)
from drawing_assist.web_app import (
    _DetectedDimensionMarking,
    _FIT_TOLERANCE_PATTERN,
    _detect_dimension_markings,
    _detect_local_dimension_markings,
    _detect_scanned_dimension_markings,
    _explicit_tolerance_range,
    _is_fit_deviation_fragment,
    _is_plausible_tolerance_marking,
    _marking_highlight_color,
    _marking_paint_parts,
    _merge_detected_markings,
    _paint_entries_join,
    _strip_from_paint_group,
    _unify_dimension_marking_entries,
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
        "公差未記載の寸法",
        "寸法を色分け",
        "製品を塗る",
        "その他の機能",
        "二重線で消す",
        "寸法と矢印を追加",
        "必要な寸法を書き直す",
        "印・必要な注記を入れる",
        "測定具・測定順を入れる",
        "一括反映",
        "公差未記載寸法を検出",
        "scanDimensionMarkingsButton",
        "detect-button",
        "厳しい公差（0.03以内・角度1°以内）",
        "角度公差の設定",
        "operation-steps",
        "workflow-tool",
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
    assert ".workflow-tool { padding-right: 9px; }" in styles
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
            (fitz.Rect(entry.rect) & descriptor_rect).get_area()
            >= descriptor_rect.get_area() * 0.80
            for entry in descriptor_batch.entries
        )

        def reaches_descriptor_tail(
            batch: DimensionMarkingBatch,
            addition: ToleranceAddition,
        ) -> bool:
            """縦書き補足文字の最終字形まで帯が届くことを確認する。"""

            assert addition.suffix_rect is not None
            axis = fitz.Point(addition.direction)
            axis /= math.hypot(axis.x, axis.y) or 1.0
            suffix = fitz.Rect(addition.suffix_rect)
            suffix_tail = max(
                point.x * axis.x + point.y * axis.y
                for point in (
                    suffix.top_left,
                    suffix.top_right,
                    suffix.bottom_right,
                    suffix.bottom_left,
                )
            )
            return any(
                entry.kind == "general_descriptor"
                and entry.quad
                and max(
                    fitz.Point(point).x * axis.x + fitz.Point(point).y * axis.y
                    for point in entry.quad
                )
                >= suffix_tail + 0.8
                for entry in batch.entries
            )

        assert reaches_descriptor_tail(descriptor_batch, face_addition)

        def reaches_redrawn_descriptor_tail(
            batch: DimensionMarkingBatch,
            addition: ToleranceAddition,
        ) -> bool:
            """公差の後ろへ再描画した補足語の末尾「）」まで帯が届く。"""

            axis = fitz.Point(addition.direction)
            axis /= math.hypot(axis.x, axis.y) or 1.0
            redrawn_tail = max(
                fitz.Point(point).x * axis.x + fitz.Point(point).y * axis.y
                for point in api._full_tolerance_addition_quad(addition)
            )
            return any(
                entry.kind == "general_descriptor"
                and entry.quad
                and max(
                    fitz.Point(point).x * axis.x + fitz.Point(point).y * axis.y
                    for point in entry.quad
                )
                >= redrawn_tail
                for entry in batch.entries
            )

        assert reaches_redrawn_descriptor_tail(descriptor_batch, face_addition)

        # 実図に残る「(幅)」も、追加した ±0.2 と同じ一本帯の末尾まで
        # 含むこと。推定した追加文字位置だけを使うと縦書きで欠ける。
        api.last_general_tolerance_batch = [groove_candidate]
        api.last_general_tolerance_additions = [addition]
        api.last_general_tolerance_marked = False
        _scan_and_apply_markings(api)
        groove_batch = api.items[-1]
        assert isinstance(groove_batch, DimensionMarkingBatch)
        groove_suffix_rect = fitz.Rect(addition.suffix_rect)
        assert any(
            (fitz.Rect(entry.rect) & groove_suffix_rect).get_area()
            >= groove_suffix_rect.get_area() * 0.80
            for entry in groove_batch.entries
        )
        assert reaches_descriptor_tail(groove_batch, addition)
        assert reaches_redrawn_descriptor_tail(groove_batch, addition)

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
    assert any(
        abs(marking.nominal_value - 0.03) < 1e-9
        and marking.kind == "geometric"
        for marking in markings
    ), [(marking.source_text, marking.nominal_value, marking.kind) for marking in markings]


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


def _verify_vertical_tolerance_postprocessing() -> None:
    nominal = LocalOcrLine(
        "Φ16.05",
        0.95,
        ((10, 10), (10, 60), (18, 60), (18, 10)),
    )
    tolerance = LocalOcrLine(
        "+0.05",
        0.98,
        ((10, 62), (10, 90), (18, 90), (18, 62)),
    )
    joined = join_nearby_tolerance_ocr_lines((nominal, tolerance))
    assert len(joined) == 1
    assert joined[0].text == "Φ16.05+0.05"
    assert abs(joined[0].direction[1]) > 0.99

    incomplete = LocalOcrLine(
        "2.5+",
        0.90,
        ((30, 30), (50, 30), (50, 40), (30, 40)),
    )
    lone_zero = LocalOcrLine(
        "0",
        0.99,
        ((52, 30), (58, 30), (58, 40), (52, 40)),
    )
    not_joined = join_nearby_tolerance_ocr_lines((incomplete, lone_zero))
    assert {line.text for line in not_joined} == {"2.5+", "0"}

    incomplete_dot = LocalOcrLine(
        "2.",
        0.95,
        ((30, 50), (42, 50), (42, 60), (30, 60)),
    )
    plus_025 = LocalOcrLine(
        "+0.25",
        0.99,
        ((48, 50), (70, 50), (70, 58), (48, 58)),
    )
    incomplete_plus = LocalOcrLine(
        "2.5+",
        0.90,
        ((28, 48), (50, 48), (50, 62), (28, 62)),
    )
    recovered = join_split_dimension_ocr_lines(
        (incomplete_dot, plus_025, incomplete_plus)
    )
    texts = {line.text for line in recovered}
    assert any(text.startswith("2.5+0.2") for text in texts)
    assert "2.+0.25" not in texts

    phi = LocalOcrLine(
        "Φ",
        0.83,
        ((10, 80), (10, 90), (18, 90), (18, 80)),
    )
    twelve_dot = LocalOcrLine(
        "12.",
        0.99,
        ((9, 58), (9, 78), (19, 78), (19, 58)),
    )
    nine = LocalOcrLine(
        "9",
        0.98,
        ((10, 50), (10, 56), (18, 56), (18, 50)),
    )
    plus_02 = LocalOcrLine(
        "+0.2",
        0.99,
        ((10, 34), (10, 50), (17, 50), (17, 34)),
    )
    neighbor = LocalOcrLine(
        "16.05",
        0.99,
        ((40, 50), (40, 90), (52, 90), (52, 50)),
    )
    stacked = join_split_dimension_ocr_lines(
        (plus_02, nine, twelve_dot, phi, neighbor)
    )
    stacked_texts = {line.text for line in stacked}
    assert any("12.9+0.2" in text for text in stacked_texts)
    assert "12.16.05" not in stacked_texts
    assert "9+0.2" not in stacked_texts
    assert any(line.text == "16.05" for line in stacked)
    recovered_12 = next(line for line in stacked if "12.9+0.2" in line.text)
    assert abs(recovered_12.direction[1]) > 0.9
    assert fitz.Rect(recovered_12.rect).width < 30

    nearby_radius = LocalOcrLine(
        "R0.2",
        0.99,
        ((55, 48), (78, 48), (78, 60), (55, 60)),
    )
    preferred = join_split_dimension_ocr_lines(
        (incomplete_dot, plus_025, incomplete_plus, nearby_radius)
    )
    preferred_texts = {line.text for line in preferred}
    assert any(text.startswith("2.5+0.2") for text in preferred_texts)
    assert not any(text.startswith("R0.2+") for text in preferred_texts)

    image = Image.new("L", (400, 400), 255)
    document = fitz.open()
    page = document.new_page(width=200, height=200)
    ocr_page = LocalOcrPage(
        400,
        400,
        2.0,
        2.0,
        image,
        (
            LocalOcrLine(
                "Φ14.",
                0.90,
                ((30, 90), (42, 90), (42, 120), (30, 120)),
            ),
            LocalOcrLine(
                "50.6±0.05",
                0.99,
                ((80, 40), (140, 40), (140, 52), (80, 52)),
            ),
            LocalOcrLine(
                "Φ24.95G6",
                0.95,
                ((90, 90), (102, 90), (102, 150), (90, 150)),
            ),
        ),
    )
    try:
        clips = select_vertical_dimension_clips(page, ocr_page, limit=8)
        assert len(clips) <= 8
        assert any(clip.x0 < 50 for clip in clips)
        assert any(80 <= (clip.x0 + clip.x1) / 2 <= 120 for clip in clips)
        assert not any(clip.y1 < 60 and clip.width > clip.height for clip in clips)
    finally:
        document.close()


def _verify_scanned_ocr_tolerance_recovery() -> None:
    repaired = normalize_drawing_text("2.8+01")
    assert repaired == "2.8+0.1"
    range_28 = _explicit_tolerance_range(repaired, 2.8)
    assert range_28 is not None and abs(range_28 - 0.1) < 1e-9
    assert _is_plausible_tolerance_marking(repaired, 2.8, range_28)

    repaired_fit = normalize_drawing_text("Φ16H7+0018")
    assert repaired_fit == "Φ16H7+0.018"
    range_fit = _explicit_tolerance_range(repaired_fit, 16.0)
    assert range_fit is not None and abs(range_fit - 0.018) < 1e-9
    assert _is_plausible_tolerance_marking(repaired_fit, 16.0, range_fit)

    image = Image.new("L", (2382, 1684), 255)
    document = fitz.open()
    page = document.new_page(width=1191, height=842)
    ocr_page = LocalOcrPage(
        2382,
        1684,
        2.0,
        2.0,
        image,
        (
            LocalOcrLine(
                "0.5-0.1",
                0.99,
                ((340, 49), (378, 49), (378, 64), (340, 64)),
            ),
            LocalOcrLine(
                "2.8+01",
                0.90,
                ((888, 91), (927, 91), (927, 109), (888, 109)),
            ),
            LocalOcrLine(
                "Φ16H7+0018",
                0.90,
                ((806, 348), (821, 348), (821, 409), (806, 409)),
            ),
        ),
    )
    try:
        markings = _detect_local_dimension_markings(
            page,
            ocr_page,
            include_plain_dimensions=False,
            scanned_page=True,
        )
        texts = {item.source_text.replace(" ", "") for item in markings}
        assert "0.5-0.1" in texts
        assert "2.8+0.1" in texts
        assert "Φ16H7+0.018" in texts
        phi_12 = [
            item
            for item in markings
            if abs(item.nominal_value - 12.9) < 1e-9
        ]
        assert not phi_12
    finally:
        document.close()

    def vertical_line(
        text: str,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
    ) -> LocalOcrLine:
        return LocalOcrLine(
            text,
            0.99,
            ((x0, y0), (x0, y1), (x1, y1), (x1, y0)),
        )

    fit_image = Image.new("L", (2382, 1684), 255)
    fit_document = fitz.open()
    fit_page = fit_document.new_page(width=1191, height=842)
    fit_ocr = LocalOcrPage(
        2382,
        1684,
        2.0,
        2.0,
        fit_image,
        (
            vertical_line("Φ18.5±0.05", 438, 429, 456, 490),
            vertical_line("Φ24.95G6", 461, 429, 481, 485),
            LocalOcrLine(
                "-0.007",
                0.99,
                ((483, 428), (496, 428), (496, 444), (483, 444)),
            ),
            LocalOcrLine(
                "-0.020",
                0.99,
                ((483, 444), (496, 444), (496, 460), (483, 460)),
            ),
            vertical_line("Φ26G6", 482, 439, 499, 475),
            LocalOcrLine(
                "-0.007",
                0.99,
                ((501, 438), (514, 438), (514, 454), (501, 454)),
            ),
            LocalOcrLine(
                "-0.020",
                0.99,
                ((501, 454), (514, 454), (514, 470), (501, 470)),
            ),
        ),
    )
    try:
        fit_markings = _detect_local_dimension_markings(
            fit_page,
            fit_ocr,
            include_plain_dimensions=False,
            scanned_page=True,
        )
        fits = [
            item
            for item in fit_markings
            if "G6" in item.source_text.replace(" ", "")
        ]
        assert len(fits) == 2
        left = next(item for item in fits if abs(item.nominal_value - 24.95) < 1e-9)
        right = next(item for item in fits if abs(item.nominal_value - 26.0) < 1e-9)
        assert left.rect[2] < right.rect[0] + 4
        overlap = (
            fitz.Rect(left.rect) & fitz.Rect(right.rect)
        ).get_area()
        assert overlap < 0.45 * min(
            fitz.Rect(left.rect).get_area(),
            fitz.Rect(right.rect).get_area(),
        )
        assert left.tolerance_rect is not None
        assert right.tolerance_rect is not None
        assert left.tolerance_rect[2] >= 494
        assert right.tolerance_rect[2] >= 512
        yellow = next(
            item
            for item in fit_markings
            if abs(item.nominal_value - 18.5) < 1e-9
        )
        left_overlap_yellow = (
            fitz.Rect(left.rect) & fitz.Rect(yellow.rect)
        ).get_area()
        assert left_overlap_yellow < 0.2 * fitz.Rect(yellow.rect).get_area()
        if left.tolerance_rect is not None:
            tol_overlap_yellow = (
                fitz.Rect(left.tolerance_rect) & fitz.Rect(yellow.rect)
            ).get_area()
            assert tol_overlap_yellow < 0.2 * fitz.Rect(yellow.rect).get_area()
        paint_parts = _marking_paint_parts(left)
        widths = [part[0][2] - part[0][0] for part in paint_parts]
        assert max(widths) - min(widths) < 3.0
    finally:
        fit_document.close()

    column_image = Image.new("L", (2382, 1684), 255)
    column_document = fitz.open()
    column_page = column_document.new_page(width=1191, height=842)
    column_ocr = LocalOcrPage(
        2382,
        1684,
        2.0,
        2.0,
        column_image,
        (
            vertical_line("Φ12.9+0.2", 485, 635, 496, 688),
            vertical_line("0", 486, 628, 494, 636),
            vertical_line("Φ16H7+0.018", 518, 626, 529, 691),
            vertical_line("0", 519, 618, 527, 626),
            vertical_line("16.05+0.05", 540, 626, 551, 686),
            vertical_line("0", 541, 618, 549, 626),
        ),
    )
    try:
        column_markings = _detect_local_dimension_markings(
            column_page,
            column_ocr,
            include_plain_dimensions=False,
            scanned_page=True,
        )
        h7 = next(
            item
            for item in column_markings
            if "H7" in item.source_text.replace(" ", "")
        )
        assert h7.rect[2] - h7.rect[0] < 28
        assert h7.rect[0] > 500
        texts = {item.source_text.replace(" ", "") for item in column_markings}
        assert any("12.9" in text for text in texts)
        assert any("16.05" in text for text in texts)
        phi_12 = next(
            item
            for item in column_markings
            if abs(item.nominal_value - 12.9) < 1e-9
        )
        assert phi_12.tolerance_range is not None
        assert phi_12.tolerance_range > 0.03
        assert _marking_highlight_color(phi_12.kind, phi_12.tolerance_range) == (
            "#ffff00"
        )
        assert not _FIT_TOLERANCE_PATTERN.search(phi_12.source_text.replace(" ", ""))
        if h7.tolerance_rect is not None:
            overlap_12 = (
                fitz.Rect(h7.tolerance_rect) & fitz.Rect(phi_12.rect)
            ).get_area()
            assert overlap_12 < 0.35 * fitz.Rect(phi_12.rect).get_area()
    finally:
        column_document.close()

    roughness_image = Image.new("L", (2382, 1684), 255)
    roughness_document = fitz.open()
    roughness_page = roughness_document.new_page(width=1191, height=842)
    roughness_ocr = LocalOcrPage(
        2382,
        1684,
        2.0,
        2.0,
        roughness_image,
        (
            LocalOcrLine(
                "(Rzmax 5.7 , Rzmax 2.9)",
                0.99,
                ((800, 46), (965, 46), (965, 61), (800, 61)),
            ),
        ),
    )
    try:
        roughness_markings = _detect_local_dimension_markings(
            roughness_page,
            roughness_ocr,
            include_plain_dimensions=False,
            scanned_page=True,
        )
        roughness = [
            item for item in roughness_markings if item.kind == "roughness"
        ]
        assert len(roughness) == 2
        widths = sorted(item.rect[2] - item.rect[0] for item in roughness)
        assert widths[-1] < 120
        left_r, right_r = sorted(roughness, key=lambda item: item.rect[0])
        assert left_r.rect[2] <= right_r.rect[0] + 1.0
        overlap = fitz.Rect(left_r.rect) & fitz.Rect(right_r.rect)
        assert overlap.is_empty or overlap.get_area() < 0.5
        dotted = _detect_local_dimension_markings(
            roughness_page,
            LocalOcrPage(
                2382,
                1684,
                2.0,
                2.0,
                roughness_image,
                (
                    LocalOcrLine(
                        "Rzmax 11.3・Rzmax 5.7・Rzmax 2.9",
                        0.99,
                        ((800, 100), (980, 100), (980, 114), (800, 114)),
                    ),
                ),
            ),
            include_plain_dimensions=False,
            scanned_page=True,
        )
        dotted_items = [item for item in dotted if item.kind == "roughness"]
        assert len(dotted_items) == 3
        ordered = sorted(dotted_items, key=lambda item: item.rect[0])
        assert ordered[0].rect[2] < ordered[1].rect[0]
        assert ordered[1].rect[2] < ordered[2].rect[0]
        heights = [item.rect[3] - item.rect[1] for item in ordered]
        assert max(heights) - min(heights) < 1.5
        paren_heights = [item.rect[3] - item.rect[1] for item in roughness]
        assert max(paren_heights) - min(paren_heights) < 1.5
        duplicate_roughness = _detect_local_dimension_markings(
            roughness_page,
            LocalOcrPage(
                2382,
                1684,
                2.0,
                2.0,
                roughness_image,
                (
                    LocalOcrLine(
                        "Rzmax 6.3",
                        0.92,
                        ((700, 300), (820, 300), (820, 318), (700, 318)),
                    ),
                    LocalOcrLine(
                        "Rzmax 6.3",
                        0.99,
                        ((760, 302), (830, 302), (830, 322), (760, 322)),
                    ),
                ),
            ),
            include_plain_dimensions=False,
            scanned_page=True,
        )
        same_value = [
            item
            for item in duplicate_roughness
            if item.kind == "roughness" and abs(item.nominal_value - 6.3) < 1e-9
        ]
        assert len(same_value) == 1
        split_fit = _detect_local_dimension_markings(
            roughness_page,
            LocalOcrPage(
                2382,
                1684,
                2.0,
                2.0,
                roughness_image,
                (
                    LocalOcrLine(
                        "Φ16",
                        0.99,
                        ((200, 400), (230, 400), (230, 414), (200, 414)),
                    ),
                    LocalOcrLine(
                        "Φ16H7+0.018",
                        0.99,
                        ((228, 400), (310, 400), (310, 416), (228, 416)),
                    ),
                ),
            ),
            include_plain_dimensions=False,
            scanned_page=True,
        )
        sixteen = [
            item
            for item in split_fit
            if abs(item.nominal_value - 16.0) < 1e-9
        ]
        assert len(sixteen) == 1
        assert sixteen[0].rect[2] - sixteen[0].rect[0] >= 70
        from drawing_assist.pdf_editor import DimensionMarkingEntry as PaintEntry

        broken = [
            PaintEntry(
                (100, 40, 148, 52),
                "#ff76bf",
                0.42,
                ((100, 42), (148, 42), (148, 50), (100, 50)),
            ),
            PaintEntry(
                (150, 39, 188, 55),
                "#ff76bf",
                0.42,
                ((150, 39), (188, 39), (188, 55), (150, 55)),
            ),
        ]
        unified = _unify_dimension_marking_entries(broken)
        assert len(unified) == 1
        assert unified[0].rect[2] - unified[0].rect[0] >= 85
        height = unified[0].rect[3] - unified[0].rect[1]
        assert 7.5 <= height <= 17.0
        descriptor_fragments = [
            PaintEntry(
                (220, 40, 264, 54),
                "#ffff00",
                0.42,
                ((220, 42), (264, 42), (264, 52), (220, 52)),
                "general_descriptor",
            ),
            PaintEntry(
                (265, 40, 318, 54),
                "#ffff00",
                0.42,
                ((265, 42), (318, 42), (318, 52), (265, 52)),
                "general_descriptor",
            ),
        ]
        descriptor_unified = _strip_from_paint_group(
            descriptor_fragments,
            None,
            1.0,
            1.0,
        )
        assert descriptor_unified.kind == "general_descriptor"
        assert descriptor_unified.rect[2] >= 317
        diagonal = PaintEntry(
            (90, 100, 160, 159),
            "#ffff00",
            0.42,
            ((100, 100), (160, 145), (150, 159), (90, 114)),
        )
        diagonal_out = _unify_dimension_marking_entries([diagonal])[0]
        diagonal_edge = (
            diagonal_out.quad[1][0] - diagonal_out.quad[0][0],
            diagonal_out.quad[1][1] - diagonal_out.quad[0][1],
        )
        assert abs(diagonal_edge[1] / diagonal_edge[0]) > 0.4
        complete_pair = [
            PaintEntry(
                (800, 46, 878, 61),
                "#ff76bf",
                0.42,
                ((800, 46), (878, 46), (878, 61), (800, 61)),
            ),
            PaintEntry(
                (890, 48, 968, 60),
                "#ff76bf",
                0.42,
                ((890, 48), (968, 48), (968, 60), (890, 60)),
            ),
        ]
        aligned = _unify_dimension_marking_entries(complete_pair)
        assert len(aligned) == 2
        aligned_heights = [item.rect[3] - item.rect[1] for item in aligned]
        assert max(aligned_heights) - min(aligned_heights) < 1.2
        overlapping_complete_pair = [
            PaintEntry(
                (800, 46, 878, 61),
                "#ff76bf",
                0.42,
                ((800, 46), (878, 46), (878, 61), (800, 61)),
            ),
            PaintEntry(
                (876, 48, 954, 60),
                "#ff76bf",
                0.42,
                ((876, 48), (954, 48), (954, 60), (876, 60)),
            ),
        ]
        overlapping_complete = _unify_dimension_marking_entries(
            overlapping_complete_pair
        )
        assert len(overlapping_complete) == 2
        overlap = fitz.Rect(overlapping_complete[0].rect) & fitz.Rect(
            overlapping_complete[1].rect
        )
        assert overlap.is_empty or overlap.get_area() < 1.0
        yellow = PaintEntry(
            (438, 429, 456, 490),
            "#ffff00",
            0.42,
            ((438, 429), (456, 429), (456, 490), (438, 490)),
        )
        pink = PaintEntry(
            (448, 428, 514, 470),
            "#ff76bf",
            0.42,
            ((448, 428), (514, 428), (514, 470), (448, 470)),
        )
        separated = _unify_dimension_marking_entries([yellow, pink])
        pink_out = next(item for item in separated if item.color == "#ff76bf")
        yellow_out = next(item for item in separated if item.color == "#ffff00")
        overlap_area = (
            fitz.Rect(pink_out.rect) & fitz.Rect(yellow_out.rect)
        ).get_area()
        assert overlap_area < 0.12 * fitz.Rect(yellow_out.rect).get_area()
        punct = _detect_local_dimension_markings(
            roughness_page,
            LocalOcrPage(
                2382,
                1684,
                2.0,
                2.0,
                roughness_image,
                (
                    LocalOcrLine(
                        "(",
                        0.99,
                        ((792, 46), (800, 46), (800, 61), (792, 61)),
                    ),
                ),
            ),
            include_plain_dimensions=False,
            scanned_page=True,
        )
        assert punct == []
        tall = _detect_local_dimension_markings(
            roughness_page,
            LocalOcrPage(
                2382,
                1684,
                2.0,
                2.0,
                roughness_image,
                (
                    LocalOcrLine(
                        "Rmax 5.7",
                        0.99,
                        ((700, 200), (708, 200), (708, 280), (700, 280)),
                    ),
                ),
            ),
            include_plain_dimensions=False,
            scanned_page=True,
        )
        assert all(item.kind != "roughness" for item in tall)
        overlapping_roughness = _detect_local_dimension_markings(
            roughness_page,
            LocalOcrPage(
                2382,
                1684,
                2.0,
                2.0,
                roughness_image,
                (
                    LocalOcrLine(
                        "Rmax 5.7",
                        0.99,
                        ((800, 80), (890, 80), (890, 95), (800, 95)),
                    ),
                    LocalOcrLine(
                        "Rmax 2.9",
                        0.99,
                        ((870, 80), (965, 80), (965, 95), (870, 95)),
                    ),
                ),
            ),
            include_plain_dimensions=False,
            scanned_page=True,
        )
        split_roughness = [
            item for item in overlapping_roughness if item.kind == "roughness"
        ]
        assert len(split_roughness) == 2
        left_s, right_s = sorted(split_roughness, key=lambda item: item.rect[0])
        split_overlap = fitz.Rect(left_s.rect) & fitz.Rect(right_s.rect)
        assert split_overlap.is_empty or split_overlap.get_area() < 0.5
    finally:
        roughness_document.close()

    joined_roughness = join_split_dimension_ocr_lines(
        (
            LocalOcrLine(
                "Rzmax 5.",
                0.95,
                ((100, 80), (160, 80), (160, 94), (100, 94)),
            ),
            LocalOcrLine(
                "7",
                0.98,
                ((162, 80), (170, 80), (170, 94), (162, 94)),
            ),
        )
    )
    assert any("5.7" in line.text.replace(" ", "") for line in joined_roughness)

    horizontal_image = Image.new("L", (2382, 1684), 255)
    horizontal_document = fitz.open()
    horizontal_page = horizontal_document.new_page(width=1191, height=842)
    horizontal_ocr = LocalOcrPage(
        2382,
        1684,
        2.0,
        2.0,
        horizontal_image,
        (
            LocalOcrLine(
                "Φ18G6",
                0.99,
                ((120, 200), (168, 200), (168, 214), (120, 214)),
            ),
            LocalOcrLine(
                "(+0.012/+0.001)",
                0.99,
                ((170, 200), (248, 200), (248, 214), (170, 214)),
            ),
        ),
    )
    try:
        horizontal_markings = _detect_local_dimension_markings(
            horizontal_page,
            horizontal_ocr,
            include_plain_dimensions=False,
            scanned_page=True,
        )
        fit18 = next(
            item
            for item in horizontal_markings
            if abs(item.nominal_value - 18.0) < 1e-9
        )
        assert fit18.rect[2] >= 240
    finally:
        horizontal_document.close()

    split_paren_image = Image.new("L", (2382, 1684), 255)
    split_paren_document = fitz.open()
    split_paren_page = split_paren_document.new_page(width=1191, height=842)
    split_paren_ocr = LocalOcrPage(
        2382,
        1684,
        2.0,
        2.0,
        split_paren_image,
        (
            LocalOcrLine(
                "Φ18G6",
                0.99,
                ((120, 300), (168, 300), (168, 314), (120, 314)),
            ),
            LocalOcrLine(
                "(",
                0.99,
                ((169, 300), (176, 300), (176, 314), (169, 314)),
            ),
            LocalOcrLine(
                "+0.012/+0.001",
                0.99,
                ((176, 300), (240, 300), (240, 314), (176, 314)),
            ),
            LocalOcrLine(
                ")",
                0.99,
                ((240, 300), (248, 300), (248, 314), (240, 314)),
            ),
        ),
    )
    try:
        split_paren_markings = _detect_local_dimension_markings(
            split_paren_page,
            split_paren_ocr,
            include_plain_dimensions=False,
            scanned_page=True,
        )
        split18 = next(
            item
            for item in split_paren_markings
            if abs(item.nominal_value - 18.0) < 1e-9
        )
        assert split18.rect[2] >= 246
        assert all(
            item.source_text.strip() not in {"(", ")"}
            for item in split_paren_markings
        )
    finally:
        split_paren_document.close()

    combined_image = Image.new("L", (2382, 1684), 255)
    combined_document = fitz.open()
    combined_page = combined_document.new_page(width=1191, height=842)
    combined_ocr = LocalOcrPage(
        2382,
        1684,
        2.0,
        2.0,
        combined_image,
        (
            LocalOcrLine(
                "Φ18G6(+0.012/+0.001)",
                0.99,
                ((120, 260), (248, 260), (248, 274), (120, 274)),
            ),
        ),
    )
    try:
        combined_markings = _detect_local_dimension_markings(
            combined_page,
            combined_ocr,
            include_plain_dimensions=False,
            scanned_page=True,
        )
        combined18 = next(
            item
            for item in combined_markings
            if abs(item.nominal_value - 18.0) < 1e-9
        )
        assert combined18.rect[2] >= 240
    finally:
        combined_document.close()

    def marking(
        x0: float,
        x1: float,
        text: str,
        nominal_value: float,
    ) -> _DetectedDimensionMarking:
        return _DetectedDimensionMarking(
            rect=(x0, 10, x1, 90),
            quad=((x0, 10), (x1, 10), (x1, 90), (x0, 90)),
            direction=(0, 1),
            source_text=text,
            nominal_value=nominal_value,
            kind="diameter",
            tolerance_range=0.02,
        )

    assert not _is_fit_deviation_fragment("+0.2")
    assert not _is_fit_deviation_fragment("+0.1")
    assert _is_fit_deviation_fragment("+0.018")
    assert _is_fit_deviation_fragment("-0.007")
    assert _is_fit_deviation_fragment("0")
    assert _FIT_TOLERANCE_PATTERN.search("Φ16H7")
    assert _FIT_TOLERANCE_PATTERN.search("Φ26g6")
    assert not _FIT_TOLERANCE_PATTERN.search("Φ12.9+0.2")
    assert not _FIT_TOLERANCE_PATTERN.search("H12.9")

    merged = _merge_detected_markings(
        [marking(10, 22, "Φ12.9+0.2", 12.9)],
        [
            marking(18, 30, "Φ16H7", 16),
            marking(27, 39, "Φ16.05+0.05", 16.05),
        ],
    )
    assert len(merged) == 3


def _verify_stacked_vector_tolerance_detection() -> None:
    """テキストPDFの上下公差と小さい小数寸法を回帰確認する。"""

    document = fitz.open()
    page = document.new_page(width=420, height=260)
    # 基準となる通常寸法を置き、小さい 0.12 が寸法文字として扱われる
    # 最低書体比になるようにする。
    page.insert_text((80, 56), "14.7", fontsize=10)
    page.insert_text((130, 56), "3.5", fontsize=10)
    page.insert_text((208, 56), "0.12", fontsize=6)
    page.draw_line((190, 60), (250, 60), width=0.25)
    page.draw_line((190, 54), (190, 66), width=0.25)
    page.draw_line((250, 54), (250, 66), width=0.25)
    # 公称値と上下公差を別テキスト行へ置く。
    page.insert_text((80, 130), "2.9", fontsize=10)
    page.insert_text((112, 123), "+0.1", fontsize=6)
    page.insert_text((112, 135), "0", fontsize=6)
    page.draw_line((70, 140), (150, 140), width=0.25)
    page.draw_line((70, 134), (70, 146), width=0.25)
    page.draw_line((150, 134), (150, 146), width=0.25)
    try:
        markings = _detect_dimension_markings(page)
        stacked = next(
            marking
            for marking in markings
            if abs(marking.nominal_value - 2.9) < 1e-9
        )
        assert stacked.tolerance_range is not None
        assert abs(stacked.tolerance_range - 0.1) < 1e-9
        assert stacked.tolerance_quad is not None
        tolerance_rect = fitz.Rect(stacked.tolerance_rect)
        assert tolerance_rect.y0 <= 118 and tolerance_rect.y1 >= 135
        paint_parts = _marking_paint_parts(stacked)
        # 上下公差を公称値と同じ高さの一本帯にして、どちらも欠けずに覆う。
        assert len(paint_parts) == 1
        paint_rect, _paint_quad = paint_parts[0]
        assert paint_rect[0] <= 80 and paint_rect[2] >= 118
        assert paint_rect[1] <= 118 and paint_rect[3] >= 135

        # 実図では上下公差のOCR枠が公称値よりさらに離れる場合がある。
        # その場合も横書きの一注記として一本の帯に統合する。
        wide_stacked = _DetectedDimensionMarking(
            rect=(80, 130, 110, 141),
            quad=((80, 130), (110, 130), (110, 141), (80, 141)),
            direction=(1.0, 0.0),
            source_text="2.9+0.1/0",
            nominal_value=2.9,
            kind="linear",
            tolerance_range=0.1,
            tolerance_rect=(112, 106, 130, 130),
            tolerance_quad=((112, 106), (130, 106), (130, 130), (112, 130)),
        )
        wide_parts = _marking_paint_parts(wide_stacked)
        assert len(wide_parts) == 1
        wide_rect, _wide_quad = wide_parts[0]
        assert wide_rect[0] <= 80 and wide_rect[2] >= 130
        assert wide_rect[1] <= 106 and wide_rect[3] >= 141

        # 細い「1」でも、横書きの読取方向を失わず上下公差まで一本で覆う。
        compact_stacked = _DetectedDimensionMarking(
            rect=(80, 168, 87, 181),
            quad=((80, 168), (87, 168), (87, 181), (80, 181)),
            direction=(1.0, 0.0),
            source_text="1 0/-0.05",
            nominal_value=1.0,
            kind="linear",
            tolerance_range=0.05,
            tolerance_rect=(88, 153, 103, 181),
            tolerance_quad=((88, 153), (103, 153), (103, 181), (88, 181)),
        )
        compact_parts = _marking_paint_parts(compact_stacked)
        assert len(compact_parts) == 1
        compact_rect, _compact_quad = compact_parts[0]
        assert compact_rect[0] <= 80 and compact_rect[2] >= 103
        assert compact_rect[1] <= 153 and compact_rect[3] >= 181

        candidates = detect_general_tolerance_candidates(
            page,
            0,
            standard="jis_b_0405",
            grade="m",
            ocr_script=ROOT / "src" / "drawing_assist" / "windows_ocr.ps1",
        )
        assert any(
            abs(candidate.nominal_value - 0.12) < 1e-9
            for candidate in candidates
        )
        assert not any(
            abs(candidate.nominal_value - 2.9) < 1e-9
            for candidate in candidates
        )
    finally:
        document.close()


def _verify_vertical_tolerance_paint_is_connected() -> None:
    """縦書き寸法の上下公差は一本の帯として連結する。"""

    marking = _DetectedDimensionMarking(
        rect=(80, 100, 90, 140),
        quad=((80, 100), (90, 100), (90, 140), (80, 140)),
        direction=(0.0, 1.0),
        source_text="φ19.4-0.01/-0.04",
        nominal_value=19.4,
        kind="diameter",
        tolerance_range=0.04,
        tolerance_rect=(74, 72, 92, 100),
        tolerance_quad=((74, 72), (92, 72), (92, 100), (74, 100)),
    )
    parts = _marking_paint_parts(marking)
    assert len(parts) == 1
    rect, _quad = parts[0]
    assert rect[0] <= 74 and rect[2] >= 92
    assert rect[1] <= 72 and rect[3] >= 140


def _verify_different_direction_paint_is_not_joined() -> None:
    """横寸法と隣接する斜め角度の帯を統合しない。"""

    horizontal = DimensionMarkingEntry(
        (10, 10, 30, 14),
        "#ffff00",
        quad=((10, 10), (30, 10), (30, 14), (10, 14)),
        kind="linear",
    )
    diagonal = DimensionMarkingEntry(
        (28, 8, 43, 23),
        "#ffff00",
        quad=((31, 8), (43, 20), (40, 23), (28, 11)),
        kind="angle",
    )
    assert not _paint_entries_join(horizontal, diagonal)

    # 同じ値・同じ黄色でも、R/C の「以下」は別々の個別指示である。
    radius_limit = DimensionMarkingEntry(
        (50, 10, 84, 16),
        "#ffff00",
        quad=((50, 10), (84, 10), (84, 16), (50, 16)),
        kind="limit",
    )
    chamfer_limit = DimensionMarkingEntry(
        (85, 10, 119, 16),
        "#ffff00",
        quad=((85, 10), (119, 10), (119, 16), (85, 16)),
        kind="limit",
    )
    assert not _paint_entries_join(radius_limit, chamfer_limit)


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
    _verify_vertical_tolerance_postprocessing()
    _verify_scanned_ocr_tolerance_recovery()
    _verify_hidden_ocr_window()
    _verify_native_drawing_detection()
    _verify_scanned_drawing_detection()
    _verify_stacked_vector_tolerance_detection()
    _verify_vertical_tolerance_paint_is_connected()
    _verify_different_direction_paint_is_not_joined()
    print(
        "general tolerance tables, hidden OCR, native/image filtering, "
        "selection, and rendering: OK"
    )


if __name__ == "__main__":
    main()
