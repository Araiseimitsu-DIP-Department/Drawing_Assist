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
    viewport: $("#documentViewport"),
    stage: $("#documentStage"),
    image: $("#pageImage"),
    canvas: $("#interactionCanvas"),
    status: $("#statusMessage"),
    dropOverlay: $("#dropOverlay"),
    zoomRange: $("#zoomRange"),
    zoomLabel: $("#zoomLabel"),
    zoomOut: $("#zoomOutButton"),
    zoomIn: $("#zoomInButton"),
    toolName: $("#settingsToolName"),
    modeHelp: $("#modeHelp"),
    opacity: $("#opacityRange"),
    opacityValue: $("#opacityValue"),
    customColor: $("#customColor"),
    markMethod: $("#markMethod"),
    highlightWidth: $("#highlightWidth"),
    angledWidthGroup: $("#angledWidthGroup"),
    workShapeStyle: $("#workShapeStyle"),
    autoWorkControls: $("#autoWorkControls"),
    manualWorkControls: $("#manualWorkControls"),
    autoWorkReplace: $("#autoWorkReplaceButton"),
    autoWorkAdd: $("#autoWorkAddButton"),
    autoWorkRemove: $("#autoWorkRemoveButton"),
    autoWorkCandidateCount: $("#autoWorkCandidateCount"),
    confirmAutoWork: $("#confirmAutoWorkButton"),
    cancelAutoWork: $("#cancelAutoWorkButton"),
    workLineWidth: $("#workLineWidth"),
    workLineWidthGroup: $("#workLineWidthGroup"),
    workPointCount: $("#workPointCount"),
    finishWorkShape: $("#finishWorkShapeButton"),
    removeWorkPoint: $("#removeWorkPointButton"),
    cancelWorkShape: $("#cancelWorkShapeButton"),
    dimensionText: $("#dimensionText"),
    fontSize: $("#fontSize"),
    stampName: $("#stampName"),
    stampDate: $("#stampDate"),
    stampSize: $("#stampSize"),
    replacementValue: $("#replacementValue"),
    originalReplacementValue: $("#originalReplacementValue"),
    replacementSelectionHint: $("#replacementSelectionHint"),
    upperTolerance: $("#upperTolerance"),
    lowerTolerance: $("#lowerTolerance"),
    replacementSize: $("#replacementSize"),
    confirmReplacement: $("#confirmReplacementButton"),
    cancelReplacement: $("#cancelReplacementButton"),
    replacementPreviewValue: $("#replacementPreviewValue"),
    replacementPreviewTolerance: $("#replacementPreviewTolerance"),
    replacementPreviewUpper: $("#replacementPreviewUpper"),
    replacementPreviewLower: $("#replacementPreviewLower"),
    toast: $("#toast"),
  };

  const toolInfo = {
    word: {
      name: "文字・記号をマーク",
      help: "文字はクリックします。任意範囲は囲むように、斜め文字は文字と平行にドラッグしてください。",
      cursor: "crosshair",
    },
    work_shape: {
      name: "ワーク形状をマーク",
      help: "半自動ではワーク内部をクリックして候補を確認します。検出しにくい場合は、面を囲む／実線をなぞる方式へ切り替えてください。",
      cursor: "crosshair",
    },
    strike: {
      name: "二重取消線",
      help: "取消線を入れたい寸法値の中央をクリックしてください。",
      cursor: "crosshair",
    },
    replace: {
      name: "寸法値を修正",
      help: "図面上の元寸法を選択し、右側で修正後の値を入力して「修正を反映」を押します。",
      cursor: "text",
    },
    dimension: {
      name: "寸法・引出線",
      help: "矢印の先端から、寸法文字を置く場所までドラッグしてください。",
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
  };

  let currentState = { loaded: false };
  let currentMode = "word";
  let currentColor = "#fff24d";
  let busy = false;
  let dragStart = null;
  let dragCurrent = null;
  let workPoints = [];
  let workAutoOperation = "replace";
  let dragDepth = 0;
  let toastTimer = null;

  function settings() {
    return {
      color: currentColor,
      opacity: Number(elements.opacity.value) / 100,
      mark_style: elements.markMethod.value,
      highlight_width: Number(elements.highlightWidth.value),
      work_shape_style: elements.workShapeStyle.value,
      work_line_width: Number(elements.workLineWidth.value),
      dimension_text: elements.dimensionText.value,
      font_size: Number(elements.fontSize.value),
      stamp_name: elements.stampName.value,
      stamp_date: elements.stampDate.value,
      stamp_size: Number(elements.stampSize.value),
      replacement_value: elements.replacementValue.value,
      upper_tolerance: elements.upperTolerance.value,
      lower_tolerance: elements.lowerTolerance.value,
      replacement_size: Number(elements.replacementSize.value),
    };
  }

  function setBusy(value) {
    busy = value;
    [elements.open, elements.emptyOpen, elements.save, elements.previous,
      elements.next, elements.undo, elements.clearPage, elements.clearAll,
      elements.confirmReplacement, elements.cancelReplacement,
      elements.finishWorkShape, elements.removeWorkPoint,
      elements.cancelWorkShape, elements.confirmAutoWork,
      elements.cancelAutoWork, elements.autoWorkReplace,
      elements.autoWorkAdd, elements.autoWorkRemove]
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

  function updateButtons() {
    const loaded = Boolean(currentState.loaded);
    const autoCandidateCount =
      Number(currentState.work_region_candidate_count || 0);
    const hasAutoCandidate = autoCandidateCount > 0;
    elements.save.disabled = busy || !loaded || hasAutoCandidate;
    elements.undo.disabled =
      busy || !loaded || (!currentState.item_count && !hasAutoCandidate);
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
    elements.confirmAutoWork.disabled =
      busy || !loaded || !hasAutoCandidate;
    elements.cancelAutoWork.disabled =
      busy || !loaded || !hasAutoCandidate;
    elements.autoWorkCandidateCount.textContent = hasAutoCandidate
      ? `${autoCandidateCount}か所を確認中`
      : "未選択";
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

    const previousOriginalValue =
      currentState.replacement_selection?.original_value;
    currentState = state;
    const replacementSelection = state.replacement_selection;
    if (
      replacementSelection?.original_value &&
      replacementSelection.original_value !== previousOriginalValue
    ) {
      elements.replacementValue.value =
        replacementSelection.original_value;
      updateReplacementPreview();
    }
    elements.originalReplacementValue.textContent = replacementSelection
      ? replacementSelection.original_text
      : "未選択";
    elements.replacementSelectionHint.textContent = replacementSelection
      ? replacementSelection.has_text
        ? `元の位置・方向・文字サイズ（${replacementSelection.font_size}pt）を維持します`
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
      const kind = state.has_text ? "文字情報あり" : "画像化PDF（ドラッグでマーキング）";
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
    if (announce && state.message) showToast(state.message);
    setBusy(false);
    updateButtons();
  }

  async function callApi(method, ...args) {
    if (busy) return;
    setBusy(true);
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
    setBusy(true);
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
      receiveState(await response.json());
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
    if (!toolInfo[mode]) return;
    if (currentMode === "work_shape" && mode !== "work_shape") {
      resetWorkShape();
      if (currentState.work_region_candidate_count) {
        callApi("cancel_work_region");
      }
    }
    currentMode = mode;
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
    drawInteraction();
  }

  function updateSpecialControls() {
    const autoMode = elements.workShapeStyle.value === "auto";
    elements.angledWidthGroup.classList.toggle(
      "hidden",
      elements.markMethod.value !== "angled"
    );
    elements.workPointCount.textContent = `${workPoints.length}点`;
    elements.autoWorkControls.classList.toggle("hidden", !autoMode);
    elements.manualWorkControls.classList.toggle("hidden", autoMode);
    elements.workLineWidthGroup.classList.toggle(
      "hidden",
      elements.workShapeStyle.value !== "line"
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
    updateSpecialControls();
    drawInteraction();
  }

  function finishWorkShape() {
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

  function drawInteraction() {
    const context = elements.canvas.getContext("2d");
    context.clearRect(0, 0, elements.canvas.width, elements.canvas.height);
    if (currentMode === "work_shape" && workPoints.length) {
      const screenPoints = workPoints.map(pdfPointToScreen);
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
      context.fillStyle = "#1746ad";
      screenPoints.forEach((point, index) => {
        context.beginPath();
        context.arc(point.screenX, point.screenY, 4, 0, Math.PI * 2);
        context.fill();
        context.fillStyle = "#ffffff";
        context.font = "bold 8px 'Yu Gothic UI'";
        context.textAlign = "center";
        context.fillText(
          String(index + 1),
          point.screenX,
          point.screenY + 3
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
      context.setLineDash([]);
      context.beginPath();
      context.moveTo(dragStart.screenX, dragStart.screenY);
      context.lineTo(dragCurrent.screenX, dragCurrent.screenY);
      context.stroke();
      const angle = Math.atan2(
        dragCurrent.screenY - dragStart.screenY,
        dragCurrent.screenX - dragStart.screenX
      );
      context.fillStyle = "#2563eb";
      context.beginPath();
      context.moveTo(dragStart.screenX, dragStart.screenY);
      context.lineTo(
        dragStart.screenX + Math.cos(angle - .45) * 12,
        dragStart.screenY + Math.sin(angle - .45) * 12
      );
      context.lineTo(
        dragStart.screenX + Math.cos(angle + .45) * 12,
        dragStart.screenY + Math.sin(angle + .45) * 12
      );
      context.closePath();
      context.fill();
      const label = elements.dimensionText.value || "寸法値";
      context.font = "bold 13px 'Yu Gothic UI'";
      const width = context.measureText(label).width + 10;
      context.fillStyle = currentColor;
      context.fillRect(dragCurrent.screenX, dragCurrent.screenY - 19, width, 23);
      context.fillStyle = "#111827";
      context.fillText(label, dragCurrent.screenX + 5, dragCurrent.screenY - 3);
    }
    context.restore();
  }

  function onPointerDown(event) {
    if (!currentState.loaded || busy || event.button !== 0) return;
    const point = canvasPoint(event);
    if (currentMode === "work_shape") {
      if (elements.workShapeStyle.value === "auto") {
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
      workPoints.push(point);
      updateSpecialControls();
      drawInteraction();
      return;
    }
    const dragMode = currentMode === "word" ||
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
    if (!dragStart) return;
    dragCurrent = canvasPoint(event);
    drawInteraction();
  }

  function onPointerUp(event) {
    if (!dragStart || !currentState.loaded) return;
    const start = dragStart;
    const end = canvasPoint(event);
    const distance = Math.hypot(end.screenX - start.screenX, end.screenY - start.screenY);
    clearInteraction();
    if (distance < 4) {
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
    const normalized = Math.max(60, Math.min(220, Math.round(value / 10) * 10));
    elements.zoomRange.value = String(normalized);
    elements.zoomLabel.textContent = `${normalized}%`;
    updateStageSize();
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
    () => callApi("cancel_work_region")
  );
  elements.workLineWidth.addEventListener("input", drawInteraction);
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
    $$(".swatch").forEach((swatch) => swatch.classList.toggle("selected", swatch === button));
  }));
  elements.customColor.addEventListener("input", () => {
    currentColor = elements.customColor.value;
    $$(".swatch").forEach((swatch) => swatch.classList.remove("selected"));
  });
  elements.opacity.addEventListener("input", () => {
    elements.opacityValue.textContent = `${elements.opacity.value}%`;
  });
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
    updateButtons();
  }
  [elements.replacementValue, elements.upperTolerance, elements.lowerTolerance]
    .forEach((input) => input.addEventListener("input", updateReplacementPreview));

  elements.canvas.addEventListener("pointerdown", onPointerDown);
  elements.canvas.addEventListener("pointermove", onPointerMove);
  elements.canvas.addEventListener("pointerup", onPointerUp);
  elements.canvas.addEventListener("pointercancel", clearInteraction);
  elements.zoomRange.addEventListener("input", () => setZoom(Number(elements.zoomRange.value)));
  elements.zoomOut.addEventListener("click", () => setZoom(Number(elements.zoomRange.value) - 10));
  elements.zoomIn.addEventListener("click", () => setZoom(Number(elements.zoomRange.value) + 10));
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
    const editingField =
      /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
    if (!editingField && currentMode === "work_shape") {
      if (event.key === "Enter") {
        event.preventDefault();
        if (elements.workShapeStyle.value === "auto") {
          callApi("confirm_work_region");
        } else {
          finishWorkShape();
        }
        return;
      }
      if (
        event.key === "Backspace" &&
        elements.workShapeStyle.value !== "auto"
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

  selectTool("word");
  updateReplacementPreview();
  updateSpecialControls();
  updateButtons();
  initialize();
})();
