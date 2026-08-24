(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const apiToken =
    new URLSearchParams(window.location.search).get("token") || "";

  const elements = {
    pdfFileInput: $("#pdfFileInput"),
    open: $("#openButton"),
    emptyOpen: $("#emptyOpenButton"),
    save: $("#saveButton"),
    previous: $("#previousButton"),
    next: $("#nextButton"),
    undo: $("#undoButton"),
    clearPage: $("#clearPageButton"),
    clearAll: $("#clearAllButton"),
    fileName: $("#fileName"),
    fileMeta: $("#fileMeta"),
    pageLabel: $("#pageLabel"),
    empty: $("#emptyState"),
    viewer: $(".viewer"),
    viewport: $("#documentViewport"),
    stage: $("#documentStage"),
    image: $("#pageImage"),
    canvas: $("#interactionCanvas"),
    status: $("#statusMessage"),
    dropOverlay: $("#dropOverlay"),
    processingOverlay: $("#processingOverlay"),
    processingTitle: $("#processingTitle"),
    processingDetail: $("#processingDetail"),
    zoomRange: $("#zoomRange"),
    zoomLabel: $("#zoomLabel"),
    zoomOut: $("#zoomOutButton"),
    zoomIn: $("#zoomInButton"),
    buildLabel: $("#buildLabel"),
    toolName: $("#settingsToolName"),
    modeHelp: $("#modeHelp"),
    opacity: $("#opacityRange"),
    opacityValue: $("#opacityValue"),
    customColor: $("#customColor"),
    markMethod: $("#markMethod"),
    highlightWidth: $("#highlightWidth"),
    angledWidthGroup: $("#angledWidthGroup"),
    workShapeStyle: $("#workShapeStyle"),
    guidedWorkControls: $("#guidedWorkControls"),
    guidedWorkPointCount: $("#guidedWorkPointCount"),
    predictWorkShape: $("#predictWorkShapeButton"),
    removeGuidedPoint: $("#removeGuidedPointButton"),
    resetGuidedPoints: $("#resetGuidedPointsButton"),
    autoWorkControls: $("#autoWorkControls"),
    workCorrectionGuide: $("#workCorrectionGuide"),
    manualWorkControls: $("#manualWorkControls"),
    autoWorkReplace: $("#autoWorkReplaceButton"),
    autoWorkAdd: $("#autoWorkAddButton"),
    autoWorkRemove: $("#autoWorkRemoveButton"),
    autoWorkCandidateCount: $("#autoWorkCandidateCount"),
    confirmAutoWork: $("#confirmAutoWorkButton"),
    cancelAutoWork: $("#cancelAutoWorkButton"),
    wordCandidateBar: $("#wordCandidateBar"),
    wordCandidateOrientation: $("#wordCandidateOrientation"),
    confirmWordCandidate: $("#confirmWordCandidateButton"),
    cancelWordCandidate: $("#cancelWordCandidateButton"),
    workLineWidth: $("#workLineWidth"),
    workLineWidthGroup: $("#workLineWidthGroup"),
    workPointCount: $("#workPointCount"),
    finishWorkShape: $("#finishWorkShapeButton"),
    removeWorkPoint: $("#removeWorkPointButton"),
    cancelWorkShape: $("#cancelWorkShapeButton"),
    dimensionText: $("#dimensionText"),
    dimensionShowLeader: $("#dimensionShowLeader"),
    dimensionAutoStyle: $("#dimensionAutoStyle"),
    dimensionManualStyle: $("#dimensionManualStyle"),
    dimensionStyleStatus: $("#dimensionStyleStatus"),
    fontSize: $("#fontSize"),
    stampName: $("#stampName"),
    stampDate: $("#stampDate"),
    stampSize: $("#stampSize"),
    procedureNoteType: $("#procedureNoteType"),
    procedureNoteText: $("#procedureNoteText"),
    procedureNoteSize: $("#procedureNoteSize"),
    measurementType: $("#measurementType"),
    measurementInstrument: $("#measurementInstrument"),
    measurementInstrumentGroup: $("#measurementInstrumentGroup"),
    measurementSequence: $("#measurementSequence"),
    measurementSequenceGroup: $("#measurementSequenceGroup"),
    measurementSize: $("#measurementSize"),
    replacementValue: $("#replacementValue"),
    originalReplacementValue: $("#originalReplacementValue"),
    replacementSelectionHint: $("#replacementSelectionHint"),
    upperTolerance: $("#upperTolerance"),
    lowerTolerance: $("#lowerTolerance"),
    replacementSize: $("#replacementSize"),
    replacementToleranceSize: $("#replacementToleranceSize"),
    confirmReplacement: $("#confirmReplacementButton"),
    cancelReplacement: $("#cancelReplacementButton"),
    replacementPreviewValue: $("#replacementPreviewValue"),
    replacementPreviewTolerance: $("#replacementPreviewTolerance"),
    replacementPreviewUpper: $("#replacementPreviewUpper"),
    replacementPreviewLower: $("#replacementPreviewLower"),
    generalToleranceStandard: $("#generalToleranceStandard"),
    generalToleranceGrade: $("#generalToleranceGrade"),
    generalToleranceGradeGroup: $("#generalToleranceGradeGroup"),
    generalToleranceAngleLength: $("#generalToleranceAngleLength"),
    scanGeneralTolerance: $("#scanGeneralToleranceButton"),
    applyGeneralTolerance: $("#applyGeneralToleranceButton"),
    removeAppliedTolerance: $("#removeAppliedToleranceButton"),
    scanDimensionMarkings: $("#scanDimensionMarkingsButton"),
    applyDimensionMarkings: $("#applyDimensionMarkingsButton"),
    removeDimensionMarking: $("#removeDimensionMarkingButton"),
    dimensionMarkingHint: $("#dimensionMarkingHint"),
    dimensionMarkingCandidateCount: $("#dimensionMarkingCandidateCount"),
    dimensionFixTools: $(".dimension-fix-tools"),
    dimensionFixStatus: $("#dimensionFixStatus"),
    generalToleranceCandidateCount: $("#generalToleranceCandidateCount"),
    generalToleranceHint: $("#generalToleranceHint"),
    toleranceFlowDetect: $("#toleranceFlowDetect"),
    toleranceFlowReview: $("#toleranceFlowReview"),
    toleranceFlowApply: $("#toleranceFlowApply"),
    markingFlowDetect: $("#markingFlowDetect"),
    markingFlowReview: $("#markingFlowReview"),
    markingFlowApply: $("#markingFlowApply"),
    toast: $("#toast"),
    taskStep: $("#taskStep"),
    taskTitle: $("#taskTitle"),
    canvasCoach: $("#canvasCoach"),
    canvasCoachStep: $("#canvasCoachStep"),
    canvasCoachText: $("#canvasCoachText"),
    summaryColor: $(".summary-color"),
  };

  const toolInfo = {
    word: {
      name: "寸法を色分け",
      help: "寸法と公差を基準色で一括色分けします。",
      cursor: "crosshair",
    },
    work_shape: {
      name: "製品部分を塗る",
      help: "製品の断面や加工部の外形を、順番にクリックします。",
      cursor: "crosshair",
    },
    strike: {
      name: "二重線で消す",
      help: "二重線を入れたい寸法の真ん中を1回クリックしてください。",
      cursor: "crosshair",
    },
    replace: {
      name: "必要な寸法を書き直す",
      help: "最初に図面上の直したい寸法を選びます。",
      cursor: "text",
    },
    general_tolerance: {
      name: "公差を入れる",
      help: "規格を選び、検出した寸法に公差を一括反映します。",
      cursor: "pointer",
    },
    dimension: {
      name: "寸法と矢印を追加",
      help: "矢印の先から文字位置までドラッグします。原図の書式に合わせ、既存寸法を避けて配置します。",
      cursor: "crosshair",
    },
    quality_stamp: {
      name: "品質保証印",
      help: "スタンプの中心にしたい場所をクリックしてください。名前と日付は上で編集できます。",
      cursor: "copy",
    },
    process_stamp: {
      name: "加工図印",
      help: "スタンプの中心にしたい場所をクリックしてください。名前と日付は上で編集できます。",
      cursor: "copy",
    },
    procedure_note: {
      name: "必要な注記",
      help: "注記の種類と内容を確認し、図面上の空いている場所をクリックします。",
      cursor: "copy",
    },
    measurement: {
      name: "測定具・測定順を入れる",
      help: "測定具記号または順番を選び、対象寸法の近くをクリックします。",
      cursor: "copy",
    },
  };

  let currentState = { loaded: false };
  let currentMode = "general_tolerance";
  let currentColor = "#fff24d";
  let busy = false;
  let dragStart = null;
  let dragCurrent = null;
  let workPoints = [];
  let workAutoOperation = "replace";
  let guidedPredictionRequested = false;
  let guidedPredictionReady = false;
  let dragDepth = 0;
  let toastTimer = null;
  let dimensionFontFamily = "'MS PGothic'";
  let dimensionLineWidth = 0.35;
  let replacementOffsets = {
    valueX: 0,
    valueY: 0,
    toleranceX: 0,
    toleranceY: 0,
  };
  let replacementDrag = null;
  let replacementHitAreas = {
    value: null,
    tolerance: null,
    valueResize: null,
    toleranceResize: null,
  };
  let editableItemDrag = null;
  let editableItemPreview = null;
  let removeAppliedToleranceMode = false;
  let removeDimensionMarkingMode = false;

  function setRemoveAppliedToleranceMode(enabled) {
    removeAppliedToleranceMode = Boolean(enabled);
    elements.removeAppliedTolerance?.classList.toggle(
      "active-toggle",
      removeAppliedToleranceMode
    );
    if (elements.removeAppliedTolerance) {
      elements.removeAppliedTolerance.textContent = removeAppliedToleranceMode
        ? "解除モード ON（クリックで解除）"
        : "不要な公差を解除";
    }
    updateButtons();
  }

  function setRemoveDimensionMarkingMode(enabled) {
    removeDimensionMarkingMode = Boolean(enabled);
    elements.removeDimensionMarking?.classList.toggle(
      "active-toggle",
      removeDimensionMarkingMode
    );
    if (elements.removeDimensionMarking) {
      elements.removeDimensionMarking.textContent = removeDimensionMarkingMode
        ? "解除モード ON（クリックで解除）"
        : "不要な色を解除";
    }
    updateButtons();
  }

  function updateMarkingFlowSteps() {
    const hasCandidates = Number(
      currentState.dimension_marking_candidate_count || 0
    ) > 0;
    const marked = Boolean(currentState.general_tolerance_marked);
    const steps = [
      elements.markingFlowDetect,
      elements.markingFlowReview,
      elements.markingFlowApply,
    ];
    if (!steps[0]) return;
    steps.forEach((step) => step.classList.remove("active", "done"));
    if (marked) {
      steps[0].classList.add("done");
      steps[1].classList.add("done");
      steps[2].classList.add("done");
    } else if (hasCandidates) {
      steps[0].classList.add("done");
      steps[1].classList.add("active");
    } else {
      steps[0].classList.add("active");
    }
  }

  function updateToleranceFlowSteps() {
    const hasCandidates = Number(
      currentState.general_tolerance_candidate_count || 0
    ) > 0;
    const hasApplied = Number(
      currentState.general_tolerance_applied_count || 0
    ) > 0;
    const steps = [
      elements.toleranceFlowDetect,
      elements.toleranceFlowReview,
      elements.toleranceFlowApply,
    ];
    if (!steps[0]) return;
    steps.forEach((step) => step.classList.remove("active", "done"));
    if (hasApplied) {
      steps[0].classList.add("done");
      steps[1].classList.add("done");
      steps[2].classList.add("done");
    } else if (hasCandidates) {
      steps[0].classList.add("done");
      steps[1].classList.add("active");
    } else {
      steps[0].classList.add("active");
    }
  }

  function settings() {
    return {
      color: currentColor,
      opacity: Number(elements.opacity.value) / 100,
      mark_style: elements.markMethod.value,
      highlight_width: Number(elements.highlightWidth.value),
      work_shape_style: elements.workShapeStyle.value,
      work_line_width: Number(elements.workLineWidth.value),
      dimension_text: elements.dimensionText.value,
      dimension_show_leader: elements.dimensionShowLeader.checked,
      dimension_auto_style: elements.dimensionAutoStyle.checked,
      font_size: Number(elements.fontSize.value),
      stamp_name: elements.stampName.value,
      stamp_date: elements.stampDate.value,
      stamp_size: Number(elements.stampSize.value),
      procedure_note_type: elements.procedureNoteType.value,
      procedure_note_text: elements.procedureNoteText.value,
      procedure_note_size: Number(elements.procedureNoteSize.value),
      measurement_type: elements.measurementType.value,
      measurement_instrument: elements.measurementInstrument.value,
      measurement_sequence: Number(elements.measurementSequence.value),
      measurement_size: Number(elements.measurementSize.value),
      replacement_value: elements.replacementValue.value,
      upper_tolerance: elements.upperTolerance.value,
      lower_tolerance: elements.lowerTolerance.value,
      replacement_size: Number(elements.replacementSize.value),
      replacement_tolerance_size: Number(elements.replacementToleranceSize.value),
      replacement_value_x: replacementOffsets.valueX,
      replacement_value_y: replacementOffsets.valueY,
      replacement_tolerance_x: replacementOffsets.toleranceX,
      replacement_tolerance_y: replacementOffsets.toleranceY,
      general_tolerance_standard: elements.generalToleranceStandard.value,
      general_tolerance_grade: elements.generalToleranceGrade.value,
      general_tolerance_angle_length: Number(
        elements.generalToleranceAngleLength.value
      ),
    };
  }

  function busyMessage(method) {
    const messages = {
      scan_general_tolerances: [
        "対象寸法を検出しています",
        "画像解析・OCR・一般公差候補の整理を行っています",
      ],
      apply_general_tolerances: [
        "公差の配置を計算しています",
        "寸法線や文字との重なりを確認しています",
      ],
      scan_dimension_markings: [
        "色分け対象を検出しています",
        "寸法値と公差の候補を整理しています",
      ],
      apply_dimension_markings: [
        "色分けを反映しています",
        "選択した候補を図面へ追加しています",
      ],
      save_pdf: ["PDFを書き出しています", "編集内容を保存しています"],
      open_pdf: ["PDFを読み込んでいます", "ページを準備しています"],
    };
    return messages[method] || ["処理しています", "しばらくお待ちください"];
  }

  function setBusy(value, method = "") {
    busy = value;
    const [title, detail] = busyMessage(method);
    if (elements.processingTitle) elements.processingTitle.textContent = title;
    if (elements.processingDetail) elements.processingDetail.textContent = detail;
    [elements.open, elements.emptyOpen, elements.save, elements.previous,
      elements.next, elements.undo, elements.clearPage, elements.clearAll,
      elements.confirmReplacement, elements.cancelReplacement,
      elements.finishWorkShape, elements.removeWorkPoint,
      elements.cancelWorkShape, elements.confirmAutoWork,
      elements.cancelAutoWork, elements.autoWorkReplace,
      elements.autoWorkAdd, elements.autoWorkRemove,
      elements.predictWorkShape, elements.removeGuidedPoint,
      elements.resetGuidedPoints,
      elements.confirmWordCandidate, elements.cancelWordCandidate]
      .concat([
        elements.scanGeneralTolerance,
        elements.applyGeneralTolerance,
        elements.scanDimensionMarkings,
        elements.applyDimensionMarkings,
      ])
      .forEach((button) => { button.disabled = value; });
    document.body.classList.toggle("busy", value);
  }

  function showToast(message, isError = false) {
    if (!message) return;
    clearTimeout(toastTimer);
    elements.toast.textContent = message;
    elements.toast.classList.toggle("error", isError);
    elements.toast.classList.add("visible");
    toastTimer = setTimeout(() => elements.toast.classList.remove("visible"), 3200);
  }

  function setStatus(message, isError = false) {
    elements.status.classList.toggle("error", isError);
    elements.status.querySelector("span:last-child").textContent =
      message || "準備完了";
  }

  function updateCoach() {
    const loaded = Boolean(currentState.loaded);
    let step = "2";
    let title = "図面の塗りたい文字をクリック";
    let detail = toolInfo[currentMode]?.help || "";

    if (!loaded) {
      step = "1";
      title = "PDFを開く";
      detail = "画面へドロップするか「PDFを選択」を押します。";
    } else if (currentState.word_candidate) {
      step = "3";
      title = "範囲を確認";
      detail = "合っていれば「マークを確定」、違えば「やり直す」。";
    } else if (currentMode === "word") {
      const appliedCount = Number(
        currentState.general_tolerance_applied_count || 0
      );
      const markingCandidateCount = Number(
        currentState.dimension_marking_candidate_count || 0
      );
      if (removeDimensionMarkingMode && currentState.general_tolerance_marked) {
        title = "不要な色をクリック";
        detail = "ピンク・黄色のマーキング上を選びます。";
      } else if (markingCandidateCount > 0 && !currentState.general_tolerance_marked) {
        step = "2";
        title = "色分け候補を確認";
        detail = "ピンク・黄色＝対象、灰色＝除外。クリックで切替。";
      } else if (appliedCount > 0 && !currentState.general_tolerance_marked) {
        title = "対象寸法を検出";
        detail = "右の「対象寸法を検出」を押します。";
      } else if (currentState.general_tolerance_marked) {
        step = "3";
        title = "手動で追加・修正";
        detail = "文字をクリック。選べないときはドラッグ。";
      } else if (currentState.general_tolerance_checked) {
        title = "対象寸法を検出";
        detail = "未記載公差の寸法がなかったため、右の「対象寸法を検出」を押します。";
      } else {
        title = "先に①公差を反映";
        detail = "左の「公差未記載の寸法」を完了してください。";
      }
    } else if (currentMode === "work_shape") {
      const style = elements.workShapeStyle.value;
      if (guidedPredictionReady) {
        step = "3";
        title = "色の付いた形を確認して確定";
        detail = "合っていれば「この形で確定する」を押します。直したい場合だけ「形を少し直す」を開きます。";
      } else if (style === "guided") {
        if (workPoints.length < 3) {
          const remaining = 3 - workPoints.length;
          title = workPoints.length
            ? `続けて外形の角をクリック（あと${remaining}点以上）`
            : "外形の角を、ぐるっと順番にクリック";
          detail = "黒い外形線の角や曲がり目を3～32点クリックします。";
        } else {
          step = "3";
          title = "右の「形を予測する」を押す";
          detail = `${workPoints.length}点を指定しました。必要なら角を追加してから予測できます。`;
        }
      } else if (style === "auto") {
        title = currentState.work_region_candidate_count
          ? "色の付いた形を確認して確定"
          : "ワークの内側を1回クリック";
        detail = currentState.work_region_candidate_count
          ? "合っていれば右の「この形で確定する」を押します。"
          : "線やハッチングを避け、塗りたい部分の内側をクリックします。";
      } else {
        const minimum = style === "fill" ? 3 : 2;
        title = workPoints.length >= minimum
          ? "右の「この形で確定する」を押す"
          : `外形を順番にクリック（${minimum}点以上）`;
        detail = style === "fill"
          ? "ワークの面を囲むように、角を順番にクリックします。"
          : "塗りたい実線に沿って、点を順番にクリックします。";
      }
    } else if (currentMode === "replace") {
      if (currentState.replacement_selection) {
        step = "3";
        title = "入力後、青い枠をドラッグして配置";
        detail = "寸法値と公差を別々に移動できます。配置後に「修正を反映」を押します。";
      } else {
        title = "図面の直したい寸法をクリック";
        detail = currentState.has_text
          ? "元の寸法の真ん中を1回クリックします。"
          : "画像PDFでは、元の寸法をドラッグで四角く囲みます。";
      }
    } else if (currentMode === "general_tolerance") {
      const candidateCount = Number(
        currentState.general_tolerance_candidate_count || 0
      );
      if (candidateCount > 0) {
        step = "2";
        title = "候補を確認";
        detail = "水色＝反映、灰色＝除外。クリックで切替。";
      } else if (Number(currentState.general_tolerance_applied_count || 0) > 0) {
        step = "3";
        title = removeAppliedToleranceMode
          ? "不要な公差をクリック"
          : "公差を反映済み";
        detail = removeAppliedToleranceMode
          ? "寸法値または追加公差の近くを選びます。"
          : "次は左の「寸法を色分け」へ進みます。";
      } else {
        step = "1";
        title = "規格を選んで検出";
        detail = "右の「対象寸法を検出」を押します。";
      }
    } else if (currentMode === "strike") {
      title = "消したい寸法をクリック";
      detail = "必要な修正が終わったら「寸法・公差を色分けする」へ進みます。";
    } else if (currentMode === "dimension") {
      title = elements.dimensionShowLeader.checked
        ? "矢印の先から文字の場所までドラッグ"
        : "寸法文字を置く場所をクリック";
      detail = "追加後は青い枠で移動・サイズ変更できます。最後の一括色分けにも反映されます。";
    } else if (
      currentMode === "quality_stamp" ||
      currentMode === "process_stamp"
    ) {
      title = editableItemPreview?.mode === currentMode
        ? "青い枠で位置・サイズを調整"
        : "スタンプを置く場所をクリック";
      detail = editableItemPreview?.mode === currentMode
        ? "枠内をドラッグすると移動、右下の丸をドラッグするとサイズを変更できます。"
        : "配置済みの印をクリックすると、位置とサイズを変更できます。";
    } else if (currentMode === "procedure_note") {
      title = editableItemPreview?.mode === currentMode
        ? "青い枠で位置・サイズを調整"
        : "注記を置く場所をクリック";
      detail = editableItemPreview?.mode === currentMode
        ? "枠内をドラッグすると移動、右下の丸をドラッグすると文字サイズを変更できます。"
        : "配置済みの注記をクリックすると、位置と文字サイズを変更できます。";
    } else if (currentMode === "measurement") {
      title = "対象寸法の近くをクリック";
      detail = "右で測定具記号または測定順番号を選びます。";
    }

    elements.taskStep.textContent = `STEP ${step}`;
    elements.taskTitle.textContent = title;
    elements.modeHelp.textContent = detail;
    elements.canvasCoachStep.textContent = step;
    elements.canvasCoachText.textContent = title;
    elements.canvasCoach.classList.toggle("hidden", !loaded);
  }

  function updateWorkflowNavigation() {
    const toleranceDone =
      Number(currentState.general_tolerance_applied_count || 0) > 0;
    const toleranceChecked = Boolean(currentState.general_tolerance_checked);
    const correctionCount =
      Number(currentState.added_dimension_count || 0) +
      Number(currentState.replacement_dimension_count || 0) +
      Number(currentState.struck_dimension_count || 0);
    const marked = Boolean(currentState.general_tolerance_marked);
    const toleranceTool = $(".workflow-tool[data-mode='general_tolerance']");
    const markingTool = $(".workflow-tool[data-mode='word']");
    const workTool = $(".workflow-tool[data-mode='work_shape']");

    toleranceTool?.classList.toggle("complete", toleranceDone);
    elements.dimensionFixTools?.classList.toggle(
      "ready",
      toleranceDone && !marked
    );
    markingTool?.classList.toggle(
      "next-step",
      (toleranceDone || toleranceChecked) && !marked
    );
    markingTool?.classList.toggle("complete", marked);
    workTool?.classList.toggle("next-step", marked);

    if (elements.dimensionFixStatus) {
      elements.dimensionFixStatus.textContent = !toleranceDone
        ? "必要なときだけ"
        : marked
          ? "完了"
          : correctionCount > 0
            ? `${correctionCount}件修正済み`
            : "必要なときだけ";
    }

    const markingCaption = markingTool?.querySelector("small");
    if (markingCaption) {
      markingCaption.textContent = !toleranceDone
        ? "一括色分け"
        : marked
          ? "色分け済み"
          : correctionCount > 0
            ? `修正${correctionCount}件を含む`
            : "一括色分け";
    }

    if (
      elements.dimensionFixTools &&
      currentMode === "general_tolerance" &&
      toleranceDone &&
      !marked
    ) {
      elements.dimensionFixTools.open = true;
    }
  }

  function updateButtons() {
    const loaded = Boolean(currentState.loaded);
    const autoCandidateCount =
      Number(currentState.work_region_candidate_count || 0);
    const hasAutoCandidate = autoCandidateCount > 0;
    const hasWordCandidate = Boolean(currentState.word_candidate);
    const toleranceCandidateCount = Number(
      currentState.general_tolerance_candidate_count || 0
    );
    const toleranceSelectedCount = Number(
      currentState.general_tolerance_selected_count || 0
    );
    const toleranceManualCount = Number(
      currentState.general_tolerance_manual_count || 0
    );
    const toleranceAutomaticCount = Math.max(
      0,
      toleranceCandidateCount - toleranceManualCount
    );
    const hasToleranceCandidates = toleranceCandidateCount > 0;
    const markingCandidateCount = Number(
      currentState.dimension_marking_candidate_count || 0
    );
    const markingSelectedCount = Number(
      currentState.dimension_marking_selected_count || 0
    );
    const hasMarkingCandidates = markingCandidateCount > 0;
    const marked = Boolean(currentState.general_tolerance_marked);
    elements.save.disabled =
      busy || !loaded || hasAutoCandidate || hasWordCandidate ||
      hasToleranceCandidates || hasMarkingCandidates;
    elements.undo.disabled =
      busy || !loaded ||
      (!currentState.item_count && !hasAutoCandidate && !hasWordCandidate &&
        !hasToleranceCandidates && !hasMarkingCandidates);
    elements.clearPage.disabled = busy || !loaded || !currentState.page_item_count;
    elements.clearAll.disabled = busy || !loaded || !currentState.item_count;
    elements.previous.disabled = busy || !loaded || currentState.page_index <= 0;
    elements.next.disabled = busy || !loaded ||
      currentState.page_index >= currentState.page_count - 1;
    const replacementSelection = currentState.replacement_selection;
    elements.cancelReplacement.disabled = busy || !replacementSelection;
    elements.confirmReplacement.disabled = busy || !replacementSelection ||
      !elements.replacementValue.value.trim();
    const minimumWorkPoints =
      elements.workShapeStyle.value === "fill" ? 3 : 2;
    elements.finishWorkShape.disabled =
      busy || !loaded || workPoints.length < minimumWorkPoints;
    elements.removeWorkPoint.disabled =
      busy || !loaded || workPoints.length === 0;
    elements.cancelWorkShape.disabled =
      busy || !loaded || workPoints.length === 0;
    elements.predictWorkShape.disabled =
      busy || !loaded || workPoints.length < 3 || workPoints.length > 32;
    elements.removeGuidedPoint.disabled =
      busy || !loaded || workPoints.length === 0;
    elements.resetGuidedPoints.disabled =
      busy || !loaded || workPoints.length === 0;
    elements.confirmAutoWork.disabled =
      busy || !loaded || !hasAutoCandidate;
    elements.cancelAutoWork.disabled =
      busy || !loaded || !hasAutoCandidate;
    elements.confirmWordCandidate.disabled =
      busy || !loaded || !hasWordCandidate;
    elements.cancelWordCandidate.disabled =
      busy || !loaded || !hasWordCandidate;
    elements.scanGeneralTolerance.disabled = busy || !loaded;
    elements.applyGeneralTolerance.disabled =
      busy || !loaded || toleranceSelectedCount < 1;
    const appliedToleranceCount = Number(
      currentState.general_tolerance_applied_count || 0
    );
    const hasAppliedTolerance = appliedToleranceCount > 0;
    elements.removeAppliedTolerance?.classList.toggle(
      "hidden",
      !loaded || hasToleranceCandidates || !hasAppliedTolerance
    );
    elements.removeAppliedTolerance.disabled = busy || !loaded;
    elements.scanDimensionMarkings.disabled =
      busy || !loaded ||
      (!appliedToleranceCount && !currentState.general_tolerance_checked) || marked;
    elements.applyDimensionMarkings.disabled =
      busy || !loaded || markingSelectedCount < 1 || marked;
    elements.removeDimensionMarking?.classList.toggle(
      "hidden",
      !loaded || !marked
    );
    elements.removeDimensionMarking.disabled = busy || !loaded;
    elements.dimensionMarkingCandidateCount.textContent = marked
      ? `${appliedToleranceCount}件 反映済み`
      : hasMarkingCandidates
        ? `${markingSelectedCount} / ${markingCandidateCount}件`
        : appliedToleranceCount > 0
          ? "未検出"
          : "—";
    elements.dimensionMarkingHint.textContent =
      removeDimensionMarkingMode && marked
        ? "解除モード中。不要な色をクリックしてください。"
        : marked
        ? "色分け済み。不要な色は上のボタンで解除できます。"
        : hasMarkingCandidates
          ? "ピンク・黄色＝対象、灰色＝除外。クリックで切替。"
          : appliedToleranceCount > 0
            ? "①検出 → ②クリックで確認 → ③一括反映"
            : "先に①で公差を反映してください。";
    updateMarkingFlowSteps();
    elements.generalToleranceCandidateCount.textContent = hasToleranceCandidates
      ? toleranceManualCount > 0
        ? `${toleranceSelectedCount} / ${toleranceAutomaticCount}件（要確認 ${toleranceManualCount}）`
        : `${toleranceSelectedCount} / ${toleranceAutomaticCount}件`
      : hasAppliedTolerance
        ? `${appliedToleranceCount}件 反映済み`
        : "未検出";
    elements.generalToleranceHint.textContent = hasToleranceCandidates
      ? toleranceManualCount > 0
        ? "水色＝反映、灰色＝除外。オレンジは個別確認。"
        : "水色＝反映、灰色＝除外。クリックで切替。"
      : hasAppliedTolerance
        ? removeAppliedToleranceMode
          ? "解除モード中。不要な箇所をクリックしてください。"
          : "反映済み。次は③の色分けへ。不要な公差は上のボタンで解除。"
        : "①検出 → ②クリックで確認 → ③一括反映";
    updateToleranceFlowSteps();
    elements.wordCandidateBar.classList.toggle(
      "hidden",
      !hasWordCandidate
    );
    elements.viewer.classList.toggle("candidate-pending", hasWordCandidate);
    elements.canvas.setAttribute(
      "aria-disabled",
      hasWordCandidate ? "true" : "false"
    );
    const candidateAngle = Number(currentState.word_candidate_angle);
    elements.wordCandidateOrientation.textContent =
      hasWordCandidate && Number.isFinite(candidateAngle) &&
      Math.abs(candidateAngle) >= 3
        ? `寸法値の向き（約${Math.round(Math.abs(candidateAngle))}°）に合わせて斜めにマーキングします`
        : "寸法値の向きに合わせてマーキングします";
    elements.autoWorkCandidateCount.textContent = hasAutoCandidate
      ? `${autoCandidateCount}か所を確認中`
      : "未選択";
    updateWorkflowNavigation();
    updateCoach();
  }

  function updateStageSize() {
    if (!currentState.loaded) return;
    const available = Math.max(360, elements.viewport.clientWidth - 72);
    const fitWidth = Math.min(Number(currentState.pdf_width), available);
    const zoom = Number(elements.zoomRange.value) / 100;
    const width = Math.max(120, fitWidth * zoom);
    const height = width * Number(currentState.pdf_height) / Number(currentState.pdf_width);
    elements.stage.style.width = `${width}px`;
    elements.stage.style.height = `${height}px`;
    elements.canvas.width = Math.round(width);
    elements.canvas.height = Math.round(height);
    clearInteraction();
  }

  function receiveState(state, announce = true) {
    if (!state) return;
    if (!state.ok) {
      setStatus(state.message || "処理に失敗しました。", true);
      if (!state.cancelled) showToast(state.message || "処理に失敗しました。", true);
      setBusy(false);
      updateButtons();
      return;
    }

    const hadWordCandidate = Boolean(currentState.word_candidate);
    const previousSelectionKey =
      currentState.replacement_selection?.selection_key;
    currentState = state;
    editableItemPreview = state.editable_item_selection
      ? {
          ...state.editable_item_selection,
          rect: [...state.editable_item_selection.rect],
        }
      : null;
    document.body.classList.toggle("document-loaded", Boolean(state.loaded));
    const dimensionStyle = state.dimension_style;
    if (dimensionStyle) {
      const detectedSize = Number(dimensionStyle.font_size);
      const detectedFont = String(dimensionStyle.font_name || "MS-PGothic");
      if (elements.dimensionAutoStyle.checked && Number.isFinite(detectedSize)) {
        elements.fontSize.value = detectedSize.toFixed(1);
      }
      dimensionFontFamily = `'${detectedFont.replaceAll("-", " ")}'`;
      dimensionLineWidth = Number(dimensionStyle.line_width) || 0.35;
      elements.dimensionStyleStatus.textContent =
        `${detectedFont}・約${detectedSize.toFixed(1)}pt・細線`;
    }
    const hasWorkCandidate =
      Number(state.work_region_candidate_count || 0) > 0;
    if (guidedPredictionRequested) {
      guidedPredictionReady = hasWorkCandidate;
      guidedPredictionRequested = false;
      if (guidedPredictionReady) {
        workPoints = [];
        workAutoOperation = "add";
      }
    } else if (guidedPredictionReady && !hasWorkCandidate) {
      guidedPredictionReady = false;
    }
    const replacementSelection = state.replacement_selection;
    if (
      replacementSelection &&
      replacementSelection.selection_key !== previousSelectionKey
    ) {
      if (replacementSelection.original_value) {
        elements.replacementValue.value = replacementSelection.original_value;
      }
      const sourceSize = Number(replacementSelection.font_size) || 9;
      elements.replacementSize.value = String(sourceSize);
      elements.replacementToleranceSize.value = Math.max(
        4,
        sourceSize * .8
      ).toFixed(2);
      replacementOffsets = {
        valueX: 0,
        valueY: 0,
        toleranceX: 0,
        toleranceY: 0,
      };
      updateReplacementPreview();
    }
    elements.originalReplacementValue.textContent = replacementSelection
      ? replacementSelection.original_text
      : "未選択";
    elements.replacementSelectionHint.textContent = replacementSelection
      ? replacementSelection.has_text
        ? `選択範囲を青く表示中・元サイズ ${replacementSelection.font_size}pt（変更できます）`
        : "画像範囲を選択中です。文字サイズは下の欄で指定できます"
      : "図面上の修正したい寸法値を選択してください";
    if (state.today && !elements.stampDate.value) {
      elements.stampDate.value = state.today;
    }
    elements.empty.classList.toggle("hidden", Boolean(state.loaded));
    elements.viewport.classList.toggle("hidden", !state.loaded);
    elements.open.classList.toggle("hidden", !state.loaded);

    if (state.loaded) {
      elements.fileName.textContent = state.file_name;
      const kind = state.has_text
        ? "文字情報あり"
        : state.page_uses_local_ocr
          ? "画像PDF（高解像度OCR）"
          : "画像PDF・アウトラインPDF（クリック自動選択対応）";
      elements.fileMeta.textContent =
        `${kind} ・ このページの追加 ${state.page_item_count}件`;
      elements.pageLabel.textContent = `${state.page_number} / ${state.page_count}`;
      elements.image.onload = updateStageSize;
      elements.image.src = state.image;
      if (elements.image.complete) updateStageSize();
    } else {
      elements.fileName.textContent = "PDF未選択";
      elements.fileMeta.textContent = "画面へドラッグ＆ドロップできます";
      elements.pageLabel.textContent = "— / —";
    }

    setStatus(state.message || "準備完了");

    if (elements.buildLabel) {
      const buildId = state.build_id || "不明";
      const ocrState = state.local_ocr_ready ? "OCR有効" : "OCR無効";
      elements.buildLabel.textContent = `${buildId} / ${ocrState}`;
      elements.buildLabel.title = `ビルド ${buildId} ・ ローカルOCR ${ocrState}`;
    }
    const needsDecision = Boolean(
      state.word_candidate || state.work_region_candidate_count ||
      state.general_tolerance_candidate_count
    );
    if (announce && state.message && !needsDecision) showToast(state.message);
    setBusy(false);
    updateSpecialControls();
    if (state.word_candidate && !hadWordCandidate) {
      elements.confirmWordCandidate.focus({ preventScroll: true });
    }
  }

  async function callApi(method, ...args) {
    if (busy) return;
    setBusy(true, method);
    updateButtons();
    try {
      const response = await fetch("/api", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Drawing-Assist-Token": apiToken,
        },
        body: JSON.stringify({
          action: method,
          arguments: args,
        }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      receiveState(result);
    } catch (error) {
      setBusy(false);
      setStatus(`処理に失敗しました: ${error}`, true);
      showToast("処理に失敗しました。", true);
      updateButtons();
    }
  }

  async function uploadPdf(file) {
    if (!file || busy) return;
    resetWorkShape();
    const isPdf = file.name.toLowerCase().endsWith(".pdf") ||
      file.type === "application/pdf";
    if (!isPdf) {
      showToast("PDFファイルを選択してください。", true);
      return;
    }
    setBusy(true, "open_pdf");
    updateButtons();
    setStatus("PDFを読み込んでいます…");
    try {
      const response = await fetch(
        `/upload?name=${encodeURIComponent(file.name)}`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/pdf",
            "X-Drawing-Assist-Token": apiToken,
          },
          body: file,
        }
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const state = await response.json();
      receiveState(state);
      if (state.loaded) {
        // 新しいPDFでは未記載公差のメニューだけを選択する。検出は利用者が
        // 「公差未記載寸法を検出」を押したときに開始し、読込直後には実行しない。
        selectTool("general_tolerance");
      }
    } catch (error) {
      setBusy(false);
      setStatus(`PDFを開けませんでした: ${error}`, true);
      showToast("PDFを開けませんでした。", true);
      updateButtons();
    } finally {
      elements.pdfFileInput.value = "";
    }
  }

  function choosePdf() {
    if (!busy) elements.pdfFileInput.click();
  }

  function selectTool(mode) {
    if (busy) return;
    if (!toolInfo[mode]) return;
    if (
      currentMode === "word" &&
      mode !== "word" &&
      currentState.word_candidate
    ) {
      callApi("cancel_word_candidate");
    }
    if (currentMode === "work_shape" && mode !== "work_shape") {
      resetWorkShape();
      if (currentState.work_region_candidate_count) {
        callApi("cancel_work_region");
      }
    }
    if (
      currentMode === "general_tolerance" &&
      mode !== "general_tolerance" &&
      currentState.general_tolerance_candidate_count
    ) {
      callApi("cancel_general_tolerance_candidates");
    }
    if (
      currentMode === "word" &&
      mode !== "word" &&
      currentState.dimension_marking_candidate_count
    ) {
      callApi("cancel_dimension_marking_candidates");
    }
    if (mode !== "general_tolerance") {
      setRemoveAppliedToleranceMode(false);
    }
    if (mode !== "word") {
      setRemoveDimensionMarkingMode(false);
    }
    currentMode = mode;
    const mainModes = ["general_tolerance", "word", "work_shape"];
    const correctionModes = ["dimension", "replace", "strike"];
    const moreTools = document.querySelector(".more-tools");
    if (moreTools) {
      moreTools.open =
        !mainModes.includes(mode) && !correctionModes.includes(mode);
    }
    if (elements.dimensionFixTools) {
      elements.dimensionFixTools.open = correctionModes.includes(mode);
    }
    if (!["quality_stamp", "process_stamp", "procedure_note"].includes(mode)) {
      const stampGroup = document.querySelector(".workflow-stamps");
      if (stampGroup) stampGroup.open = false;
    } else {
      const stampGroup = document.querySelector(".workflow-stamps");
      if (stampGroup) stampGroup.open = true;
    }
    $$(".tool-card").forEach((button) =>
      button.classList.toggle("active", button.dataset.mode === mode));
    $$("[data-for]").forEach((section) => {
      const modes = section.dataset.for.split(/\s+/);
      section.classList.toggle("hidden", !modes.includes(mode));
    });
    elements.toolName.textContent = toolInfo[mode].name;
    elements.modeHelp.textContent = toolInfo[mode].help;
    elements.canvas.style.cursor = toolInfo[mode].cursor;
    clearInteraction();
    updateCoach();
  }

  function canvasPoint(event) {
    const rect = elements.canvas.getBoundingClientRect();
    return {
      screenX: event.clientX - rect.left,
      screenY: event.clientY - rect.top,
      x: (event.clientX - rect.left) * currentState.pdf_width / rect.width,
      y: (event.clientY - rect.top) * currentState.pdf_height / rect.height,
    };
  }

  function pdfPointToScreen(point) {
    return {
      screenX: point.x * elements.canvas.width / currentState.pdf_width,
      screenY: point.y * elements.canvas.height / currentState.pdf_height,
    };
  }

  function clearInteraction() {
    dragStart = null;
    dragCurrent = null;
    replacementDrag = null;
    editableItemDrag = null;
    drawInteraction();
  }

  function updateSpecialControls() {
    const style = elements.workShapeStyle.value;
    const autoMode = style === "auto";
    const guidedMode = style === "guided";
    const correctionMode = autoMode || (guidedMode && guidedPredictionReady);
    elements.angledWidthGroup.classList.toggle(
      "hidden",
      elements.markMethod.value !== "angled"
    );
    elements.workPointCount.textContent = `${workPoints.length}点`;
    elements.guidedWorkPointCount.textContent = `${workPoints.length} / 32点`;
    elements.guidedWorkControls.classList.toggle(
      "hidden",
      !guidedMode || guidedPredictionReady
    );
    elements.autoWorkControls.classList.toggle("hidden", !correctionMode);
    elements.workCorrectionGuide.classList.toggle(
      "hidden",
      !guidedMode || !guidedPredictionReady
    );
    elements.manualWorkControls.classList.toggle(
      "hidden",
      autoMode || guidedMode
    );
    elements.workLineWidthGroup.classList.toggle(
      "hidden",
      elements.workShapeStyle.value !== "line"
    );
    elements.dimensionManualStyle.classList.toggle(
      "hidden",
      elements.dimensionAutoStyle.checked
    );
    [
      ["replace", elements.autoWorkReplace],
      ["add", elements.autoWorkAdd],
      ["remove", elements.autoWorkRemove],
    ].forEach(([operation, button]) =>
      button.classList.toggle("active", workAutoOperation === operation));
    updateButtons();
  }

  function resetWorkShape() {
    workPoints = [];
    guidedPredictionRequested = false;
    guidedPredictionReady = false;
    updateSpecialControls();
    drawInteraction();
  }

  function predictWorkShape() {
    if (busy || !currentState.loaded || workPoints.length < 3) {
      showToast("ワーク外形の角・変曲点を順番に3点以上指定してください。", true);
      return;
    }
    if (workPoints.length > 32) {
      showToast("指定点は32点以内にしてください。", true);
      return;
    }
    guidedPredictionRequested = true;
    callApi(
      "predict_work_shape",
      {
        points: workPoints.map((point) => ({ x: point.x, y: point.y })),
      },
      settings()
    );
  }

  function finishWorkShape() {
    if (elements.workShapeStyle.value === "guided") {
      predictWorkShape();
      return;
    }
    const minimumPoints =
      elements.workShapeStyle.value === "fill" ? 3 : 2;
    if (busy || !currentState.loaded || workPoints.length < minimumPoints) {
      showToast(
        elements.workShapeStyle.value === "fill"
          ? "面を囲むには3点以上を指定してください。"
          : "実線をなぞるには2点以上を指定してください。",
        true
      );
      return;
    }
    const payload = {
      points: workPoints.map((point) => ({ x: point.x, y: point.y })),
    };
    workPoints = [];
    updateSpecialControls();
    drawInteraction();
    callApi("apply_action", "work_shape", payload, settings());
  }

  function transformedRectangle(origin, angle, x0, y0, x1, y1) {
    const cosine = Math.cos(angle);
    const sine = Math.sin(angle);
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map(
      ([x, y]) => ({
        x: origin.x + x * cosine - y * sine,
        y: origin.y + x * sine + y * cosine,
      })
    );
  }

  function pointInScreenPolygon(point, polygon) {
    if (!polygon) return false;
    let inside = false;
    let previous = polygon[polygon.length - 1];
    polygon.forEach((current) => {
      if ((current.y > point.screenY) !== (previous.y > point.screenY)) {
        const intersectionX = (previous.x - current.x) *
          (point.screenY - current.y) / (previous.y - current.y) + current.x;
        if (point.screenX < intersectionX) inside = !inside;
      }
      previous = current;
    });
    return inside;
  }

  function replacementPartAt(point) {
    if (pointInScreenPolygon(point, replacementHitAreas.toleranceResize)) return "toleranceResize";
    if (pointInScreenPolygon(point, replacementHitAreas.valueResize)) return "valueResize";
    if (pointInScreenPolygon(point, replacementHitAreas.tolerance)) return "tolerance";
    if (pointInScreenPolygon(point, replacementHitAreas.value)) return "value";
    return null;
  }

  function drawReplacementResizeHandle(context, origin, angle, bounds, name) {
    const handleSize = 11;
    const centerX = bounds[2];
    const centerY = bounds[3];
    context.save();
    context.translate(origin.x, origin.y);
    context.rotate(angle);
    context.setLineDash([]);
    context.fillStyle = "#2563eb";
    context.strokeStyle = "#ffffff";
    context.lineWidth = 1.5;
    context.fillRect(
      centerX - handleSize / 2,
      centerY - handleSize / 2,
      handleSize,
      handleSize
    );
    context.strokeRect(
      centerX - handleSize / 2,
      centerY - handleSize / 2,
      handleSize,
      handleSize
    );
    context.restore();
    replacementHitAreas[name] = transformedRectangle(
      origin,
      angle,
      centerX - handleSize,
      centerY - handleSize,
      centerX + handleSize,
      centerY + handleSize
    );
  }

  function drawReplacementCanvasPreview(context) {
    replacementHitAreas = {
      value: null,
      tolerance: null,
      valueResize: null,
      toleranceResize: null,
    };
    const selection = currentState.replacement_selection;
    if (currentMode !== "replace" || !selection) return;

    const scaleX = elements.canvas.width / Number(currentState.pdf_width);
    const scaleY = elements.canvas.height / Number(currentState.pdf_height);
    const scale = (scaleX + scaleY) / 2;
    const whiteout = selection.whiteout_rect || selection.rect;
    if (whiteout?.length === 4) {
      context.save();
      context.fillStyle = "rgba(255,255,255,.96)";
      context.fillRect(
        whiteout[0] * scaleX,
        whiteout[1] * scaleY,
        (whiteout[2] - whiteout[0]) * scaleX,
        (whiteout[3] - whiteout[1]) * scaleY
      );
      context.restore();
    }

    const sourceDirection = selection.direction || [1, 0];
    const directionLength = Math.hypot(sourceDirection[0], sourceDirection[1]) || 1;
    const direction = {
      x: sourceDirection[0] / directionLength,
      y: sourceDirection[1] / directionLength,
    };
    const angle = Math.atan2(direction.y * scaleY, direction.x * scaleX);
    const normal = { x: -direction.y, y: direction.x };
    const sourceOrigin = selection.origin || [selection.rect[0], selection.rect[3]];
    const valueOrigin = {
      x: (sourceOrigin[0] + replacementOffsets.valueX) * scaleX,
      y: (sourceOrigin[1] + replacementOffsets.valueY) * scaleY,
    };
    const value = elements.replacementValue.value || "新しい寸法";
    const valueFont = Math.max(5, Number(elements.replacementSize.value) || 9);
    const toleranceFont = Math.max(
      4,
      Number(elements.replacementToleranceSize.value) || valueFont * .8
    );
    const family = String(selection.font_name || "MS PGothic").replaceAll("-", " ");
    const valuePixels = valueFont * scale;
    const tolerancePixels = toleranceFont * scale;

    context.save();
    context.translate(valueOrigin.x, valueOrigin.y);
    context.rotate(angle);
    context.font = `${valuePixels}px '${family}', sans-serif`;
    context.textBaseline = "alphabetic";
    const valueWidth = context.measureText(value).width;
    context.lineJoin = "round";
    context.lineWidth = Math.max(2, valuePixels * .08);
    context.strokeStyle = "rgba(255,255,255,.96)";
    context.strokeText(value, 0, 0);
    context.fillStyle = "#111827";
    context.fillText(value, 0, 0);
    const valueBounds = [
      -5,
      -valuePixels * 1.12 - 4,
      valueWidth + 5,
      valuePixels * .30 + 4,
    ];
    context.setLineDash([5, 4]);
    context.strokeStyle = "#2563eb";
    context.lineWidth = 1.5;
    context.strokeRect(
      valueBounds[0], valueBounds[1],
      valueBounds[2] - valueBounds[0], valueBounds[3] - valueBounds[1]
    );
    context.restore();
    replacementHitAreas.value = transformedRectangle(
      valueOrigin,
      angle,
      ...valueBounds
    );
    drawReplacementResizeHandle(
      context,
      valueOrigin,
      angle,
      valueBounds,
      "valueResize"
    );

    const upper = elements.upperTolerance.value;
    const lower = elements.lowerTolerance.value;
    if (!upper && !lower) return;
    const valueWidthPdf = valueWidth / scale;
    const toleranceOrigin = {
      x: (sourceOrigin[0] + direction.x * (valueWidthPdf + .5) +
        replacementOffsets.toleranceX) * scaleX,
      y: (sourceOrigin[1] + direction.y * (valueWidthPdf + .5) +
        replacementOffsets.toleranceY) * scaleY,
    };
    context.save();
    context.translate(toleranceOrigin.x, toleranceOrigin.y);
    context.rotate(angle);
    context.font = `${tolerancePixels}px '${family}', sans-serif`;
    context.textBaseline = "alphabetic";
    context.lineJoin = "round";
    context.lineWidth = Math.max(2, tolerancePixels * .08);
    context.strokeStyle = "rgba(255,255,255,.96)";
    let maximumWidth = 0;
    if (upper) {
      maximumWidth = Math.max(maximumWidth, context.measureText(upper).width);
      context.strokeText(upper, 0, -tolerancePixels);
    }
    if (lower) {
      maximumWidth = Math.max(maximumWidth, context.measureText(lower).width);
      context.strokeText(lower, 0, 0);
    }
    context.fillStyle = "#111827";
    if (upper) context.fillText(upper, 0, -tolerancePixels);
    if (lower) context.fillText(lower, 0, 0);
    const toleranceBounds = [
      -5,
      upper ? -tolerancePixels * 2.10 - 4 : -tolerancePixels * 1.10 - 4,
      maximumWidth + 5,
      lower ? tolerancePixels * .30 + 4 : -tolerancePixels * .70 + 4,
    ];
    context.setLineDash([5, 4]);
    context.strokeStyle = "#2563eb";
    context.lineWidth = 1.5;
    context.strokeRect(
      toleranceBounds[0], toleranceBounds[1],
      toleranceBounds[2] - toleranceBounds[0],
      toleranceBounds[3] - toleranceBounds[1]
    );
    context.restore();
    replacementHitAreas.tolerance = transformedRectangle(
      toleranceOrigin,
      angle,
      ...toleranceBounds
    );
    drawReplacementResizeHandle(
      context,
      toleranceOrigin,
      angle,
      toleranceBounds,
      "toleranceResize"
    );
  }

  function editableSelectionScreenRect() {
    const selection = editableItemPreview;
    if (!selection || selection.mode !== currentMode || !currentState.loaded) {
      return null;
    }
    const rect = selection.rect;
    const topLeft = pdfPointToScreen({ x: rect[0], y: rect[1] });
    const bottomRight = pdfPointToScreen({ x: rect[2], y: rect[3] });
    return {
      x0: topLeft.screenX,
      y0: topLeft.screenY,
      x1: bottomRight.screenX,
      y1: bottomRight.screenY,
    };
  }

  function editablePartAt(point) {
    const rect = editableSelectionScreenRect();
    if (!rect) return null;
    if (editableItemPreview?.target_movable && editableItemPreview?.target) {
      const target = pdfPointToScreen({
        x: editableItemPreview.target[0],
        y: editableItemPreview.target[1],
      });
      if (Math.hypot(point.screenX - target.screenX, point.screenY - target.screenY) <= 13) {
        return "target";
      }
    }
    if (editableItemPreview?.move_only) {
      return (
        point.screenX >= rect.x0 - 6 && point.screenX <= rect.x1 + 6 &&
        point.screenY >= rect.y0 - 6 && point.screenY <= rect.y1 + 6
      ) ? "move" : null;
    }
    const handleRadius = 12;
    if (Math.hypot(point.screenX - rect.x1, point.screenY - rect.y1) <= handleRadius) {
      return "resize";
    }
    if (
      point.screenX >= rect.x0 - 4 && point.screenX <= rect.x1 + 4 &&
      point.screenY >= rect.y0 - 4 && point.screenY <= rect.y1 + 4
    ) {
      return "move";
    }
    return null;
  }

  function drawEditableItemSelection(context) {
    const rect = editableSelectionScreenRect();
    if (!rect) return;
    context.save();
    context.strokeStyle = "#2563eb";
    context.fillStyle = "#2563eb";
    context.lineWidth = 2;
    context.setLineDash([7, 4]);
    context.strokeRect(rect.x0, rect.y0, rect.x1 - rect.x0, rect.y1 - rect.y0);
    if (editableItemPreview?.target_movable && editableItemPreview?.target) {
      const target = pdfPointToScreen({
        x: editableItemPreview.target[0],
        y: editableItemPreview.target[1],
      });
      const anchorX = Math.max(rect.x0, Math.min(target.screenX, rect.x1));
      const anchorY = Math.max(rect.y0, Math.min(target.screenY, rect.y1));
      context.beginPath();
      context.moveTo(target.screenX, target.screenY);
      context.lineTo(anchorX, anchorY);
      context.stroke();
      context.setLineDash([]);
      context.beginPath();
      context.arc(target.screenX, target.screenY, 7, 0, Math.PI * 2);
      context.fill();
      context.strokeStyle = "#ffffff";
      context.lineWidth = 1.5;
      context.stroke();
      context.strokeStyle = "#2563eb";
      context.setLineDash([7, 4]);
    }
    if (editableItemPreview?.move_only) {
      context.restore();
      return;
    }
    context.setLineDash([]);
    context.beginPath();
    context.arc(rect.x1, rect.y1, 7, 0, Math.PI * 2);
    context.fill();
    context.strokeStyle = "#ffffff";
    context.lineWidth = 1.5;
    context.stroke();
    context.restore();
  }

  function drawInteraction() {
    const context = elements.canvas.getContext("2d");
    context.clearRect(0, 0, elements.canvas.width, elements.canvas.height);
    drawReplacementCanvasPreview(context);
    drawEditableItemSelection(context);
    if (currentMode === "work_shape" && workPoints.length) {
      const screenPoints = workPoints.map(pdfPointToScreen);
      const guidedMode = elements.workShapeStyle.value === "guided";
      context.save();
      context.lineWidth = Math.max(
        2,
        elements.workShapeStyle.value === "line"
          ? Number(elements.workLineWidth.value) *
            elements.canvas.width / currentState.pdf_width
          : 2
      );
      context.lineJoin = "round";
      context.lineCap = "round";
      context.strokeStyle = currentColor;
      context.fillStyle = `${currentColor}55`;
      if (guidedMode) {
        context.globalAlpha = 0.8;
        context.setLineDash([7, 5]);
        context.beginPath();
        context.moveTo(screenPoints[0].screenX, screenPoints[0].screenY);
        screenPoints.slice(1).forEach((point) =>
          context.lineTo(point.screenX, point.screenY));
        if (workPoints.length >= 3) context.closePath();
        context.stroke();
        context.setLineDash([]);
        context.globalAlpha = 1;
      } else {
        context.beginPath();
        context.moveTo(screenPoints[0].screenX, screenPoints[0].screenY);
        screenPoints.slice(1).forEach((point) =>
          context.lineTo(point.screenX, point.screenY));
        if (
          elements.workShapeStyle.value === "fill" &&
          workPoints.length >= 3
        ) {
          context.closePath();
          context.fill();
        }
        context.stroke();
      }
      context.fillStyle = "#1746ad";
      screenPoints.forEach((point, index) => {
        context.beginPath();
        context.arc(
          point.screenX,
          point.screenY,
          guidedMode ? 8 : 4,
          0,
          Math.PI * 2
        );
        context.fill();
        context.fillStyle = "#ffffff";
        context.font = `bold ${guidedMode ? 10 : 8}px 'Yu Gothic UI'`;
        context.textAlign = "center";
        context.fillText(
          String(index + 1),
          point.screenX,
          point.screenY + (guidedMode ? 4 : 3)
        );
        context.fillStyle = "#1746ad";
      });
      context.restore();
    }
    if (!dragStart || !dragCurrent) return;

    context.save();
    context.lineWidth = 2;
    context.strokeStyle =
      currentMode === "dimension" ? "#2563eb" : currentColor;
    context.setLineDash([7, 5]);

    if (
      currentMode === "word" &&
      elements.markMethod.value === "angled"
    ) {
      const dx = dragCurrent.screenX - dragStart.screenX;
      const dy = dragCurrent.screenY - dragStart.screenY;
      const length = Math.hypot(dx, dy) || 1;
      const widthScale = elements.canvas.width / currentState.pdf_width;
      const halfWidth = Number(elements.highlightWidth.value) * widthScale / 2;
      const nx = -dy / length * halfWidth;
      const ny = dx / length * halfWidth;
      const points = [
        [dragStart.screenX + nx, dragStart.screenY + ny],
        [dragCurrent.screenX + nx, dragCurrent.screenY + ny],
        [dragCurrent.screenX - nx, dragCurrent.screenY - ny],
        [dragStart.screenX - nx, dragStart.screenY - ny],
      ];
      context.fillStyle = `${currentColor}66`;
      context.beginPath();
      context.moveTo(points[0][0], points[0][1]);
      points.slice(1).forEach((point) => context.lineTo(point[0], point[1]));
      context.closePath();
      context.fill();
      context.stroke();
    } else if (
      currentMode === "word" ||
      currentMode === "replace"
    ) {
      const x = Math.min(dragStart.screenX, dragCurrent.screenX);
      const y = Math.min(dragStart.screenY, dragCurrent.screenY);
      const width = Math.abs(dragCurrent.screenX - dragStart.screenX);
      const height = Math.abs(dragCurrent.screenY - dragStart.screenY);
      context.fillStyle = currentMode === "replace"
        ? "rgba(255,255,255,.82)"
        : `${currentColor}66`;
      context.fillRect(x, y, width, height);
      context.strokeRect(x, y, width, height);
      if (currentMode === "replace") {
        context.setLineDash([]);
        context.fillStyle = "#2563eb";
        context.font = "bold 12px 'Yu Gothic UI'";
        context.fillText(elements.replacementValue.value || "新しい寸法", x + 4, y + 15);
      }
    } else {
      const drawingScale = elements.canvas.width / currentState.pdf_width;
      const dimensionFontPixels = Math.max(
        8,
        Number(elements.fontSize.value) * drawingScale
      );
      const arrowLength = Math.max(4, dimensionFontPixels * .48);
      context.lineWidth = Math.max(1, dimensionLineWidth * drawingScale);
      context.setLineDash([]);
      if (elements.dimensionShowLeader.checked) {
        context.beginPath();
        context.moveTo(dragStart.screenX, dragStart.screenY);
        context.lineTo(dragCurrent.screenX, dragCurrent.screenY);
        context.stroke();
      }
      const angle = Math.atan2(
        dragCurrent.screenY - dragStart.screenY,
        dragCurrent.screenX - dragStart.screenX
      );
      if (elements.dimensionShowLeader.checked) {
        context.fillStyle = "#2563eb";
        context.beginPath();
        context.moveTo(dragStart.screenX, dragStart.screenY);
        context.lineTo(
          dragStart.screenX + Math.cos(angle - .45) * arrowLength,
          dragStart.screenY + Math.sin(angle - .45) * arrowLength
        );
        context.lineTo(
          dragStart.screenX + Math.cos(angle + .45) * arrowLength,
          dragStart.screenY + Math.sin(angle + .45) * arrowLength
        );
        context.closePath();
        context.fill();
      }
      const label = elements.dimensionText.value || "寸法値";
      context.font = `${dimensionFontPixels}px ${dimensionFontFamily}, sans-serif`;
      const width = context.measureText(label).width + 10;
      context.fillStyle = currentColor;
      context.fillRect(
        dragCurrent.screenX,
        dragCurrent.screenY,
        width,
        dimensionFontPixels * 1.55
      );
      context.fillStyle = "#111827";
      context.fillText(
        label,
        dragCurrent.screenX + 5,
        dragCurrent.screenY + dimensionFontPixels * 1.12
      );
    }
    context.restore();
  }

  function onPointerDown(event) {
    if (!currentState.loaded || busy || event.button !== 0) return;
    if (currentState.word_candidate) {
      elements.confirmWordCandidate.focus({ preventScroll: true });
      return;
    }
    const point = canvasPoint(event);
    if (currentMode === "dimension" || currentMode === "replace") {
      const editablePart = editablePartAt(point);
      if (editablePart) {
        editableItemDrag = {
          part: editablePart,
          start: point,
          initialRect: [...editableItemPreview.rect],
          initialTarget: editableItemPreview.target
            ? [...editableItemPreview.target]
            : null,
        };
        elements.canvas.setPointerCapture(event.pointerId);
        elements.canvas.style.cursor = editablePart === "target"
          ? "crosshair"
          : editablePart === "resize"
            ? "nwse-resize"
            : "grabbing";
        return;
      }
    }
    if (
      ["quality_stamp", "process_stamp", "procedure_note"].includes(currentMode) ||
      (
        currentMode === "general_tolerance" &&
        !Number(currentState.general_tolerance_candidate_count || 0) &&
        Number(currentState.general_tolerance_applied_count || 0) > 0
      )
    ) {
      const editablePart = editablePartAt(point);
      if (editablePart) {
        editableItemDrag = {
          part: editablePart,
          start: point,
          initialRect: [...editableItemPreview.rect],
        };
        elements.canvas.setPointerCapture(event.pointerId);
        elements.canvas.style.cursor = editablePart === "resize" ? "nwse-resize" : "grabbing";
        return;
      }
      if (currentMode === "general_tolerance") {
        if (removeAppliedToleranceMode) {
          callApi("remove_applied_general_tolerance", { x: point.x, y: point.y });
        } else {
          callApi("select_general_tolerance_addition", { x: point.x, y: point.y });
        }
      } else {
        callApi(
          "apply_action",
          currentMode,
          { x: point.x, y: point.y, select_existing: true },
          settings()
        );
      }
      return;
    }
    if (currentMode === "replace" && currentState.replacement_selection) {
      const replacementPart = replacementPartAt(point);
      if (replacementPart) {
        replacementDrag = {
          part: replacementPart,
          start: point,
          initial: { ...replacementOffsets },
          initialValueSize: Number(elements.replacementSize.value) || 9,
          initialToleranceSize: Number(elements.replacementToleranceSize.value) || 7,
        };
        elements.canvas.setPointerCapture(event.pointerId);
        elements.canvas.style.cursor = replacementPart.endsWith("Resize")
          ? "nwse-resize"
          : "grabbing";
        return;
      }
    }
    if (currentMode === "work_shape") {
      const style = elements.workShapeStyle.value;
      if (
        style === "auto" ||
        (style === "guided" && guidedPredictionReady)
      ) {
        callApi(
          "detect_work_region",
          {
            x: point.x,
            y: point.y,
            operation: workAutoOperation,
          },
          settings()
        );
        return;
      }
      if (style === "guided" && workPoints.length >= 32) {
        showToast("指定できる点は32点までです。予測を実行してください。", true);
        return;
      }
      workPoints.push(point);
      updateSpecialControls();
      drawInteraction();
      return;
    }
    if (currentMode === "general_tolerance") {
      if (currentState.general_tolerance_candidate_count) {
        callApi("toggle_general_tolerance", { x: point.x, y: point.y });
      } else if (removeAppliedToleranceMode) {
        callApi("remove_applied_general_tolerance", { x: point.x, y: point.y });
      } else {
        showToast("先に「対象寸法を検出」を押してください。");
      }
      return;
    }
    if (
      currentMode === "word" &&
      removeDimensionMarkingMode &&
      currentState.general_tolerance_marked
    ) {
      callApi("remove_dimension_marking", { x: point.x, y: point.y });
      return;
    }
    if (
      currentMode === "word" &&
      Number(currentState.dimension_marking_candidate_count || 0) > 0 &&
      !currentState.general_tolerance_marked
    ) {
      callApi("toggle_dimension_marking", { x: point.x, y: point.y });
      return;
    }
    const dragMode =
      (currentMode === "word" &&
        !Number(currentState.dimension_marking_candidate_count || 0)) ||
      currentMode === "dimension" ||
      (currentMode === "replace" && !currentState.has_text);
    if (dragMode) {
      dragStart = point;
      dragCurrent = point;
      elements.canvas.setPointerCapture(event.pointerId);
      drawInteraction();
      return;
    }
    if (currentMode === "replace") {
      callApi("select_replacement", { x: point.x, y: point.y });
    } else {
      callApi("apply_action", currentMode, { x: point.x, y: point.y }, settings());
    }
  }

  function onPointerMove(event) {
    if (editableItemDrag) {
      const point = canvasPoint(event);
      const initial = editableItemDrag.initialRect;
      const deltaX = point.x - editableItemDrag.start.x;
      const deltaY = point.y - editableItemDrag.start.y;
      let rect;
      if (editableItemDrag.part === "target") {
        editableItemPreview = {
          ...editableItemPreview,
          target: [
            Math.max(0, Math.min(currentState.pdf_width, point.x)),
            Math.max(0, Math.min(currentState.pdf_height, point.y)),
          ],
        };
        drawInteraction();
        return;
      } else if (editableItemDrag.part === "move") {
        const width = initial[2] - initial[0];
        const height = initial[3] - initial[1];
        const x0 = Math.max(0, Math.min(currentState.pdf_width - width, initial[0] + deltaX));
        const y0 = Math.max(0, Math.min(currentState.pdf_height - height, initial[1] + deltaY));
        rect = [x0, y0, x0 + width, y0 + height];
      } else if (currentMode === "general_tolerance") {
        const initialWidth = Math.max(1, initial[2] - initial[0]);
        const initialHeight = Math.max(1, initial[3] - initial[1]);
        const widthScale = Math.max(0.35, (initialWidth + deltaX) / initialWidth);
        const heightScale = Math.max(0.35, (initialHeight + deltaY) / initialHeight);
        const scale = Math.min(widthScale, heightScale);
        const width = initialWidth * scale;
        const height = initialHeight * scale;
        rect = [initial[0], initial[1], initial[0] + width, initial[1] + height];
      } else {
        let width = Math.max(12, initial[2] - initial[0] + deltaX);
        let height = Math.max(12, initial[3] - initial[1] + deltaY);
        if (currentMode === "quality_stamp" || currentMode === "process_stamp") {
          width = height = Math.max(width, height);
        }
        width = Math.min(width, currentState.pdf_width - initial[0]);
        height = Math.min(height, currentState.pdf_height - initial[1]);
        rect = [initial[0], initial[1], initial[0] + width, initial[1] + height];
      }
      editableItemPreview = { ...editableItemPreview, rect };
      drawInteraction();
      return;
    }
    if (replacementDrag) {
      const point = canvasPoint(event);
      const deltaX = point.x - replacementDrag.start.x;
      const deltaY = point.y - replacementDrag.start.y;
      if (replacementDrag.part === "valueResize" || replacementDrag.part === "toleranceResize") {
        const screenDelta = (
          point.screenX - replacementDrag.start.screenX +
          point.screenY - replacementDrag.start.screenY
        ) * .045;
        if (replacementDrag.part === "valueResize") {
          elements.replacementSize.value = String(Math.max(
            5,
            Math.min(36, replacementDrag.initialValueSize + screenDelta)
          ).toFixed(1));
        } else {
          elements.replacementToleranceSize.value = String(Math.max(
            4,
            Math.min(24, replacementDrag.initialToleranceSize + screenDelta)
          ).toFixed(1));
        }
      } else if (replacementDrag.part === "value") {
        replacementOffsets.valueX = Math.max(
          -100,
          Math.min(100, replacementDrag.initial.valueX + deltaX)
        );
        replacementOffsets.valueY = Math.max(
          -100,
          Math.min(100, replacementDrag.initial.valueY + deltaY)
        );
      } else {
        replacementOffsets.toleranceX = Math.max(
          -100,
          Math.min(100, replacementDrag.initial.toleranceX + deltaX)
        );
        replacementOffsets.toleranceY = Math.max(
          -100,
          Math.min(100, replacementDrag.initial.toleranceY + deltaY)
        );
      }
      drawInteraction();
      return;
    }
    if (
      currentMode === "replace" &&
      currentState.replacement_selection &&
      !dragStart
    ) {
      const replacementPart = replacementPartAt(canvasPoint(event));
      elements.canvas.style.cursor = replacementPart?.endsWith("Resize")
        ? "nwse-resize"
        : replacementPart
          ? "grab"
          : toolInfo[currentMode].cursor;
    }
    if (["quality_stamp", "process_stamp", "procedure_note", "general_tolerance", "dimension", "replace"].includes(currentMode) && !dragStart) {
      const part = editablePartAt(canvasPoint(event));
      elements.canvas.style.cursor = part === "resize"
        ? "nwse-resize"
        : part === "target"
          ? "crosshair"
        : part === "move"
          ? "grab"
          : toolInfo[currentMode].cursor;
    }
    if (!dragStart) return;
    dragCurrent = canvasPoint(event);
    drawInteraction();
  }

  function onPointerUp(event) {
    if (editableItemDrag) {
      if (elements.canvas.hasPointerCapture(event.pointerId)) {
        elements.canvas.releasePointerCapture(event.pointerId);
      }
      const rect = [...editableItemPreview.rect];
      editableItemDrag = null;
      elements.canvas.style.cursor = "grab";
      callApi(
        currentMode === "general_tolerance"
          ? "move_general_tolerance_addition"
          : "update_editable_item",
        {
          x0: rect[0], y0: rect[1], x1: rect[2], y1: rect[3],
          target_x: editableItemPreview?.target?.[0],
          target_y: editableItemPreview?.target?.[1],
        }
      );
      return;
    }
    if (replacementDrag) {
      if (elements.canvas.hasPointerCapture(event.pointerId)) {
        elements.canvas.releasePointerCapture(event.pointerId);
      }
      replacementDrag = null;
      elements.canvas.style.cursor = "grab";
      drawInteraction();
      return;
    }
    if (!dragStart || !currentState.loaded) return;
    const start = dragStart;
    const end = canvasPoint(event);
    const distance = Math.hypot(end.screenX - start.screenX, end.screenY - start.screenY);
    clearInteraction();
    if (distance < 4) {
      if (currentMode === "replace") {
        callApi("select_replacement", { x: end.x, y: end.y });
        return;
      }
      if (currentMode === "dimension") {
        if (!elements.dimensionShowLeader.checked) {
          callApi(
            "apply_action",
            "dimension",
            {
              x: end.x, y: end.y,
              x0: end.x, y0: end.y, x1: end.x, y1: end.y,
              select_existing: true,
              create_if_empty: true,
            },
            settings()
          );
          return;
        }
        callApi(
          "apply_action",
          "dimension",
          { x: end.x, y: end.y, select_existing: true },
          settings()
        );
        return;
      }
      if (currentMode === "word") {
        callApi(
          "apply_action",
          "word",
          { x: end.x, y: end.y },
          settings()
        );
        return;
      }
      const message = currentMode === "replace"
          ? "置き換える元の寸法を囲んでください。"
        : currentMode === "word"
          ? elements.markMethod.value === "angled"
            ? "斜め文字に沿ってドラッグしてください。"
            : "マークする範囲をドラッグしてください。"
          : "矢先から文字位置までドラッグしてください。";
      showToast(message, true);
      return;
    }
    const payload = {
      x0: start.x, y0: start.y, x1: end.x, y1: end.y,
    };
    if (currentMode === "replace") {
      callApi("select_replacement", payload);
    } else {
      callApi("apply_action", currentMode, payload, settings());
    }
  }

  function setZoom(value) {
    const normalized = Math.max(60, Math.min(500, Math.round(value / 10) * 10));
    elements.zoomRange.value = String(normalized);
    elements.zoomLabel.textContent = `${normalized}%`;
    updateStageSize();
  }

  function zoomWithMouseWheel(event) {
    if (!currentState.loaded || busy) return;
    event.preventDefault();
    const viewportRect = elements.viewport.getBoundingClientRect();
    const anchorX = event.clientX - viewportRect.left;
    const anchorY = event.clientY - viewportRect.top;
    const oldZoom = Number(elements.zoomRange.value);
    const step = oldZoom >= 250 ? 20 : 10;
    const nextZoom = Math.max(
      60,
      Math.min(500, oldZoom + (event.deltaY < 0 ? step : -step))
    );
    if (nextZoom === oldZoom) return;
    const ratio = nextZoom / oldZoom;
    const oldScrollLeft = elements.viewport.scrollLeft;
    const oldScrollTop = elements.viewport.scrollTop;
    setZoom(nextZoom);
    elements.viewport.scrollLeft =
      (oldScrollLeft + anchorX) * ratio - anchorX;
    elements.viewport.scrollTop =
      (oldScrollTop + anchorY) * ratio - anchorY;
  }

  elements.open.addEventListener("click", choosePdf);
  elements.emptyOpen.addEventListener("click", choosePdf);
  elements.pdfFileInput.addEventListener("change", () => {
    uploadPdf(elements.pdfFileInput.files?.[0]);
  });
  elements.save.addEventListener("click", () => callApi("save_pdf"));
  elements.previous.addEventListener("click", () => {
    resetWorkShape();
    callApi("previous_page");
  });
  elements.next.addEventListener("click", () => {
    resetWorkShape();
    callApi("next_page");
  });
  elements.undo.addEventListener("click", () => callApi("undo"));
  elements.confirmReplacement.addEventListener("click", () =>
    callApi("confirm_replacement", settings()));
  elements.cancelReplacement.addEventListener("click", () =>
    callApi("cancel_replacement"));
  elements.scanGeneralTolerance.addEventListener("click", () => {
    setRemoveAppliedToleranceMode(false);
    callApi("scan_general_tolerances", settings());
  });
  elements.applyGeneralTolerance.addEventListener("click", () =>
    callApi("apply_general_tolerances"));
  elements.removeAppliedTolerance?.addEventListener("click", () =>
    setRemoveAppliedToleranceMode(!removeAppliedToleranceMode));
  elements.scanDimensionMarkings.addEventListener("click", () => {
    setRemoveDimensionMarkingMode(false);
    callApi("scan_dimension_markings");
  });
  elements.applyDimensionMarkings.addEventListener("click", () =>
    callApi("apply_dimension_markings"));
  elements.removeDimensionMarking?.addEventListener("click", () =>
    setRemoveDimensionMarkingMode(!removeDimensionMarkingMode));
  function updateGeneralToleranceControls() {
    elements.generalToleranceGradeGroup.classList.toggle(
      "hidden",
      elements.generalToleranceStandard.value !== "jis_b_0405"
    );
  }
  elements.generalToleranceStandard.addEventListener(
    "change",
    updateGeneralToleranceControls
  );
  updateGeneralToleranceControls();
  elements.workShapeStyle.addEventListener("change", () => {
    resetWorkShape();
    if (currentState.work_region_candidate_count) {
      callApi("cancel_work_region");
    }
    updateSpecialControls();
  });
  elements.markMethod.addEventListener("change", () => {
    clearInteraction();
    updateSpecialControls();
  });
  function setAutoWorkOperation(operation) {
    workAutoOperation = operation;
    updateSpecialControls();
  }
  elements.autoWorkReplace.addEventListener(
    "click",
    () => setAutoWorkOperation("replace")
  );
  elements.autoWorkAdd.addEventListener(
    "click",
    () => setAutoWorkOperation("add")
  );
  elements.autoWorkRemove.addEventListener(
    "click",
    () => setAutoWorkOperation("remove")
  );
  elements.confirmAutoWork.addEventListener(
    "click",
    () => callApi("confirm_work_region")
  );
  elements.cancelAutoWork.addEventListener(
    "click",
    () => {
      resetWorkShape();
      callApi("cancel_work_region");
    }
  );
  elements.predictWorkShape.addEventListener("click", predictWorkShape);
  elements.removeGuidedPoint.addEventListener("click", () => {
    workPoints.pop();
    updateSpecialControls();
    drawInteraction();
  });
  elements.resetGuidedPoints.addEventListener("click", resetWorkShape);
  elements.confirmWordCandidate.addEventListener(
    "click",
    () => callApi("confirm_word_candidate")
  );
  elements.cancelWordCandidate.addEventListener(
    "click",
    () => callApi("cancel_word_candidate")
  );
  elements.workLineWidth.addEventListener("input", drawInteraction);
  elements.dimensionAutoStyle.addEventListener("change", () => {
    updateSpecialControls();
    drawInteraction();
  });
  elements.fontSize.addEventListener("input", drawInteraction);
  elements.finishWorkShape.addEventListener("click", finishWorkShape);
  elements.removeWorkPoint.addEventListener("click", () => {
    workPoints.pop();
    updateSpecialControls();
    drawInteraction();
  });
  elements.cancelWorkShape.addEventListener("click", resetWorkShape);
  elements.clearPage.addEventListener("click", () => {
    if (confirm("このページに追加したマーク・線・スタンプをすべて消去しますか？")) {
      callApi("clear_page");
    }
  });
  elements.clearAll.addEventListener("click", () => {
    if (confirm("全ページに追加した内容をすべて消去しますか？")) {
      callApi("clear_all");
    }
  });

  $$(".tool-card").forEach((button) =>
    button.addEventListener("click", () => selectTool(button.dataset.mode)));
  $$(".swatch").forEach((button) => button.addEventListener("click", () => {
    currentColor = button.dataset.color;
    elements.customColor.value = currentColor;
    elements.summaryColor.style.background = currentColor;
    $$(".swatch").forEach((swatch) => swatch.classList.toggle("selected", swatch === button));
  }));
  elements.customColor.addEventListener("input", () => {
    currentColor = elements.customColor.value;
    elements.summaryColor.style.background = currentColor;
    $$(".swatch").forEach((swatch) => swatch.classList.remove("selected"));
  });
  elements.opacity.addEventListener("input", () => {
    elements.opacityValue.textContent = `${elements.opacity.value}%`;
  });
  const procedureNoteDefaults = {
    confidential: "【社外秘】\n注）使用後、速やかにシュレッダーで廃棄の事",
    phase: "位置関係注意",
    post_process: "処理場所：○社内／客先\n処理内容：無電解ニッケルメッキ 3μm～5μm",
    thread: "ねじ外径：\nねじ内径：\nねじの具合：",
    borrowed: "借用ゲージ",
    cut_split: "カット・スリワリ：食い込み 有・無",
    surface: "粗さ記号・粗さ値：",
    special: "独自規格：ピスコ／Astemo／栃木日東工器／その他",
    custom: "",
  };
  function updateProcedureNote() {
    elements.procedureNoteText.value =
      procedureNoteDefaults[elements.procedureNoteType.value] || "";
  }
  function updateMeasurementControls() {
    const sequence = elements.measurementType.value === "sequence";
    elements.measurementInstrumentGroup.classList.toggle("hidden", sequence);
    elements.measurementSequenceGroup.classList.toggle("hidden", !sequence);
  }
  elements.procedureNoteType.addEventListener("change", updateProcedureNote);
  elements.measurementType.addEventListener("change", updateMeasurementControls);
  updateMeasurementControls();
  function updateReplacementPreview() {
    elements.replacementPreviewValue.textContent =
      elements.replacementValue.value || "φ15.4";
    elements.replacementPreviewUpper.textContent =
      elements.upperTolerance.value;
    elements.replacementPreviewLower.textContent =
      elements.lowerTolerance.value;
    elements.replacementPreviewTolerance.classList.toggle(
      "hidden",
      !elements.upperTolerance.value && !elements.lowerTolerance.value
    );
    const valueSize = Math.max(5, Number(elements.replacementSize.value) || 9);
    const toleranceSize = Math.max(
      4,
      Number(elements.replacementToleranceSize.value) || valueSize * .8
    );
    elements.replacementPreviewValue.style.fontSize = `${Math.min(30, valueSize * 1.5)}px`;
    elements.replacementPreviewTolerance.style.fontSize = `${Math.min(24, toleranceSize * 1.5)}px`;
    elements.replacementPreviewUpper.style.fontSize = `${Math.min(24, toleranceSize * 1.5)}px`;
    elements.replacementPreviewLower.style.fontSize = `${Math.min(24, toleranceSize * 1.5)}px`;
    elements.replacementPreviewValue.style.transform = "none";
    elements.replacementPreviewTolerance.style.transform = "none";
    drawInteraction();
    updateButtons();
  }
  [elements.replacementValue, elements.upperTolerance, elements.lowerTolerance,
    elements.replacementSize, elements.replacementToleranceSize]
    .forEach((input) => input.addEventListener("input", updateReplacementPreview));

  elements.canvas.addEventListener("pointerdown", onPointerDown);
  elements.canvas.addEventListener("pointermove", onPointerMove);
  elements.canvas.addEventListener("pointerup", onPointerUp);
  elements.canvas.addEventListener("pointercancel", clearInteraction);
  elements.zoomRange.addEventListener("input", () => setZoom(Number(elements.zoomRange.value)));
  elements.zoomOut.addEventListener("click", () => setZoom(Number(elements.zoomRange.value) - 10));
  elements.zoomIn.addEventListener("click", () => setZoom(Number(elements.zoomRange.value) + 10));
  elements.viewport.addEventListener("wheel", zoomWithMouseWheel, { passive: false });
  window.addEventListener("resize", updateStageSize);

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey && event.key.toLowerCase() === "z") {
      event.preventDefault();
      callApi("undo");
      return;
    }
    if (event.ctrlKey && event.key.toLowerCase() === "s") {
      event.preventDefault();
      callApi("save_pdf");
      return;
    }
    if (event.key === "Escape") {
      if (currentState.word_candidate) {
        callApi("cancel_word_candidate");
        return;
      }
      if (currentMode === "work_shape") {
        resetWorkShape();
        if (currentState.work_region_candidate_count) {
          callApi("cancel_work_region");
        }
      } else {
        clearInteraction();
      }
      return;
    }
    if (
      event.key === "Enter" &&
      currentMode === "word" &&
      currentState.word_candidate
    ) {
      event.preventDefault();
      callApi("confirm_word_candidate");
      return;
    }
    const editingField =
      /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
    if (!editingField && currentMode === "work_shape") {
      if (event.key === "Enter") {
        event.preventDefault();
        const style = elements.workShapeStyle.value;
        if (style === "auto" || (style === "guided" && guidedPredictionReady)) {
          callApi("confirm_work_region");
        } else {
          finishWorkShape();
        }
        return;
      }
      if (
        event.key === "Backspace" &&
        elements.workShapeStyle.value !== "auto" &&
        !guidedPredictionReady
      ) {
        event.preventDefault();
        workPoints.pop();
        updateSpecialControls();
        drawInteraction();
        return;
      }
    }
    if (editingField) return;
  });

  document.addEventListener("dragenter", (event) => {
    event.preventDefault();
    dragDepth += 1;
    elements.dropOverlay.classList.add("visible");
  });
  document.addEventListener("dragover", (event) => event.preventDefault());
  document.addEventListener("dragleave", (event) => {
    event.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) elements.dropOverlay.classList.remove("visible");
  });
  document.addEventListener("drop", (event) => {
    event.preventDefault();
    dragDepth = 0;
    elements.dropOverlay.classList.remove("visible");
    uploadPdf(event.dataTransfer?.files?.[0]);
  });

  window.drawingAssist = {
    receiveState: (state) => receiveState(state),
  };

  async function initialize() {
    try {
      const response = await fetch("/api", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Drawing-Assist-Token": apiToken,
        },
        body: JSON.stringify({
          action: "get_initial_state",
          arguments: [],
        }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      receiveState(await response.json(), false);
    } catch (error) {
      setStatus(`初期化に失敗しました: ${error}`, true);
    }
  }

  selectTool("general_tolerance");
  updateReplacementPreview();
  updateSpecialControls();
  updateButtons();
  initialize();
})();
