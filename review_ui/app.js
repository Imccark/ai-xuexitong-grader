const state = {
  students: [],
  filteredStudents: [],
  currentStudentId: null,
  currentReview: null,
  layoutControlsBound: false,
  resizeHandleBound: false,
  currentView: null,
  weeks: [],
  selectedWeekId: null,
  loadedWeekId: null,
  promptLoaded: false,
  promptEditable: false,
  subjectsLoaded: false,
  subjectsData: null,
  subjectsMode: "form",
  dashboardLogs: [],
  pipelineLatestByKey: {},
  pipelineLogCursorByTaskId: {},
  pipelineStatusByTaskId: {},
  pipelinePolling: false,
  pipelinePollTimer: null,
  isExporting: false,
  exportEngine: "katex",
  exportSettingsLoaded: false,
  latexStatus: null,
  currentExportImage: null,
  exportStatusPollTimer: null,
  exportStatusPollToken: 0,
  studentLoadToken: 0,
};

const studentCountEl = document.getElementById("studentCount");
const studentListEl = document.getElementById("studentList");
const studentSearchEl = document.getElementById("studentSearch");
const reviewFilterEl = document.getElementById("reviewFilter");
const studentTitleEl = document.getElementById("studentTitle");
const pageMetaEl = document.getElementById("pageMeta");
const imagesContainerEl = document.getElementById("imagesContainer");
const modulesContainerEl = document.getElementById("modulesContainer");
const saveStatusEl = document.getElementById("saveStatus");
const reviewRiskPanelEl = document.getElementById("reviewRiskPanel");
const prevStudentBtnEl = document.getElementById("prevStudentBtn");
const nextStudentBtnEl = document.getElementById("nextStudentBtn");
const exportImageBtnEl = document.getElementById("exportImageBtn");
const copyImageBtnEl = document.getElementById("copyImageBtn");
const regenerateImageBtnEl = document.getElementById("regenerateImageBtn");
const appShellEl = document.getElementById("appShell");
const mainLayoutEl = document.getElementById("mainLayout");
const mainResizeHandleEl = document.getElementById("mainResizeHandle");
const toggleSidebarBtnEl = document.getElementById("toggleSidebarBtn");
const restoreSidebarBtnEl = document.getElementById("restoreSidebarBtn");
const weekButtonsEl = document.getElementById("weekButtons");
const weekCountEl = document.getElementById("weekCount");
const newWeekNameInputEl = document.getElementById("newWeekNameInput");
const createWeekBtnEl = document.getElementById("createWeekBtn");
const weekManageStatusEl = document.getElementById("weekManageStatus");
const currentWeekCardContentEl = document.getElementById("currentWeekCardContent");
const weekResourceCardContentEl = document.getElementById("weekResourceCardContent");
const deleteWeekCardContentEl = document.getElementById("deleteWeekCardContent");
const preprocessTaskCardContentEl = document.getElementById("preprocessTaskCardContent");
const dashboardLogsContentEl = document.getElementById("dashboardLogsContent");
const gradingTaskCardContentEl = document.getElementById("gradingTaskCardContent");
const exportEngineCardContentEl = document.getElementById("exportEngineCardContent");
const cardCurrentWeekEl = document.getElementById("cardCurrentWeek");
const cardWeekResourceEl = document.getElementById("cardWeekResource");
const cardDeleteWeekEl = document.getElementById("cardDeleteWeek");
const cardPreprocessTaskEl = document.getElementById("cardPreprocessTask");
const cardDashboardLogsEl = document.getElementById("cardDashboardLogs");
const cardGradingTaskEl = document.getElementById("cardGradingTask");
const cardExportEngineEl = document.getElementById("cardExportEngine");
const promptFileNameEl = document.getElementById("promptFileName");
const promptStateLabelEl = document.getElementById("promptStateLabel");
const promptEditorEl = document.getElementById("promptEditor");
const promptEditorPanelEl = document.getElementById("promptEditorPanel");
const subjectsEditorEl = document.getElementById("subjectsEditor");
const subjectsFormPanelEl = document.getElementById("subjectsFormPanel");
const subjectsJsonPanelEl = document.getElementById("subjectsJsonPanel");
const viewPromptBtnEl = document.getElementById("viewPromptBtn");
const editPromptBtnEl = document.getElementById("editPromptBtn");
const savePromptBtnEl = document.getElementById("savePromptBtn");
const resetPromptBtnEl = document.getElementById("resetPromptBtn");
const loadSubjectsBtnEl = document.getElementById("loadSubjectsBtn");
const saveSubjectsFormBtnEl = document.getElementById("saveSubjectsFormBtn");
const toggleSubjectsJsonBtnEl = document.getElementById("toggleSubjectsJsonBtn");
const saveSubjectsJsonBtnEl = document.getElementById("saveSubjectsJsonBtn");
const promptSaveStatusEl = document.getElementById("promptSaveStatus");
const subjectsSaveStatusEl = document.getElementById("subjectsSaveStatus");
const subjectIdInputEl = document.getElementById("subjectIdInput");
const subjectNameInputEl = document.getElementById("subjectNameInput");
const subjectModelInputEl = document.getElementById("subjectModelInput");
const subjectBaseUrlInputEl = document.getElementById("subjectBaseUrlInput");
const subjectApiKeyEnvInputEl = document.getElementById("subjectApiKeyEnvInput");
const subjectPromptTemplateInputEl = document.getElementById("subjectPromptTemplateInput");
const subjectGradingRequirementsInputEl = document.getElementById("subjectGradingRequirementsInput");
const subjectOutputFormatInputEl = document.getElementById("subjectOutputFormatInput");
const apiKeyModalEl = document.getElementById("apiKeyModal");
const closeApiKeyModalBtnEl = document.getElementById("closeApiKeyModalBtn");
const apiKeyEnvNameEl = document.getElementById("apiKeyEnvName");
const apiKeyPlatformHintEl = document.getElementById("apiKeyPlatformHint");
const apiKeyInputEl = document.getElementById("apiKeyInput");
const saveApiKeyBtnEl = document.getElementById("saveApiKeyBtn");
const copyApiKeyBtnEl = document.getElementById("copyApiKeyBtn");
const apiKeyStatusEl = document.getElementById("apiKeyStatus");
const apiCmdLinuxEl = document.getElementById("apiCmdLinux");
const apiCmdPowershellEl = document.getElementById("apiCmdPowershell");
const apiCmdCmdEl = document.getElementById("apiCmdCmd");
const copyApiCmdLinuxBtnEl = document.getElementById("copyApiCmdLinuxBtn");
const copyApiCmdPowershellBtnEl = document.getElementById("copyApiCmdPowershellBtn");
const copyApiCmdCmdBtnEl = document.getElementById("copyApiCmdCmdBtn");
const SIDEBAR_COLLAPSED_KEY = "review_ui_sidebar_collapsed";
const MAIN_LEFT_WIDTH_KEY = "review_ui_main_left_width";
const NEARBY_EXPORT_PRELOAD_RADIUS = 2;

function setExportButtonBusy(isBusy, label) {
  if (!exportImageBtnEl) {
    return;
  }
  exportImageBtnEl.disabled = Boolean(isBusy);
  exportImageBtnEl.textContent = label || (isBusy ? "导出中..." : "导出图片");
}

function setCopyButtonBusy(isBusy, label) {
  if (!copyImageBtnEl) {
    return;
  }
  copyImageBtnEl.disabled = Boolean(isBusy);
  copyImageBtnEl.textContent = label || (isBusy ? "复制中..." : "复制图片");
}

function setRegenerateButtonBusy(isBusy, label) {
  if (!regenerateImageBtnEl) {
    return;
  }
  regenerateImageBtnEl.disabled = Boolean(isBusy);
  regenerateImageBtnEl.textContent = label || (isBusy ? "重新生成中..." : "重新生成图片");
}

function runWhenIdle(callback, timeout = 300) {
  if (typeof window.requestIdleCallback === "function") {
    window.requestIdleCallback(() => callback(), { timeout });
    return;
  }
  window.setTimeout(() => callback(), Math.min(120, timeout));
}

function fetchJson(url, options) {
  return fetch(url, options).then(async (response) => {
    const raw = await response.text();
    let data = {};
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch (error) {
      const endpoint = String(url || "");
      if (!response.ok && endpoint.includes("/api/apikey") && response.status === 404) {
        throw new Error("当前后端未启用 API Key 存储接口，请重启 review_app.py 后刷新页面");
      }
      if (!response.ok) {
        throw new Error(`接口不可用（HTTP ${response.status}），请重启 review_app.py 并刷新页面`);
      }
      throw new Error("接口返回了非 JSON 内容");
    }
    if (!response.ok) {
      throw new Error(data.error || `请求失败: ${response.status}`);
    }
    return data;
  });
}

function setConfigStatus(targetEl, message, level = "normal") {
  if (!targetEl) {
    return;
  }
  targetEl.textContent = message;
  const className = level === "ok" ? "config-status ok" : level === "error" ? "config-status error" : "config-status";
  targetEl.className = className;
}

function setPromptEditorVisible(visible) {
  if (!promptEditorPanelEl || !promptEditorEl) {
    return;
  }
  promptEditorPanelEl.classList.toggle("is-hidden", !visible);
  promptEditorEl.disabled = !state.promptEditable;
}

function updatePromptMeta(stateLabel) {
  if (promptFileNameEl) {
    promptFileNameEl.textContent = "prompts/default_prompt.txt";
  }
  if (promptStateLabelEl) {
    promptStateLabelEl.textContent = stateLabel;
  }
}

function ensureSubjectsObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function getSubjectsFormData() {
  const base = ensureSubjectsObject(state.subjectsData);
  return {
    subject_id: subjectIdInputEl ? subjectIdInputEl.value.trim() : "",
    subject_name: subjectNameInputEl ? subjectNameInputEl.value.trim() : "",
    model: subjectModelInputEl ? subjectModelInputEl.value.trim() : "",
    base_url: subjectBaseUrlInputEl ? subjectBaseUrlInputEl.value.trim() : "",
    api_key_env: String(base.api_key_env || ""),
    prompt_template: String(base.prompt_template || ""),
    grading_requirements: subjectGradingRequirementsInputEl ? subjectGradingRequirementsInputEl.value.trim() : "",
    output_format: String(base.output_format || ""),
  };
}

function validateSubjectsData(data) {
  const payload = ensureSubjectsObject(data);
  const requiredFields = [
    "subject_id",
    "subject_name",
    "model",
    "base_url",
    "api_key_env",
    "prompt_template",
    "grading_requirements",
    "output_format",
  ];
  for (const field of requiredFields) {
    const value = payload[field];
    if (typeof value !== "string" || !value.trim()) {
      return { ok: false, message: `${field} 不能为空` };
    }
  }
  return { ok: true, message: "" };
}

function writeSubjectsForm(data) {
  const payload = ensureSubjectsObject(data);
  if (subjectIdInputEl) subjectIdInputEl.value = String(payload.subject_id || "");
  if (subjectNameInputEl) subjectNameInputEl.value = String(payload.subject_name || "");
  if (subjectModelInputEl) subjectModelInputEl.value = String(payload.model || "");
  if (subjectBaseUrlInputEl) subjectBaseUrlInputEl.value = String(payload.base_url || "");
  if (subjectGradingRequirementsInputEl) subjectGradingRequirementsInputEl.value = String(payload.grading_requirements || "");
}

function syncSubjectsJsonFromData() {
  if (!subjectsEditorEl || !state.subjectsData) {
    return;
  }
  subjectsEditorEl.value = `${JSON.stringify(state.subjectsData, null, 2)}\n`;
}

function syncSubjectsDataFromForm() {
  state.subjectsData = getSubjectsFormData();
  syncSubjectsJsonFromData();
}

function setSubjectsMode(mode) {
  state.subjectsMode = mode === "json" ? "json" : "form";
  if (subjectsFormPanelEl) {
    subjectsFormPanelEl.classList.toggle("is-hidden", state.subjectsMode !== "form");
  }
  if (subjectsJsonPanelEl) {
    subjectsJsonPanelEl.classList.toggle("is-hidden", state.subjectsMode !== "json");
  }
  if (toggleSubjectsJsonBtnEl) {
    toggleSubjectsJsonBtnEl.textContent = state.subjectsMode === "json" ? "返回表单编辑" : "高级 JSON 编辑";
  }
  if (saveSubjectsFormBtnEl) {
    saveSubjectsFormBtnEl.classList.toggle("is-hidden", state.subjectsMode !== "form");
  }
  if (saveSubjectsJsonBtnEl) {
    saveSubjectsJsonBtnEl.classList.toggle("is-hidden", state.subjectsMode !== "json");
  }
}

function normalizePayload(payload) {
  const safePayload = payload && typeof payload === "object" ? payload : {};
  const modules = safePayload.modules && typeof safePayload.modules === "object" ? safePayload.modules : {};
  const normalizedModules = {};
  Object.entries(modules).forEach(([moduleName, moduleData]) => {
    if (!moduleData || typeof moduleData !== "object") {
      normalizedModules[moduleName] = { raw_text: String(moduleData || ""), items: [] };
      return;
    }
    const rawItems = Array.isArray(moduleData.items) ? moduleData.items : [];
    const items = rawItems.map((item) => String(item).trim()).filter(Boolean);
    normalizedModules[moduleName] = {
      raw_text: typeof moduleData.raw_text === "string" ? moduleData.raw_text : "",
      items,
    };
  });
  return {
    student_name_or_id: String(safePayload.student_name_or_id || ""),
    overall: String(safePayload.overall || ""),
    modules: normalizedModules,
    error_details_by_question:
      safePayload.error_details_by_question && typeof safePayload.error_details_by_question === "object"
        ? safePayload.error_details_by_question
        : {},
    proof_review_by_question:
      safePayload.proof_review_by_question && typeof safePayload.proof_review_by_question === "object"
        ? safePayload.proof_review_by_question
        : {},
  };
}

function updateSaveStatus(message) {
  saveStatusEl.textContent = message;
}

function setSidebarCollapsed(collapsed) {
  if (!appShellEl) {
    return;
  }
  appShellEl.classList.toggle("sidebar-collapsed", Boolean(collapsed));
  if (toggleSidebarBtnEl) {
    toggleSidebarBtnEl.setAttribute("aria-label", collapsed ? "恢复左栏" : "隐藏左栏");
  }
  if (restoreSidebarBtnEl) {
    restoreSidebarBtnEl.classList.toggle("is-gone", !collapsed);
  }
  window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
}

function switchView(viewName) {
  const firstSwitch = state.currentView === null;
  if (!firstSwitch && state.currentView === viewName) {
    return;
  }
  state.currentView = viewName;

  document.querySelectorAll(".nav-tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.view === viewName);
  });

  document.querySelectorAll(".view-section").forEach((section) => {
    const isTarget = section.dataset.view === viewName;
    section.classList.toggle("is-hidden", !isTarget);
  });

  if (viewName === "review") {
    initLayoutControls();
  } else if (viewName === "dashboard") {
    loadWeeks().catch((error) => {
      if (weekCountEl) {
        weekCountEl.textContent = "周次加载失败";
      }
      window.console.error(error);
    });
  }
}

function initNavTabs() {
  document.querySelectorAll(".nav-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const view = tab.dataset.view;
      if (!view) {
        return;
      }
      if (view === "review") {
        openReviewView();
      } else {
        switchView(view);
      }
    });
  });
}

function initLayoutControls() {
  const collapsedSaved = window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1";
  setSidebarCollapsed(collapsedSaved);

  if (!state.layoutControlsBound) {
    if (toggleSidebarBtnEl) {
      toggleSidebarBtnEl.addEventListener("click", () => {
        const isCollapsed = appShellEl?.classList.contains("sidebar-collapsed");
        setSidebarCollapsed(!isCollapsed);
      });
    }

    if (restoreSidebarBtnEl) {
      restoreSidebarBtnEl.addEventListener("click", () => setSidebarCollapsed(false));
    }

    state.layoutControlsBound = true;
  }

  if (mainLayoutEl) {
    const widthSaved = Number.parseFloat(window.localStorage.getItem(MAIN_LEFT_WIDTH_KEY) || "");
    if (Number.isFinite(widthSaved) && widthSaved >= 340) {
      mainLayoutEl.style.setProperty("--main-left-width", `${widthSaved}px`);
    }
  }

  if (!mainResizeHandleEl || !mainLayoutEl) {
    return;
  }

  if (state.resizeHandleBound) {
    return;
  }

  const onPointerDown = (event) => {
    if (window.matchMedia("(max-width: 1080px)").matches) {
      return;
    }
    event.preventDefault();
    const rect = mainLayoutEl.getBoundingClientRect();
    const style = getComputedStyle(mainLayoutEl);
    const imagesPanelEl = mainLayoutEl.querySelector(".images-panel");
    const currentLeftWidth = imagesPanelEl
      ? imagesPanelEl.getBoundingClientRect().width
      : parseFloat(style.getPropertyValue("--main-left-width")) || rect.width * 0.58;
    const startX = event.clientX;
    const minLeft = 360;
    const minRight = 360;
    const columnGap = parseFloat(style.columnGap) || 0;
    const handleWidth = mainResizeHandleEl.getBoundingClientRect().width || 10;
    const maxLeft = rect.width - minRight - handleWidth - columnGap * 2;

    mainResizeHandleEl.classList.add("is-dragging");
    mainResizeHandleEl.setPointerCapture(event.pointerId);
    document.body.style.userSelect = "none";

    const onPointerMove = (moveEvent) => {
      const delta = moveEvent.clientX - startX;
      const nextWidth = Math.min(maxLeft, Math.max(minLeft, currentLeftWidth + delta));
      mainLayoutEl.style.setProperty("--main-left-width", `${nextWidth}px`);
    };

    const onPointerUp = (upEvent) => {
      mainResizeHandleEl.classList.remove("is-dragging");
      mainResizeHandleEl.releasePointerCapture(upEvent.pointerId);
      mainResizeHandleEl.removeEventListener("pointermove", onPointerMove);
      mainResizeHandleEl.removeEventListener("pointerup", onPointerUp);
      mainResizeHandleEl.removeEventListener("pointercancel", onPointerUp);
      document.body.style.userSelect = "";
      const appliedWidth = parseFloat(getComputedStyle(mainLayoutEl).getPropertyValue("--main-left-width"));
      if (Number.isFinite(appliedWidth)) {
        window.localStorage.setItem(MAIN_LEFT_WIDTH_KEY, String(appliedWidth));
      }
    };

    mainResizeHandleEl.addEventListener("pointermove", onPointerMove);
    mainResizeHandleEl.addEventListener("pointerup", onPointerUp);
    mainResizeHandleEl.addEventListener("pointercancel", onPointerUp);
  };

  mainResizeHandleEl.addEventListener("pointerdown", onPointerDown);
  state.resizeHandleBound = true;
}

function currentIndex() {
  return state.filteredStudents.findIndex((student) => student.id === state.currentStudentId);
}

const REVIEW_STATUS_LABELS = {
  pending: "未批改",
  candidate_ready: "Agent 已完成",
  review_required: "Agent 未完全收敛",
  unreadable: "图片不可辨认",
  reference_mismatch: "题目版本不匹配",
  pipeline_failed: "流程失败",
};

function reviewStatusLabel(status) {
  return REVIEW_STATUS_LABELS[String(status || "")] || String(status || "未知状态");
}

function renderStudentList() {
  studentListEl.innerHTML = "";
  const keyword = studentSearchEl.value.trim().toLowerCase();
  const statusFilter = reviewFilterEl ? reviewFilterEl.value : "all";
  state.filteredStudents = state.students.filter((student) => {
    const studentId = String(student.id || "");
    const matchesKeyword = studentId.toLowerCase().includes(keyword);
    const matchesStatus = statusFilter === "all" || String(student.reviewStatus || "pending") === statusFilter;
    return matchesKeyword && matchesStatus;
  });
  studentCountEl.textContent = `共 ${state.filteredStudents.length} 位学生`;

  if (!state.filteredStudents.length) {
    studentListEl.innerHTML = '<div class="empty-state">没有匹配的学生。</div>';
    return;
  }

  state.filteredStudents.forEach((student) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `student-item${student.id === state.currentStudentId ? " active" : ""}`;
    button.innerHTML = `
      <h3>${escapeHtml(student.id)}</h3>
      <p class="student-meta">${student.pageCount} 页 · ${student.hasResult ? "已有结果" : "待写结果"} · ${escapeHtml(reviewStatusLabel(student.reviewStatus))}</p>
    `;
    button.addEventListener("click", () => loadStudent(student.id));
    studentListEl.appendChild(button);
  });
}

function drawEvidenceBox(card, bbox) {
  if (!card || !Array.isArray(bbox) || bbox.length !== 4) {
    return;
  }
  const image = card.querySelector("img");
  const box = card.querySelector(".evidence-bbox");
  if (!image || !box || !image.naturalWidth || !image.naturalHeight) {
    return;
  }
  const imageRect = image.getBoundingClientRect();
  const cardRect = card.getBoundingClientRect();
  const [rawX1, rawY1, rawX2, rawY2] = bbox.map((value) => Number(value));
  const x1 = Math.max(0, Math.min(image.naturalWidth, rawX1));
  const y1 = Math.max(0, Math.min(image.naturalHeight, rawY1));
  const x2 = Math.max(x1, Math.min(image.naturalWidth, rawX2));
  const y2 = Math.max(y1, Math.min(image.naturalHeight, rawY2));
  box.style.left = `${imageRect.left - cardRect.left + (x1 / image.naturalWidth) * imageRect.width}px`;
  box.style.top = `${imageRect.top - cardRect.top + (y1 / image.naturalHeight) * imageRect.height}px`;
  box.style.width = `${((x2 - x1) / image.naturalWidth) * imageRect.width}px`;
  box.style.height = `${((y2 - y1) / image.naturalHeight) * imageRect.height}px`;
  box.classList.add("is-visible");
}

function scrollToEvidencePage(page, bbox = null) {
  const index = Math.max(0, Number(page || 1) - 1);
  const card = imagesContainerEl.children[index];
  if (card) {
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.add("evidence-focus");
    document.querySelectorAll(".evidence-bbox.is-visible").forEach((box) => box.classList.remove("is-visible"));
    const image = card.querySelector("img");
    if (image) {
      const draw = () => drawEvidenceBox(card, bbox);
      if (image.complete) {
        draw();
      } else {
        image.addEventListener("load", draw, { once: true });
      }
    }
    window.setTimeout(() => card.classList.remove("evidence-focus"), 1300);
  }
}

function renderReviewRiskPanel(review) {
  if (!reviewRiskPanelEl) {
    return;
  }
  reviewRiskPanelEl.innerHTML = "";
  if (!review) {
    return;
  }
  const candidate = review.candidate || {};
  const heading = document.createElement("div");
  heading.className = "review-risk-header";
  const summary = document.createElement("div");
  summary.innerHTML = `<strong>Agent 状态：${escapeHtml(reviewStatusLabel(review.status))}</strong><span>整体：${escapeHtml(candidate.overall || "unknown")} · 未收敛风险 ${Number(candidate.unresolved_risk_count || 0)}</span>`;
  heading.appendChild(summary);
  const readOnlyBadge = document.createElement("span");
  readOnlyBadge.className = "review-readonly-badge";
  readOnlyBadge.textContent = "只读 · Agent 正式结果";
  heading.appendChild(readOnlyBadge);
  reviewRiskPanelEl.appendChild(heading);

  const results = Object.entries(candidate.question_results || {}).sort(([left], [right]) => left.localeCompare(right));
  if (!results.length) {
    const note = document.createElement("p");
    note.className = "review-risk-empty";
    note.textContent = "当前尚无逐题 Agent 结果，请先在控制台运行批改任务。";
    reviewRiskPanelEl.appendChild(note);
    return;
  }
  const list = document.createElement("div");
  list.className = "review-risk-list";
  results.forEach(([questionId, result]) => {
    const card = document.createElement("article");
    card.className = `review-risk-item${result.needs_verification ? " is-risk" : ""}`;
    const title = document.createElement("div");
    title.className = "review-risk-item-title";
    title.innerHTML = `<strong>${escapeHtml(questionId)}</strong><span>${escapeHtml(result.verdict || "unknown")} · ${escapeHtml(result.risk_level || "low")} · ${(Number(result.confidence || 0) * 100).toFixed(0)}%</span>`;
    card.appendChild(title);
    const evidence = document.createElement("div");
    evidence.className = "review-evidence-list";
    const refs = [...(result.evidence_refs || [])];
    (result.rubric_decisions || []).forEach((decision) => refs.push(...(decision.evidence_refs || [])));
    if (refs.length) {
      refs.forEach((ref) => {
        const link = document.createElement("button");
        link.type = "button";
        link.className = "evidence-link";
        link.textContent = `证据：第 ${ref.page} 页 [${(ref.bbox || []).join(", ")}]`;
        link.addEventListener("click", () => scrollToEvidencePage(ref.page, ref.bbox));
        evidence.appendChild(link);
      });
    } else {
      evidence.textContent = "暂无证据定位";
    }
    card.appendChild(evidence);
    const details = document.createElement("div");
    details.className = "review-result-details";
    const transcriptionText = (result.transcription || []).map((span) => String(span.text || "")).filter(Boolean).join(" | ");
    if (transcriptionText) {
      const transcription = document.createElement("div");
      transcription.textContent = `忠实转写：${transcriptionText}`;
      details.appendChild(transcription);
    }
    (result.rubric_decisions || []).forEach((decision) => {
      const rubric = document.createElement("div");
      rubric.textContent = `评分点 ${decision.rubric_id || ""}：${decision.status || "unknown"}${decision.reason ? ` · ${decision.reason}` : ""}`;
      details.appendChild(rubric);
    });
    if (result.verifier_result) {
      const verifier = document.createElement("div");
      verifier.textContent = `验证器：${result.verifier_result.reason || (result.verifier_result.decisive ? "已裁决" : "未裁决")}`;
      details.appendChild(verifier);
    }
    if (details.childElementCount) {
      card.appendChild(details);
    }
    list.appendChild(card);
  });
  reviewRiskPanelEl.appendChild(list);
}

function renderImages(images, imageVariants = []) {
  imagesContainerEl.innerHTML = "";
  if (!images.length) {
    imagesContainerEl.innerHTML = '<div class="empty-state">该学生暂无图片。</div>';
    return;
  }

  images.forEach((imageUrl, index) => {
    const variant = imageVariants[index] || { original: imageUrl };
    const wrapper = document.createElement("article");
    wrapper.className = "image-card";
    wrapper.dataset.page = String(variant.page || index + 1);
    const heading = document.createElement("h3");
    heading.textContent = `第 ${index + 1} 页`;
    wrapper.appendChild(heading);
    const toolbar = document.createElement("div");
    toolbar.className = "image-view-toolbar";
    const label = document.createElement("label");
    label.textContent = "视图";
    const select = document.createElement("select");
    select.className = "image-view-select";
    [["original", "原图"], ["rectified", "平铺图"], ["normalized", "规范图"], ["enhanced", "增强图"]].forEach(([view, viewLabel]) => {
      if (!variant[view]) {
        return;
      }
      const option = document.createElement("option");
      option.value = view;
      option.textContent = viewLabel;
      select.appendChild(option);
    });
    const image = document.createElement("img");
    image.src = encodeURI(variant.original || imageUrl);
    image.alt = `第 ${index + 1} 页作业`;
    image.loading = "lazy";
    select.addEventListener("change", () => {
      image.src = encodeURI(variant[select.value] || variant.original || imageUrl);
    });
    label.appendChild(select);
    toolbar.appendChild(label);
    wrapper.appendChild(toolbar);
    wrapper.appendChild(image);
    const evidenceBox = document.createElement("div");
    evidenceBox.className = "evidence-bbox";
    evidenceBox.setAttribute("aria-hidden", "true");
    wrapper.appendChild(evidenceBox);
    imagesContainerEl.appendChild(wrapper);
  });
}

function containsMatrixExpression(expr) {
  const source = String(expr || "");
  return /\\begin\{(?:matrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|array|cases|aligned|smallmatrix)\}/.test(source);
}

function splitLatexTopLevel(source, separator) {
  const parts = [];
  let buffer = "";
  let braceDepth = 0;

  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    const next = source[index + 1];

    if (char === "\\") {
      if (separator === "\\\\" && next === "\\" && braceDepth === 0) {
        parts.push(buffer.trim());
        buffer = "";
        index += 1;
        while (/\s/.test(source[index + 1] || "")) {
          index += 1;
        }
        if ((source[index + 1] || "") === "[") {
          let offset = index + 2;
          let optionDepth = 1;
          while (offset < source.length && optionDepth > 0) {
            const optionChar = source[offset];
            if (optionChar === "[") {
              optionDepth += 1;
            } else if (optionChar === "]") {
              optionDepth -= 1;
            }
            offset += 1;
          }
          index = Math.max(index, offset - 1);
        }
        continue;
      }

      buffer += char;
      if (typeof next === "string") {
        buffer += next;
        index += 1;
      }
      continue;
    }

    if (separator === "&" && char === "&" && braceDepth === 0) {
      parts.push(buffer.trim());
      buffer = "";
      continue;
    }

    if (char === "{") {
      braceDepth += 1;
    } else if (char === "}") {
      braceDepth = Math.max(0, braceDepth - 1);
    }

    buffer += char;
  }

  parts.push(buffer.trim());
  return parts;
}

function escapeHtml(text) {
  return String(text || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function normalizeLatexExpression(expr) {
  return String(expr || "")
    .replace(/[＼﹨∖]/g, "\\")
    .replace(/(^|[\s{[(,;:=<>+\-*])[/／]([A-Za-z]+)\b/g, (_, prefix, command) => `${prefix}\\${command}`);
}

function normalizeMatrixCellLatex(cellText) {
  const value = normalizeLatexExpression(cellText).trim();
  if (!value) {
    return "\\phantom{0}";
  }
  if (/^(?:\.{3,}|\\dots|\\ldots|\\cdots)$/i.test(value)) {
    return "\\cdots";
  }
  return value;
}

function renderInlineLatexFragment(expr) {
  try {
    return katex.renderToString(normalizeLatexExpression(expr), {
      displayMode: false,
      throwOnError: false,
      strict: "ignore",
    });
  } catch (error) {
    return escapeHtml(expr);
  }
}

function parseRenderableMatrix(expr) {
  const source = String(expr || "").trim();
  let env = "";
  let body = "";
  let delimiter = "none";

  const bareMatrix = source.match(/^\\begin\{(matrix|pmatrix|bmatrix|smallmatrix)\}([\s\S]*)\\end\{\1\}$/);
  if (bareMatrix) {
    env = bareMatrix[1];
    body = String(bareMatrix[2] || "").trim();
  } else {
    const wrappedArray = source.match(
      /^\\left([\(\[])\s*\\begin\{array\}(?:\{[^}]*\})?([\s\S]*)\\end\{array\}\s*\\right([\)\]])$/,
    );
    const bareArray = source.match(/^\\begin\{array\}(?:\{[^}]*\})?([\s\S]*)\\end\{array\}$/);
    if (wrappedArray) {
      env = "array";
      body = String(wrappedArray[2] || "").trim();
      delimiter = wrappedArray[1] === "[" && wrappedArray[3] === "]" ? "bracket" : "paren";
    } else if (bareArray) {
      env = "array";
      body = String(bareArray[1] || "").trim();
      delimiter = "none";
    } else {
      return null;
    }
  }

  if (!body) {
    return null;
  }

  const rows = splitLatexTopLevel(body, "\\\\")
    .map((row) => splitLatexTopLevel(row, "&").map((cell) => String(cell || "").trim()))
    .filter((row) => row.some(Boolean));
  if (!rows.length) {
    return null;
  }

  const columnCount = Math.max(1, ...rows.map((row) => row.length || 1));
  const delimiterByEnv = {
    matrix: "none",
    smallmatrix: "none",
    pmatrix: "paren",
    bmatrix: "bracket",
    array: delimiter || "none",
  };

  return {
    rows,
    columnCount,
    delimiter: delimiterByEnv[env] || "none",
  };
}

function renderCustomMatrix(expr, displayMode) {
  const parsed = parseRenderableMatrix(expr);
  if (!parsed) {
    return null;
  }

  const rowsHtml = parsed.rows
    .map((row) => {
      const singleEllipsisRow = row.length === 1 && /^(?:\.{3,}|\\dots|\\ldots|\\cdots)$/i.test(row[0] || "");
      const cellsHtml = singleEllipsisRow
        ? `<span class="rendered-matrix__cell rendered-matrix__cell--ellipsis" style="grid-column: 1 / span ${parsed.columnCount};">${renderInlineLatexFragment("\\cdots")}</span>`
        : row
            .map((cell) => `<span class="rendered-matrix__cell">${renderInlineLatexFragment(normalizeMatrixCellLatex(cell))}</span>`)
            .join("");

      return `<span class="rendered-matrix__row" style="--matrix-cols: ${parsed.columnCount};">${cellsHtml}</span>`;
    })
    .join("");

  const matrixHtml = `
    <span class="rendered-matrix rendered-matrix--${parsed.delimiter}">
      <span class="rendered-matrix__delim rendered-matrix__delim--left" aria-hidden="true"></span>
      <span class="rendered-matrix__body">${rowsHtml}</span>
      <span class="rendered-matrix__delim rendered-matrix__delim--right" aria-hidden="true"></span>
    </span>
  `.trim();

  return displayMode ? `<span class="math-block math-block--matrix">${matrixHtml}</span>` : matrixHtml;
}

function renderMarkdownLatex(rawText, previewEl, options = {}) {
  if (!previewEl) {
    return;
  }
  const preferDisplayForMatrices = Boolean(options.preferDisplayForMatrices);
  const source = String(rawText || "")
    .normalize("NFKC")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "")
    .replace(/[\u200B-\u200D\u2060\uFEFF]/g, "")
    .replace(/=\u0338/g, "≠")
    .replace(/<\u0338/g, "≮")
    .replace(/>\u0338/g, "≯");
  const tokens = [];
  let tokenIndex = 0;
  const pushToken = (expr, displayMode) => {
    const key = `@@MATH_${tokenIndex++}@@`;
    const normalizedExpr = normalizeLatexExpression(expr);
    tokens.push({
      key,
      expr: normalizedExpr,
      displayMode,
      isMatrix: containsMatrixExpression(normalizedExpr),
    });
    return key;
  };

  // 先抽离公式，避免 Markdown 解析破坏 LaTeX 内容。
  const sourceWithTokens = source
    .replace(/\\\[([\s\S]+?)\\\]/g, (_, expr) => pushToken(expr, true))
    .replace(/\\\(([\s\S]+?)\\\)/g, (_, expr) => {
      const displayMode = preferDisplayForMatrices && containsMatrixExpression(expr);
      return pushToken(expr, displayMode);
    })
    .replace(/\$\$([\s\S]+?)\$\$/g, (_, expr) => pushToken(expr, true))
    .replace(/\$([^\n$]+?)\$/g, (_, expr) => {
      const displayMode = preferDisplayForMatrices && containsMatrixExpression(expr);
      return pushToken(expr, displayMode);
    });

  if (typeof marked === "undefined") {
    previewEl.textContent = source;
    return;
  }

  let markdownHtml = "";
  try {
    markdownHtml = marked.parse(sourceWithTokens, { breaks: true });
  } catch (error) {
    previewEl.textContent = source;
    return;
  }

  let safeHtml =
    typeof DOMPurify !== "undefined" ? DOMPurify.sanitize(markdownHtml, { USE_PROFILES: { html: true } }) : markdownHtml;

  // 优先使用 katex.renderToString（不依赖 auto-render）。
  if (typeof katex !== "undefined" && typeof katex.renderToString === "function") {
    tokens.forEach((token) => {
      const customMatrix = token.isMatrix ? renderCustomMatrix(token.expr, token.displayMode) : null;
      const rendered = customMatrix
        ? customMatrix
        : katex.renderToString(token.expr, {
            displayMode: token.displayMode,
            throwOnError: false,
            strict: "ignore",
          });
      const wrappedRendered =
        customMatrix || !token.displayMode ? rendered : `<span class="math-block${token.isMatrix ? " math-block--matrix" : ""}">${rendered}</span>`;
      safeHtml = safeHtml.replaceAll(token.key, wrappedRendered);
    });
    previewEl.innerHTML = safeHtml;
    return;
  }

  previewEl.innerHTML = safeHtml;
  if (typeof renderMathInElement === "function") {
    try {
      renderMathInElement(previewEl, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "$", right: "$", display: false },
        ],
        throwOnError: false,
      });
    } catch (error) {
      previewEl.textContent = source;
    }
  }
}


function renderModules(payload) {
  modulesContainerEl.innerHTML = "";

  const headerCard = document.createElement("article");
  headerCard.className = "module-card summary-card";
  headerCard.innerHTML = `
    <h3>基础信息</h3>
    <div class="readonly-field"><span>姓名/学号</span><strong>${escapeHtml(payload.student_name_or_id || "")}</strong></div>
    <div class="readonly-field"><span>整体情况</span><strong>${escapeHtml(payload.overall || "尚未生成")}</strong></div>
  `;
  modulesContainerEl.appendChild(headerCard);

  const moduleEntries = Object.entries(payload.modules || {});
  if (!moduleEntries.length) {
    const emptyCard = document.createElement("div");
    emptyCard.className = "empty-state";
    emptyCard.textContent = "暂无可展示的模块内容。";
    modulesContainerEl.appendChild(emptyCard);
    return;
  }

  moduleEntries.forEach(([moduleName, block]) => {
    const card = document.createElement("article");
    card.className = "module-card";

    const items = Array.isArray(block.items) && block.items.length ? block.items : [block.raw_text || ""];
    card.innerHTML = `
      <div class="module-header">
        <h3>${escapeHtml(moduleName)}</h3>
      </div>
      <div class="module-items"></div>
    `;
    const moduleItemsEl = card.querySelector(".module-items");
    items.forEach((item, index) => {
      const row = document.createElement("div");
      row.className = "item-row readonly-item-row";
      row.innerHTML = `<span class="item-order">${index + 1}.</span><div class="item-preview"></div>`;
      renderMarkdownLatex(String(item || ""), row.querySelector(".item-preview"), { preferDisplayForMatrices: true });
      moduleItemsEl.appendChild(row);
    });
    modulesContainerEl.appendChild(card);
  });
}

function renderResultText(payload) {
  const lines = [
    "========================================",
    `姓名/学号：${payload.student_name_or_id || ""}`,
    `整体情况：${payload.overall || ""}`,
  ];
  Object.entries(payload.modules || {}).forEach(([moduleName, block]) => {
    lines.push(`${moduleName}：`);
    const items = Array.isArray(block.items) && block.items.length ? block.items : [block.raw_text || ""];
    items
      .map((item) => String(item).trim())
      .filter(Boolean)
      .forEach((item, index) => {
        lines.push(`${index + 1}. ${item}`);
      });
  });
  lines.push("========================================");
  return `${lines.join("\n")}\n`;
}

function downloadBlob(blob, fileName) {
  const objectUrl = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = fileName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1200);
  }
}

function getExportFileNameFromResponse(response, fallbackFileName) {
  const header = response.headers.get("Content-Disposition") || "";
  const utf8Match = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match && utf8Match[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch (error) {
      return utf8Match[1];
    }
  }
  const plainMatch = header.match(/filename=\"?([^\";]+)\"?/i);
  if (plainMatch && plainMatch[1]) {
    return plainMatch[1];
  }
  return fallbackFileName;
}

function clearExportImageStatusPolling() {
  if (state.exportStatusPollTimer) {
    window.clearTimeout(state.exportStatusPollTimer);
    state.exportStatusPollTimer = null;
  }
  state.exportStatusPollToken += 1;
}

function normalizeExportImageStatus(status) {
  if (!status || typeof status !== "object") {
    return null;
  }
  return {
    status: String(status.status || ""),
    ready: Boolean(status.ready),
    queued: Boolean(status.queued),
    rendering: Boolean(status.rendering),
    missing: Boolean(status.missing),
    error: String(status.error || ""),
    imageUrl: String(status.imageUrl || ""),
    updatedAt: Number(status.updatedAt || 0),
  };
}

function applyExportImageStatus(status) {
  const normalized = normalizeExportImageStatus(status);
  state.currentExportImage = normalized;
  if (state.isExporting) {
    return normalized;
  }
  if (normalized?.queued || normalized?.rendering) {
    setRegenerateButtonBusy(false, "图片生成中...");
    setExportButtonBusy(false, "图片生成中...");
    setCopyButtonBusy(false, "图片生成中...");
  } else {
    setRegenerateButtonBusy(false, "重新生成图片");
    setExportButtonBusy(false, "导出图片");
    setCopyButtonBusy(false, "复制图片");
  }
  return normalized;
}

function ensureStudentUnchanged(studentId, actionLabel = "当前操作") {
  if (String(studentId || "") !== String(state.currentStudentId || "")) {
    throw new Error(`${actionLabel}期间已切换学生，请重新操作。`);
  }
}

async function fetchExportImageStatus(studentId = state.currentStudentId, options = {}) {
  if (!studentId) {
    return null;
  }
  const { priorityHigh = false, enqueue = true } = options;
  const data = await requestExportImageStatus(studentId, { priorityHigh, enqueue });
  return applyExportImageStatus(data);
}

async function requestExportImageStatus(studentId, options = {}) {
  if (!studentId) {
    return null;
  }
  const { priorityHigh = false, enqueue = true, force = false } = options;
  const query = new URLSearchParams();
  if (enqueue) {
    query.set("enqueue", "1");
  }
  if (priorityHigh) {
    query.set("priority", "high");
  }
  if (force) {
    query.set("force", "1");
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  const data = await fetchJson(`/api/student/${encodeURIComponent(studentId)}/export-image-status${suffix}`);
  return normalizeExportImageStatus(data.exportImage);
}

function getNearbyStudentIds(studentId, radius = NEARBY_EXPORT_PRELOAD_RADIUS) {
  const sourceList = state.filteredStudents.length ? state.filteredStudents : state.students;
  const index = sourceList.findIndex((student) => student.id === studentId);
  if (index === -1) {
    return [];
  }
  const result = [];
  for (let offset = 1; offset <= Math.max(0, radius); offset += 1) {
    const prev = sourceList[index - offset];
    const next = sourceList[index + offset];
    if (prev?.id) {
      result.push(prev.id);
    }
    if (next?.id) {
      result.push(next.id);
    }
  }
  return result;
}

function warmNearbyExportImages(studentId) {
  const centerId = String(studentId || "").trim();
  if (!centerId) {
    return;
  }
  const nearbyIds = getNearbyStudentIds(centerId);
  if (!nearbyIds.length) {
    return;
  }
  Promise.allSettled(nearbyIds.map((id) => requestExportImageStatus(id, { enqueue: true, priorityHigh: false }))).catch(
    () => {}
  );
}

function startExportImageStatusPolling(options = {}) {
  const { priorityHigh = false, intervalMs = 900 } = options;
  if (!state.currentStudentId) {
    return;
  }

  clearExportImageStatusPolling();
  const token = state.exportStatusPollToken;

  const poll = async () => {
    if (token !== state.exportStatusPollToken || !state.currentStudentId) {
      return;
    }
    try {
      const status = await fetchExportImageStatus(state.currentStudentId, { priorityHigh, enqueue: true });
      if (token !== state.exportStatusPollToken) {
        return;
      }
      if (status?.queued || status?.rendering) {
        state.exportStatusPollTimer = window.setTimeout(poll, intervalMs);
      } else {
        state.exportStatusPollTimer = null;
        if (!state.isExporting) {
          if (status?.ready) {
            updateSaveStatus("图片已就绪");
          } else if (status?.error) {
            updateSaveStatus("图片生成失败");
          }
        }
      }
    } catch (error) {
      if (token !== state.exportStatusPollToken) {
        return;
      }
      state.exportStatusPollTimer = window.setTimeout(poll, Math.max(intervalMs, 1200));
    }
  };

  state.exportStatusPollTimer = window.setTimeout(poll, 0);
}

async function waitForExportImageReady(options = {}) {
  const studentId = String(options.studentId || state.currentStudentId || "");
  const { priorityHigh = false, timeoutMs = 45000 } = options;
  if (!studentId) {
    throw new Error("请先选择一位学生。");
  }
  const startedAt = Date.now();
  let status = await fetchExportImageStatus(studentId, { priorityHigh, enqueue: true });
  while (status && !status.ready) {
    ensureStudentUnchanged(studentId, "等待图片生成");
    if (status.error) {
      throw new Error(status.error || "图片生成失败");
    }
    if (status.missing) {
      throw new Error("当前学生还没有可导出的 Agent 结果。");
    }
    if (Date.now() - startedAt >= timeoutMs) {
      throw new Error("图片仍在生成中，请稍后再试。");
    }
    updateSaveStatus(status.rendering ? "图片生成中..." : "图片排队中...");
    await new Promise((resolve) => window.setTimeout(resolve, 800));
    status = await fetchExportImageStatus(studentId, { priorityHigh: true, enqueue: true });
  }
  ensureStudentUnchanged(studentId, "等待图片生成");
  return status;
}

async function fetchReadyExportImagePayload() {
  const studentId = String(state.currentStudentId || "");
  if (!studentId) {
    throw new Error("请先选择一位学生。");
  }

  await waitForExportImageReady({ studentId, priorityHigh: true, timeoutMs: 45000 });
  const response = await fetch(`/api/student/${encodeURIComponent(studentId)}/export-image`, {
    method: "POST",
  });

  if (!response.ok) {
    const raw = await response.text();
    let message = "导出失败";
    try {
      const data = raw ? JSON.parse(raw) : {};
      message = data.error || data.message || message;
    } catch (error) {
      if (raw.trim()) {
        message = raw.trim();
      }
    }
    throw new Error(message);
  }

  const blob = await response.blob();
  ensureStudentUnchanged(studentId, "导出图片");
  const fallbackFileName = `${studentId || "annotations"}-${Date.now()}.png`;
  const fileName = getExportFileNameFromResponse(response, fallbackFileName);
  if (!(blob instanceof Blob)) {
    throw new Error("后端未返回可下载的 PNG 数据。");
  }
  return { fileName, blob };
}

async function regenerateCurrentExportImage() {
  const studentId = String(state.currentStudentId || "");
  if (!studentId) {
    window.alert("请先选择一位学生，再重新生成图片。");
    return;
  }
  if (state.isExporting) {
    return;
  }

  state.isExporting = true;
  setRegenerateButtonBusy(true, "重新生成中...");
  setExportButtonBusy(true, "导出图片");
  setCopyButtonBusy(true, "复制图片");
  updateSaveStatus("重新生成图片中...");

  try {
    const status = await requestExportImageStatus(studentId, {
      enqueue: true,
      priorityHigh: true,
      force: true,
    });
    ensureStudentUnchanged(studentId, "重新生成图片");
    applyExportImageStatus(status);
    startExportImageStatusPolling({ priorityHigh: true });
    await waitForExportImageReady({ studentId, priorityHigh: true, timeoutMs: 45000 });
    updateSaveStatus("图片已重新生成");
    window.setTimeout(() => updateSaveStatus("已加载"), 1800);
  } catch (error) {
    updateSaveStatus("重新生成失败");
    window.alert(error?.message || "重新生成失败");
  } finally {
    state.isExporting = false;
    setRegenerateButtonBusy(false, "重新生成图片");
    applyExportImageStatus(state.currentExportImage);
  }
}

async function exportAnnotationsAsImage() {
  if (!state.currentStudentId) {
    window.alert("请先选择一位学生，再导出图片。");
    return;
  }
  if (state.isExporting) {
    return;
  }

  state.isExporting = true;
  setExportButtonBusy(true, "导出中...");
  setCopyButtonBusy(true, "复制图片");
  updateSaveStatus("导出中...");

  try {
    const { fileName, blob } = await fetchReadyExportImagePayload();
    downloadBlob(blob, fileName);
    updateSaveStatus("已下载 PNG");
    window.setTimeout(() => updateSaveStatus("已加载"), 1800);
  } catch (error) {
    updateSaveStatus("导出失败");
    window.alert(error?.message || "导出失败");
  } finally {
    state.isExporting = false;
    applyExportImageStatus(state.currentExportImage);
  }
}

async function copyAnnotationsImage() {
  if (!state.currentStudentId) {
    window.alert("请先选择一位学生，再复制图片。");
    return;
  }
  if (state.isExporting) {
    return;
  }
  if (!navigator.clipboard || typeof window.ClipboardItem === "undefined") {
    window.alert("当前浏览器不支持复制图片到剪贴板。");
    return;
  }

  state.isExporting = true;
  setExportButtonBusy(true, "导出图片");
  setCopyButtonBusy(true, "复制中...");
  updateSaveStatus("复制中...");

  try {
    const { blob } = await fetchReadyExportImagePayload();
    await navigator.clipboard.write([new ClipboardItem({ [blob.type || "image/png"]: blob })]);
    updateSaveStatus("图片已复制");
    window.setTimeout(() => updateSaveStatus("已加载"), 1800);
  } catch (error) {
    const message = String(error?.message || "");
    if (/NotAllowedError|focus/i.test(message)) {
      window.alert("复制失败：请保持页面聚焦，并允许浏览器访问剪贴板。");
    } else {
      window.alert(error?.message || "复制失败");
    }
    updateSaveStatus("复制失败");
  } finally {
    state.isExporting = false;
    applyExportImageStatus(state.currentExportImage);
  }
}

async function loadStudents() {
  const data = await fetchJson("/api/students");
  state.students = data.students;
  state.filteredStudents = data.students;
  renderStudentList();
  if (data.students.length) {
    const firstStudentId = data.students[0].id;
    const loadFirstStudent = () => {
      if (state.currentStudentId) {
        return;
      }
      loadStudent(firstStudentId, true).catch((error) => {
        window.console.warn("load first student failed", error);
      });
    };
    if (state.currentView === "dashboard" || state.currentView === null) {
      runWhenIdle(loadFirstStudent, 450);
    } else {
      await loadStudent(firstStudentId, true);
    }
  }
}

async function loadWeeks() {
  const data = await fetchJson("/api/weeks");
  state.weeks = data.weeks;
  if (typeof data.currentWeekId === "string" && data.currentWeekId) {
    state.loadedWeekId = data.currentWeekId;
  }

  const availableWeekIds = new Set(state.weeks.map((item) => item.id));
  if (!state.selectedWeekId || !availableWeekIds.has(state.selectedWeekId)) {
    if (state.loadedWeekId && availableWeekIds.has(state.loadedWeekId)) {
      state.selectedWeekId = state.loadedWeekId;
    } else if (state.weeks.length) {
      state.selectedWeekId = state.weeks[0].id;
    }
  }
  renderWeekButtons();
}

function renderWeekButtons() {
  if (!weekButtonsEl) {
    return;
  }
  weekButtonsEl.innerHTML = "";
  state.weeks.forEach((week) => {
    const row = document.createElement("div");
    row.className = "week-row";

    const selectBtn = document.createElement("button");
    selectBtn.className = `week-btn${week.id === state.selectedWeekId ? " is-active" : ""}`;
    selectBtn.type = "button";
    selectBtn.textContent = week.name || week.id;
    selectBtn.addEventListener("click", () => selectWeek(week.id));
    row.appendChild(selectBtn);
    weekButtonsEl.appendChild(row);
  });
  if (weekCountEl) {
    weekCountEl.textContent = `共 ${state.weeks.length} 周`;
  }
  renderDashboardSummaryCards();
}

function setWeekManageStatus(message, level = "normal", options = {}) {
  const { log = true, inline = false } = options;
  if (weekManageStatusEl) {
    if (inline) {
      weekManageStatusEl.textContent = message;
      weekManageStatusEl.style.color = level === "error" ? "#a63d2a" : level === "ok" ? "#4a8c3f" : "";
    } else {
      weekManageStatusEl.textContent = "";
      weekManageStatusEl.style.color = "";
    }
  }
  if (message && log) {
    appendDashboardLog(message);
  }
}

function appendDashboardLog(message) {
  const text = String(message || "").trim();
  if (!text) {
    return;
  }
  const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  state.dashboardLogs.unshift(`[${time}] ${text}`);
  if (state.dashboardLogs.length > 80) {
    state.dashboardLogs = state.dashboardLogs.slice(0, 80);
  }
  renderDashboardSummaryCards();
}

function getSelectedWeek() {
  return state.weeks.find((item) => item.id === state.selectedWeekId) || null;
}

function formatTaskStatus(task) {
  if (!task) return "未运行";
  if (task.status === "running") return "运行中";
  if (task.status === "success") return "已完成";
  return `失败（${task.error || `退出码 ${task.returnCode ?? "-"}`}）`;
}

function taskLabel(taskType) {
  return taskType === "preprocess" ? "前处理" : "Agent 批改";
}

function taskCacheKey(taskType, weekId) {
  return `${taskType}:${weekId}`;
}

function buildPipelineTaskCard(taskType, selectedWeek, latestTask) {
  const weekId = selectedWeek.id;
  const assignmentPath = `configs/assignments/${weekId}.json`;
  const isPreprocess = taskType === "preprocess";
  const title = isPreprocess ? "run_preprocessing.py" : "Agent 多阶段批改";
  const workersId = `${taskType}WorkersInput`;
  const flagId = `${taskType}FlagInput`;
  const runBtnId = `${taskType}RunBtn`;
  const status = formatTaskStatus(latestTask);
  const extraLabel = isPreprocess ? "reprocess（重新生成已有图片）" : "regrade（重新批改全部学生）";
  const flagChecked = latestTask?.flagEnabled ? "checked" : "";
  const workersValue = Number.isInteger(latestTask?.maxWorkers) ? latestTask.maxWorkers : 4;
  const isRunning = latestTask?.status === "running";

  return `
    <div class="task-run-card">
      <p class="task-run-script">${title}</p>
      <p class="task-run-week">当前周参数：<code>${assignmentPath}</code></p>
      <div class="task-run-params">
        <label>max-workers <input id="${workersId}" type="number" min="1" value="${workersValue}" /></label>
        <label class="task-run-flag"><input id="${flagId}" type="checkbox" ${flagChecked} /> ${extraLabel}</label>
      </div>
      <p class="task-run-status">状态：${status}</p>
      <button type="button" class="summary-action-btn" id="${runBtnId}" ${isRunning ? "disabled" : ""}>
        ${isRunning ? "运行中..." : `启动 ${title}`}
      </button>
    </div>
  `;
}

async function loadLatestPipelineTask(task, weekId) {
  try {
    const data = await fetchJson(`/api/pipeline/latest?task=${encodeURIComponent(task)}&weekId=${encodeURIComponent(weekId)}`);
    return data.task || null;
  } catch (error) {
    window.console.warn("load latest pipeline task failed", error);
    return null;
  }
}

async function runPipelineTask(task, weekId, maxWorkers, flagEnabled) {
  const data = await fetchJson("/api/pipeline/run", {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify({ task, weekId, maxWorkers, flagEnabled }),
  });
  return data.task;
}

async function fetchPipelineTaskDetail(taskId, sinceLine = 0, limit = 60) {
  const data = await fetchJson(
    `/api/pipeline/task?taskId=${encodeURIComponent(taskId)}&sinceLine=${encodeURIComponent(sinceLine)}&limit=${encodeURIComponent(limit)}`
  );
  return data;
}

function bindPipelineTaskCard(taskType, selectedWeek) {
  const isPreprocess = taskType === "preprocess";
  const workersInput = document.getElementById(`${taskType}WorkersInput`);
  const flagInput = document.getElementById(`${taskType}FlagInput`);
  const runBtn = document.getElementById(`${taskType}RunBtn`);
  if (!runBtn || !workersInput || !flagInput) {
    return;
  }
  runBtn.addEventListener("click", async () => {
    const cacheKey = taskCacheKey(taskType, selectedWeek.id);
    const latestTask = state.pipelineLatestByKey[cacheKey] || null;
    if (latestTask?.status === "running") {
      setWeekManageStatus(`${taskLabel(taskType)}任务正在运行中，请勿重复启动`, "error");
      runBtn.disabled = true;
      return;
    }
    const latestFromServer = await loadLatestPipelineTask(taskType, selectedWeek.id);
    if (latestFromServer?.status === "running") {
      state.pipelineLatestByKey[cacheKey] = latestFromServer;
      setWeekManageStatus(`${taskLabel(taskType)}任务正在运行中，请勿重复启动`, "error");
      renderDashboardSummaryCards();
      return;
    }
    const maxWorkers = Number.parseInt(workersInput.value || "4", 10);
    if (!Number.isInteger(maxWorkers) || maxWorkers < 1) {
      setWeekManageStatus("max-workers 必须大于等于 1", "error");
      return;
    }
    runBtn.disabled = true;
    try {
      const task = await runPipelineTask(taskType, selectedWeek.id, maxWorkers, Boolean(flagInput.checked));
      setWeekManageStatus(
        `${isPreprocess ? "前处理" : "批改"}任务已启动（${selectedWeek.name || selectedWeek.id}）`,
        "ok"
      );
      state.pipelineLatestByKey[cacheKey] = task;
      state.pipelineLogCursorByTaskId[task.taskId] = 0;
      state.pipelineStatusByTaskId[task.taskId] = task.status;
      appendDashboardLog(`${taskLabel(taskType)}启动：${selectedWeek.name || selectedWeek.id} | taskId=${task.taskId}`);
      renderDashboardSummaryCards();
    } catch (error) {
      setWeekManageStatus(`启动失败：${error.message}`, "error");
    } finally {
      runBtn.disabled = false;
    }
  });
}

async function syncPipelineTaskProgress(taskType, weekId) {
  const key = taskCacheKey(taskType, weekId);
  const latestTask = await loadLatestPipelineTask(taskType, weekId);
  const previousTask = state.pipelineLatestByKey[key] || null;
  state.pipelineLatestByKey[key] = latestTask;
  if (!latestTask || !latestTask.taskId) {
    return;
  }
  const taskId = latestTask.taskId;
  const sinceLine = Number(state.pipelineLogCursorByTaskId[taskId] || 0);
  try {
    const detail = await fetchPipelineTaskDetail(taskId, sinceLine, 80);
    const lines = Array.isArray(detail.lines) ? detail.lines : [];
    const totalLines = Number.isInteger(detail.totalLines) ? detail.totalLines : sinceLine + lines.length;
    state.pipelineLogCursorByTaskId[taskId] = totalLines;
    lines.forEach((line) => {
      const text = String(line || "").trim();
      if (!text || text.startsWith("[CMD]")) {
        return;
      }
      appendDashboardLog(`[${taskLabel(taskType)}] ${text}`);
    });
  } catch (error) {
    window.console.warn("sync pipeline task detail failed", error);
  }

  const prevStatus = state.pipelineStatusByTaskId[taskId];
  if (prevStatus !== latestTask.status) {
    state.pipelineStatusByTaskId[taskId] = latestTask.status;
    if (latestTask.status === "success") {
      appendDashboardLog(`[${taskLabel(taskType)}] 已完成（${weekId}）`);
    } else if (latestTask.status === "failed") {
      appendDashboardLog(`[${taskLabel(taskType)}] 失败：${latestTask.error || `退出码 ${latestTask.returnCode ?? "-"}`}`);
    } else if (!previousTask) {
      appendDashboardLog(`[${taskLabel(taskType)}] 任务运行中`);
    }
  }
}

async function pollPipelineProgress() {
  const selectedWeek = getSelectedWeek();
  if (!selectedWeek || state.pipelinePolling) {
    return;
  }
  state.pipelinePolling = true;
  try {
    await Promise.all([
      syncPipelineTaskProgress("preprocess", selectedWeek.id),
      syncPipelineTaskProgress("grading", selectedWeek.id),
    ]);
  } finally {
    state.pipelinePolling = false;
  }
}

function renderDashboardSummaryCards() {
  const selectedWeek = getSelectedWeek();

  if (currentWeekCardContentEl) {
    const nowText = new Date().toLocaleString("zh-CN", { hour12: false });
    currentWeekCardContentEl.innerHTML = selectedWeek
      ? `<div class="summary-week-title">
           <span class="summary-week-label">当前选中</span>
           <span class="summary-week-name">${selectedWeek.name || selectedWeek.id}</span>
           <span class="summary-week-time">${nowText}</span>
         </div>`
      : '<p class="dashboard-stat-placeholder">当前未选择周</p>';
  }

  if (weekResourceCardContentEl) {
    if (!selectedWeek) {
      weekResourceCardContentEl.innerHTML = '<p class="dashboard-stat-placeholder">先在左侧选中一周</p>';
    } else {
      weekResourceCardContentEl.innerHTML = `
        <p class="summary-tip">学生作业文件夹格式：解压后的学生级压缩包，也就是“一名学生一个 zip”，而不是整班总 zip。</p>
        <div class="summary-actions-grid">
          <button type="button" class="summary-action-btn" data-role="summary-open-raw">打开学生作业文件夹</button>
          <button type="button" class="summary-action-btn" data-role="summary-open-answer">打开 answer.tex</button>
          <button type="button" class="summary-action-btn" data-role="summary-copy-raw">复制学生作业文件夹路径</button>
          <button type="button" class="summary-action-btn" data-role="summary-copy-answer">复制 answer 路径</button>
        </div>
      `;
      weekResourceCardContentEl.querySelector('[data-role="summary-open-raw"]')?.addEventListener("click", () => {
        openWeekResource(selectedWeek.id, "raw_submissions");
      });
      weekResourceCardContentEl.querySelector('[data-role="summary-open-answer"]')?.addEventListener("click", () => {
        openWeekResource(selectedWeek.id, "answer_key");
      });
      weekResourceCardContentEl.querySelector('[data-role="summary-copy-raw"]')?.addEventListener("click", () => {
        selectedWeek.rawSubmissionsPath
          ? copyPathWithFeedback(selectedWeek.rawSubmissionsPath, "已复制学生作业文件夹路径")
          : copyWeekResourcePath(selectedWeek.id, "raw_submissions", "已复制学生作业文件夹路径");
      });
      weekResourceCardContentEl.querySelector('[data-role="summary-copy-answer"]')?.addEventListener("click", () => {
        selectedWeek.answerKeyPath
          ? copyPathWithFeedback(selectedWeek.answerKeyPath, "已复制 answer.tex 路径")
          : copyWeekResourcePath(selectedWeek.id, "answer_key", "已复制 answer.tex 路径");
      });
    }
  }

  if (deleteWeekCardContentEl) {
    if (!selectedWeek) {
      deleteWeekCardContentEl.innerHTML = '<p class="dashboard-stat-placeholder">先在左侧选中一周</p>';
    } else {
      deleteWeekCardContentEl.innerHTML = `
        <div class="summary-delete-actions">
          <button type="button" class="summary-delete-btn api" data-role="summary-config-api">配置 API Key</button>
          <button type="button" class="summary-delete-btn safe" data-role="summary-delete-safe">删除配置（安全）</button>
          <button type="button" class="summary-delete-btn danger" data-role="summary-delete-all">删除配置+周目录（危险）</button>
        </div>
      `;
      deleteWeekCardContentEl.querySelector('[data-role="summary-config-api"]')?.addEventListener("click", () => {
        configureApiKey();
      });
      deleteWeekCardContentEl.querySelector('[data-role="summary-delete-safe"]')?.addEventListener("click", () => {
        deleteWeekConservative(selectedWeek);
      });
      deleteWeekCardContentEl.querySelector('[data-role="summary-delete-all"]')?.addEventListener("click", () => {
        deleteWeekAggressive(selectedWeek);
      });
    }
  }

  if (preprocessTaskCardContentEl) {
    if (!selectedWeek) {
      preprocessTaskCardContentEl.innerHTML = '<p class="dashboard-stat-placeholder">先在左侧选中一周</p>';
    } else {
      const latestTask = state.pipelineLatestByKey[taskCacheKey("preprocess", selectedWeek.id)] || null;
      preprocessTaskCardContentEl.innerHTML = buildPipelineTaskCard("preprocess", selectedWeek, latestTask);
      bindPipelineTaskCard("preprocess", selectedWeek);
    }
  }

  if (dashboardLogsContentEl) {
    if (!state.dashboardLogs.length) {
      dashboardLogsContentEl.innerHTML = '<p class="dashboard-stat-placeholder">日志会显示在这里</p>';
    } else {
      const list = state.dashboardLogs
        .slice(0, 12)
        .map((line) => `<li class="summary-log-item">${line}</li>`)
        .join("");
      dashboardLogsContentEl.innerHTML = `<ul class="summary-log-list">${list}</ul>`;
    }
  }

  if (gradingTaskCardContentEl) {
    if (!selectedWeek) {
      gradingTaskCardContentEl.innerHTML = '<p class="dashboard-stat-placeholder">先在左侧选中一周</p>';
    } else {
      const latestTask = state.pipelineLatestByKey[taskCacheKey("grading", selectedWeek.id)] || null;
      gradingTaskCardContentEl.innerHTML = buildPipelineTaskCard("grading", selectedWeek, latestTask);
      bindPipelineTaskCard("grading", selectedWeek);
    }
  }

  if (exportEngineCardContentEl) {
    const engineLabel = state.exportEngine === "latex" ? "LaTeX" : "KaTeX";
    const latexStatus = state.latexStatus || {};
    const platformLabel = String(latexStatus.platform?.label || latexStatus.platform?.system || "当前系统");
    const latexAvailable = Boolean(latexStatus.available);
    const latexDetail = String(latexStatus.detail || "").trim();
    const latexHint = String(latexStatus.hint || "").trim();
    exportEngineCardContentEl.innerHTML = `
      <div class="export-engine-card">
        <p class="summary-tip">选择默认图片导出引擎。LaTeX 依赖本机 <code>lualatex</code>，KaTeX 使用本地 Playwright 渲染并截图。</p>
        <select id="exportEngineSelect" class="export-engine-select">
          <option value="latex" ${state.exportEngine === "latex" ? "selected" : ""}>LaTeX</option>
          <option value="katex" ${state.exportEngine === "katex" ? "selected" : ""}>KaTeX</option>
        </select>
        <div class="export-engine-actions">
          <button id="saveExportEngineBtn" type="button" class="config-btn primary">保存默认引擎</button>
        </div>
        <p class="export-engine-status">当前默认：${engineLabel}${state.exportSettingsLoaded ? "" : "（未加载完成）"}</p>
        <p class="export-engine-status">${platformLabel} LaTeX 检测：${latexAvailable ? "可用" : "不可用"}</p>
        ${latexDetail ? `<p class="export-engine-status">${latexDetail}</p>` : ""}
        ${latexHint ? `<p class="export-engine-status">${latexHint}</p>` : ""}
      </div>
    `;
    exportEngineCardContentEl.querySelector("#saveExportEngineBtn")?.addEventListener("click", () => {
      saveExportSettings().catch((error) => window.alert(`保存导出设置失败：${error.message}`));
    });
  }

  applyDashboardCardSizing();
}

function applyDashboardCardSizing() {
  [cardCurrentWeekEl, cardWeekResourceEl, cardDeleteWeekEl, cardPreprocessTaskEl, cardDashboardLogsEl, cardGradingTaskEl, cardExportEngineEl].forEach((card) => {
    if (!card) return;
    card.classList.remove("span-2", "span-4", "align-with-week-resource");
  });
  if (cardWeekResourceEl) {
    cardWeekResourceEl.classList.add("span-2");
  }
  if (cardDashboardLogsEl) {
    cardDashboardLogsEl.classList.add("span-2");
    cardDashboardLogsEl.classList.add("align-with-week-resource");
  }
  if (window.innerWidth < 1180) {
    if (cardWeekResourceEl) cardWeekResourceEl.classList.remove("span-2");
    if (cardDashboardLogsEl) cardDashboardLogsEl.classList.remove("span-2", "align-with-week-resource");
  }
}

async function configureApiKey() {
  let envName = "DASHSCOPE_API_KEY";
  let apiKey = "";
  let hasApiKey = false;
  try {
    if (!state.subjectsLoaded) {
      await loadSubjectsJson();
    }
    const value = String(state.subjectsData?.api_key_env || "").trim();
    if (value) {
      envName = value;
    }
  } catch (error) {
    window.console.warn("load subjects before configuring api key failed", error);
  }
  try {
    const data = await fetchJson(`/api/apikey?env=${encodeURIComponent(envName)}`);
    envName = String(data.envName || envName).trim() || envName;
    hasApiKey = Boolean(data.hasApiKey);
  } catch (error) {
    setWeekManageStatus(`读取本地 API Key 失败：${error.message}`, "error");
  }

  showApiKeyModal(envName, apiKey, hasApiKey);
  setWeekManageStatus(`已打开 API Key 配置：${envName}`);
}

function getClientPlatform() {
  const ua = `${navigator.userAgent || ""} ${navigator.platform || ""}`.toLowerCase();
  return ua.includes("win") ? "windows" : "linux";
}

function escapeShellDoubleQuoted(value) {
  return String(value || "").replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function fillApiKeyModalCommands(envName, apiKey, hasApiKey = Boolean(apiKey)) {
  const keyValue = String(apiKey || "").trim() || "你的密钥";
  const escaped = escapeShellDoubleQuoted(keyValue);
  const linuxCommand = `export ${envName}="${escaped}"`;
  const powershellCommand = `$env:${envName}="${escaped}"`;
  const cmdCommand = `set ${envName}=${keyValue}`;
  const platform = getClientPlatform();

  if (apiKeyEnvNameEl) {
    apiKeyEnvNameEl.textContent = envName;
  }
  if (apiKeyPlatformHintEl) {
    apiKeyPlatformHintEl.textContent =
      platform === "windows"
        ? "检测到当前是 Windows 环境，优先使用 PowerShell 或 CMD 命令。"
        : "检测到当前是 Linux/macOS 环境，优先使用 bash 命令。";
  }
  if (apiKeyInputEl) {
    apiKeyInputEl.value = apiKey || "";
  }
  if (apiKeyStatusEl) {
    setConfigStatus(
      apiKeyStatusEl,
      apiKey ? "已读取本地 API Key" : hasApiKey ? "已保存 API Key（不会回显）" : "尚未保存 API Key",
    );
  }
  if (apiCmdLinuxEl) {
    apiCmdLinuxEl.value = linuxCommand;
  }
  if (apiCmdPowershellEl) {
    apiCmdPowershellEl.value = powershellCommand;
  }
  if (apiCmdCmdEl) {
    apiCmdCmdEl.value = cmdCommand;
  }
}

function showApiKeyModal(envName, apiKey, hasApiKey = Boolean(apiKey)) {
  if (!apiKeyModalEl) {
    return;
  }
  fillApiKeyModalCommands(envName, apiKey, hasApiKey);
  apiKeyModalEl.classList.remove("is-hidden");
}

function closeApiKeyModal() {
  if (!apiKeyModalEl) {
    return;
  }
  apiKeyModalEl.classList.add("is-hidden");
}

async function copyApiKeyCommand(commandEl, message) {
  if (!commandEl) {
    return;
  }
  try {
    await navigator.clipboard.writeText(commandEl.value);
    setWeekManageStatus(message, "ok");
  } catch (error) {
    setWeekManageStatus(`复制失败：${error.message}`, "error");
  }
}

async function saveApiKeyToLocal() {
  const envName = String(apiKeyEnvNameEl?.textContent || "").trim();
  const apiKey = String(apiKeyInputEl?.value || "").trim();
  if (!envName) {
    setConfigStatus(apiKeyStatusEl, "环境变量名为空", "error");
    return;
  }
  if (!apiKey) {
    setConfigStatus(apiKeyStatusEl, "请先输入 API Key", "error");
    return;
  }
  setConfigStatus(apiKeyStatusEl, "保存中...");
  try {
    const data = await fetchJson("/api/apikey", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ envName, apiKey }),
    });
    fillApiKeyModalCommands(envName, apiKey, true);
    setConfigStatus(apiKeyStatusEl, `已保存到 ${data.storePath || "configs/env/local.env"}`, "ok");
    setWeekManageStatus(`已保存 ${envName} 到本地环境文件`, "ok");
  } catch (error) {
    setConfigStatus(apiKeyStatusEl, `保存失败：${error.message}`, "error");
    setWeekManageStatus(`保存 API Key 失败：${error.message}`, "error");
  }
}

async function copyApiKeyValue() {
  const value = String(apiKeyInputEl?.value || "").trim();
  if (!value) {
    setConfigStatus(apiKeyStatusEl, "当前没有可复制的 API Key", "error");
    return;
  }
  try {
    await navigator.clipboard.writeText(value);
    setConfigStatus(apiKeyStatusEl, "API Key 已复制", "ok");
    setWeekManageStatus("API Key 已复制", "ok");
  } catch (error) {
    setConfigStatus(apiKeyStatusEl, `复制失败：${error.message}`, "error");
  }
}

async function copyPathWithFeedback(path, message) {
  if (!path) {
    setWeekManageStatus("路径未加载，正在请求...", "normal");
    return;
  }
  try {
    await navigator.clipboard.writeText(path);
    setWeekManageStatus(message, "ok");
  } catch (error) {
    setWeekManageStatus(`复制失败：${error.message}`, "error");
  }
}

async function createWeek() {
  const weekName = String(newWeekNameInputEl?.value || "").trim();
  if (!weekName) {
    setWeekManageStatus("请输入周名称", "error");
    return;
  }
  setWeekManageStatus("创建中...");
  try {
    const response = await fetch("/api/weeks/create", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ weekName }),
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    if (newWeekNameInputEl) {
      newWeekNameInputEl.value = "";
    }
    await loadWeeks();
    state.selectedWeekId = data.weekId;
    renderWeekButtons();
    setWeekManageStatus(`已创建：${data.weekName}`, "ok");
  } catch (error) {
    setWeekManageStatus(`创建失败：${error.message}`, "error");
  }
}

async function deleteWeekConservative(week) {
  const weekId = String(week?.id || "");
  if (!weekId) {
    return;
  }
  const confirmed = window.confirm(
    `将删除 assignment 配置 ${weekId}.json。\n不会删除周目录、学生作业文件夹、processed_images、results。\n是否继续？`,
  );
  if (!confirmed) {
    return;
  }
  const confirmText = window.prompt(`请输入确认文本：DELETE ${weekId}`);
  if (confirmText !== `DELETE ${weekId}`) {
    setWeekManageStatus("删除已取消：确认文本不匹配", "error");
    return;
  }
  setWeekManageStatus("删除中...");
  try {
    const response = await fetch("/api/weeks/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ weekId, confirm: confirmText }),
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    await loadWeeks();
    if (state.selectedWeekId === weekId) {
      state.selectedWeekId = state.weeks.length ? state.weeks[0].id : null;
    }
    renderWeekButtons();
    setWeekManageStatus(`已删除 ${weekId}.json（仅配置）`, "ok");
  } catch (error) {
    setWeekManageStatus(`删除失败：${error.message}`, "error");
  }
}

async function deleteWeekAggressive(week) {
  const weekId = String(week?.id || "");
  const weekName = String(week?.name || weekId);
  if (!weekId) {
    return;
  }
  const confirmed = window.confirm(
    `危险操作：将删除 ${weekName} 的 assignment 配置和周目录。\n学生作业文件夹/processed_images/results 都会被删除。\n是否继续？`,
  );
  if (!confirmed) {
    return;
  }
  const confirmText = window.prompt(`请输入确认文本：DELETE ALL ${weekId}`);
  if (confirmText !== `DELETE ALL ${weekId}`) {
    setWeekManageStatus("删除已取消：确认文本不匹配", "error");
    return;
  }
  setWeekManageStatus("高风险删除中...");
  try {
    const response = await fetch("/api/weeks/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ weekId, confirm: confirmText, mode: "assignment_and_week_dir" }),
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    await loadWeeks();
    if (state.selectedWeekId === weekId) {
      state.selectedWeekId = state.weeks.length ? state.weeks[0].id : null;
    }
    renderWeekButtons();
    setWeekManageStatus(`已删除 ${weekId}（配置+周目录）`, "ok");
  } catch (error) {
    setWeekManageStatus(`删除失败：${error.message}`, "error");
  }
}

async function openWeekResource(weekId, target) {
  setWeekManageStatus("尝试打开中...");
  try {
    const response = await fetch("/api/weeks/open", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ weekId, target }),
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    if (data.opened) {
      setWeekManageStatus(`已尝试打开：${data.path}`, "ok");
      return;
    }
    await copyPathWithFeedback(data.path, `无法自动打开，已复制路径：${data.path}`);
  } catch (error) {
    setWeekManageStatus(`打开失败：${error.message}`, "error");
  }
}

async function copyWeekResourcePath(weekId, target, message) {
  try {
    const data = await fetchJson("/api/weeks/path", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ weekId, target }),
    });
    await copyPathWithFeedback(data.path, message);
  } catch (error) {
    setWeekManageStatus(`复制失败：${error.message}`, "error");
  }
}

async function loadPromptFile() {
  if (!promptEditorEl) {
    return;
  }
  setConfigStatus(promptSaveStatusEl, "加载中...");
  try {
    const data = await fetchJson("/api/prompt");
    promptEditorEl.value = String(data.content || "");
    state.promptLoaded = true;
    updatePromptMeta("已加载");
    setConfigStatus(promptSaveStatusEl, "已加载", "ok");
  } catch (error) {
    updatePromptMeta("加载失败");
    setConfigStatus(promptSaveStatusEl, `加载失败：${error.message}`, "error");
    throw error;
  }
}

async function loadExportSettings() {
  try {
    const data = await fetchJson("/api/export-settings");
    const engine = String(data.exportEngine || "").trim().toLowerCase();
    state.exportEngine = engine === "latex" ? "latex" : "katex";
    state.latexStatus = data.latexStatus && typeof data.latexStatus === "object" ? data.latexStatus : null;
    state.exportSettingsLoaded = true;
    renderDashboardSummaryCards();
    return data;
  } catch (error) {
    state.exportEngine = "katex";
    state.exportSettingsLoaded = false;
    state.latexStatus = null;
    renderDashboardSummaryCards();
    throw error;
  }
}

async function saveExportSettings() {
  const selectEl = document.getElementById("exportEngineSelect");
  const exportEngine = String(selectEl?.value || state.exportEngine || "katex").trim().toLowerCase();
  const data = await fetchJson("/api/export-settings", {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify({ exportEngine }),
  });
  state.exportEngine = String(data.exportEngine || exportEngine).trim().toLowerCase() === "latex" ? "latex" : "katex";
  state.latexStatus = data.latexStatus && typeof data.latexStatus === "object" ? data.latexStatus : null;
  state.exportSettingsLoaded = true;
  renderDashboardSummaryCards();
  if (state.exportEngine === "latex" && state.latexStatus && !state.latexStatus.available) {
    const detail = String(state.latexStatus.detail || "").trim();
    const hint = String(state.latexStatus.hint || "").trim();
    window.alert(`已保存默认导出引擎为 LaTeX，但当前环境检测为不可用。${detail ? `\n${detail}` : ""}${hint ? `\n${hint}` : ""}`);
  }
  setWeekManageStatus(`已保存默认导出引擎：${state.exportEngine === "latex" ? "LaTeX" : "KaTeX"}`, "ok");
  return data;
}

async function viewPromptTemplate() {
  state.promptEditable = false;
  if (!state.promptLoaded) {
    await loadPromptFile();
  }
  setPromptEditorVisible(true);
  updatePromptMeta("查看中");
}

async function editPromptTemplate() {
  state.promptEditable = true;
  if (!state.promptLoaded) {
    await loadPromptFile();
  }
  setPromptEditorVisible(true);
  promptEditorEl.disabled = false;
  promptEditorEl.focus();
  updatePromptMeta("编辑中");
}

async function savePromptFile() {
  if (!promptEditorEl) {
    return;
  }
  const content = promptEditorEl.value;
  const placeholders = ["{subject_name}", "{standard_answer}", "{grading_requirements}", "{output_format}"];
  const missing = placeholders.filter((item) => !content.includes(item));
  if (missing.length) {
    const confirmed = window.confirm(
      `检测到以下占位符缺失：${missing.join("、")}。\n这些通常是系统变量，建议保留。确定仍然保存吗？`,
    );
    if (!confirmed) {
      return;
    }
  }
  setConfigStatus(promptSaveStatusEl, "保存中...");
  try {
    const response = await fetch("/api/prompt", {
      method: "POST",
      headers: { "Content-Type": "text/plain; charset=utf-8" },
      body: content,
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `HTTP ${response.status}`);
    }
    setConfigStatus(promptSaveStatusEl, "已保存到 prompts/default_prompt.txt", "ok");
    updatePromptMeta("已保存");
  } catch (error) {
    setConfigStatus(promptSaveStatusEl, `保存失败：${error.message}`, "error");
    window.alert(`Prompt 保存失败：${error.message}`);
  }
}

async function resetPromptFile() {
  setConfigStatus(promptSaveStatusEl, "恢复默认中...");
  try {
    const response = await fetch("/api/prompt/reset", { method: "POST" });
    const raw = await response.text();
    let data = {};
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch (error) {
      if (!response.ok) {
        throw new Error(`接口不可用（HTTP ${response.status}），请重启 review_app.py 后重试`);
      }
      throw new Error("接口返回了非 JSON 内容，请检查后端日志");
    }
    if (!response.ok || data.error) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    promptEditorEl.value = String(data.content || "");
    state.promptLoaded = true;
    setPromptEditorVisible(true);
    updatePromptMeta("已恢复默认");
    setConfigStatus(promptSaveStatusEl, "已恢复默认模板", "ok");
  } catch (error) {
    setConfigStatus(promptSaveStatusEl, `恢复失败：${error.message}`, "error");
    window.alert(`恢复默认失败：${error.message}`);
  }
}

async function loadSubjectsJson() {
  if (!subjectsEditorEl) {
    return;
  }
  setConfigStatus(subjectsSaveStatusEl, "加载中...");
  try {
    const data = await fetchJson("/api/subjects");
    const parsed = JSON.parse(String(data.content || "{}"));
    const payload = ensureSubjectsObject(parsed);
    state.subjectsData = payload;
    state.subjectsLoaded = true;
    writeSubjectsForm(payload);
    syncSubjectsJsonFromData();
    setSubjectsMode("form");
    setConfigStatus(subjectsSaveStatusEl, "已加载", "ok");
  } catch (error) {
    setConfigStatus(subjectsSaveStatusEl, `加载失败：${error.message}`, "error");
    throw error;
  }
}

async function postSubjectsData(data) {
  const body = `${JSON.stringify(data, null, 2)}\n`;
  const response = await fetch("/api/subjects", {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body,
  });
  const result = await response.json();
  if (!response.ok || result.error) {
    throw new Error(result.error || `HTTP ${response.status}`);
  }
}

async function saveSubjectsForm() {
  syncSubjectsDataFromForm();
  const validation = validateSubjectsData(state.subjectsData);
  if (!validation.ok) {
    setConfigStatus(subjectsSaveStatusEl, `校验失败：${validation.message}`, "error");
    window.alert(`保存失败：${validation.message}`);
    return;
  }
  setConfigStatus(subjectsSaveStatusEl, "保存中...");
  try {
    await postSubjectsData(state.subjectsData);
    syncSubjectsJsonFromData();
    state.subjectsLoaded = true;
    setConfigStatus(subjectsSaveStatusEl, "subjects.json 保存成功", "ok");
  } catch (error) {
    setConfigStatus(subjectsSaveStatusEl, `保存失败：${error.message}`, "error");
    window.alert(`保存失败：${error.message}`);
  }
}

async function saveSubjectsJson() {
  if (!subjectsEditorEl) {
    return;
  }
  let parsed;
  try {
    parsed = JSON.parse(subjectsEditorEl.value);
  } catch (error) {
    setConfigStatus(subjectsSaveStatusEl, `JSON 格式错误：${error.message}`, "error");
    window.alert(`JSON 格式错误：${error.message}`);
    return;
  }
  const validation = validateSubjectsData(parsed);
  if (!validation.ok) {
    setConfigStatus(subjectsSaveStatusEl, `校验失败：${validation.message}`, "error");
    window.alert(`保存失败：${validation.message}`);
    return;
  }
  setConfigStatus(subjectsSaveStatusEl, "保存中...");
  try {
    await postSubjectsData(parsed);
    state.subjectsData = ensureSubjectsObject(parsed);
    writeSubjectsForm(state.subjectsData);
    syncSubjectsJsonFromData();
    state.subjectsLoaded = true;
    setConfigStatus(subjectsSaveStatusEl, "subjects.json 保存成功", "ok");
  } catch (error) {
    setConfigStatus(subjectsSaveStatusEl, `保存失败：${error.message}`, "error");
    window.alert(`保存失败：${error.message}`);
  }
}

function toggleSubjectsJsonMode() {
  if (!state.subjectsLoaded) {
    window.alert("请先点击“读取配置”。");
    return;
  }
  if (state.subjectsMode === "form") {
    syncSubjectsDataFromForm();
    setSubjectsMode("json");
    return;
  }
  try {
    const parsed = JSON.parse(subjectsEditorEl.value);
    state.subjectsData = ensureSubjectsObject(parsed);
    writeSubjectsForm(state.subjectsData);
    setSubjectsMode("form");
  } catch (error) {
    setConfigStatus(subjectsSaveStatusEl, `JSON 格式错误：${error.message}`, "error");
    window.alert(`JSON 格式错误：${error.message}`);
  }
}

function initSubjectsFormListeners() {
  [
    subjectIdInputEl,
    subjectNameInputEl,
    subjectModelInputEl,
    subjectBaseUrlInputEl,
    subjectGradingRequirementsInputEl,
  ]
    .filter(Boolean)
    .forEach((el) => {
      el.addEventListener("input", () => {
        if (state.subjectsLoaded) {
          syncSubjectsDataFromForm();
          setConfigStatus(subjectsSaveStatusEl, "未保存");
        }
      });
    });
}

function initPromptAndSubjectsPanels() {
  updatePromptMeta("未加载");
  setPromptEditorVisible(false);
  setSubjectsMode("form");
  setConfigStatus(promptSaveStatusEl, "请先查看或编辑模板");
  setConfigStatus(subjectsSaveStatusEl, "请先读取配置");
  setWeekManageStatus("");
  initSubjectsFormListeners();
  if (promptEditorEl) {
    promptEditorEl.addEventListener("input", () => {
      if (state.promptLoaded) {
        setConfigStatus(promptSaveStatusEl, "未保存");
      }
    });
  }
  if (subjectsEditorEl) {
    subjectsEditorEl.addEventListener("input", () => {
      if (state.subjectsLoaded && state.subjectsMode === "json") {
        setConfigStatus(subjectsSaveStatusEl, "未保存");
      }
    });
  }
}

async function ensureDashboardConfigsLoaded() {
  if (!state.exportSettingsLoaded) {
    try {
      await loadExportSettings();
    } catch (error) {
      window.console.error(error);
    }
  }
  if (!state.promptLoaded) {
    try {
      await loadPromptFile();
    } catch (error) {
      window.console.error(error);
    }
  }
  if (!state.subjectsLoaded) {
    try {
      await loadSubjectsJson();
    } catch (error) {
      window.console.error(error);
    }
  }
}

function startPipelinePolling() {
  if (state.pipelinePollTimer) {
    return;
  }
  state.pipelinePollTimer = window.setInterval(() => {
    if (state.currentView !== "dashboard") {
      return;
    }
    pollPipelineProgress().catch((error) => window.console.warn("pipeline polling failed", error));
  }, 1500);
}

async function enterDashboardWithConfigInit() {
  switchView("dashboard");
  await ensureDashboardConfigsLoaded();
  startPipelinePolling();
  await pollPipelineProgress();
}

function selectWeek(weekId) {
  const selectedId = String(weekId || "").trim();
  if (!selectedId) {
    return;
  }
  state.selectedWeekId = selectedId;
  renderWeekButtons();
  const selectedWeek = getSelectedWeek();
  if (selectedWeek) {
    appendDashboardLog(`已选中 ${selectedWeek.name || selectedWeek.id}`);
    pollPipelineProgress().catch((error) => window.console.warn("poll pipeline after select failed", error));
  }
}

async function activateSelectedWeek() {
  const targetWeekId = state.selectedWeekId || state.loadedWeekId;
  if (!targetWeekId) {
    return;
  }

  if (targetWeekId !== state.loadedWeekId) {
    state.students = [];
    state.filteredStudents = [];
    state.currentStudentId = null;
    studentListEl.innerHTML = '<div class="empty-state">切换周次中...</div>';
    imagesContainerEl.innerHTML = '<div class="empty-state">切换周次中...</div>';
    modulesContainerEl.innerHTML = '<div class="empty-state">切换周次中...</div>';

    await fetchJson(`/api/switch-week/${encodeURIComponent(targetWeekId)}`);
    state.loadedWeekId = targetWeekId;
  }

  if (!state.students.length) {
    await loadStudents();
  }
}

async function openReviewView() {
  switchView("review");
  try {
    await activateSelectedWeek();
  } catch (error) {
    studentCountEl.textContent = "加载失败";
    imagesContainerEl.innerHTML = `<div class="empty-state">${error.message}</div>`;
    modulesContainerEl.innerHTML = `<div class="empty-state">${error.message}</div>`;
    updateSaveStatus("加载失败");
    return;
  }
}

async function loadStudent(studentId, silent = false) {
  const loadToken = state.studentLoadToken + 1;
  state.studentLoadToken = loadToken;
  clearExportImageStatusPolling();
  const data = await fetchJson(`/api/student/${encodeURIComponent(studentId)}`);
  if (loadToken !== state.studentLoadToken) {
    return;
  }
  const payload = normalizePayload(data.resultJson);
  state.currentStudentId = data.id;
  state.currentReview = data.review || null;
  studentTitleEl.textContent = data.id;
  pageMetaEl.textContent = `${data.images.length} 页图片`;
  renderImages(data.images, data.imageVariants || []);
  renderModules(payload);
  renderReviewRiskPanel(state.currentReview);
  renderStudentList();
  const exportStatus = applyExportImageStatus(data.exportImage);
  fetchExportImageStatus(data.id, { priorityHigh: true, enqueue: true }).catch((error) => {
    window.console.warn("refresh current export image status failed", error);
  });
  warmNearbyExportImages(data.id);
  if (exportStatus?.queued || exportStatus?.rendering) {
    updateSaveStatus("已加载，正在预生成图片...");
    startExportImageStatusPolling();
  } else if (exportStatus?.ready) {
    updateSaveStatus("已加载");
  } else if (exportStatus?.error) {
    updateSaveStatus("图片生成失败");
  } else {
    updateSaveStatus("已加载");
  }
}


function moveStudent(offset) {
  if (!state.filteredStudents.length) {
    return;
  }
  const index = currentIndex();
  if (index === -1) {
    loadStudent(state.filteredStudents[0].id);
    return;
  }
  const nextIndex = index + offset;
  if (nextIndex < 0 || nextIndex >= state.filteredStudents.length) {
    return;
  }
  loadStudent(state.filteredStudents[nextIndex].id);
}

studentSearchEl.addEventListener("input", renderStudentList);
if (reviewFilterEl) {
  reviewFilterEl.addEventListener("change", renderStudentList);
}
prevStudentBtnEl.addEventListener("click", () => moveStudent(-1));
nextStudentBtnEl.addEventListener("click", () => moveStudent(1));
if (regenerateImageBtnEl) {
  regenerateImageBtnEl.addEventListener("click", regenerateCurrentExportImage);
}
if (exportImageBtnEl) {
  exportImageBtnEl.addEventListener("click", exportAnnotationsAsImage);
}
if (copyImageBtnEl) {
  copyImageBtnEl.addEventListener("click", copyAnnotationsImage);
}
if (viewPromptBtnEl) {
  viewPromptBtnEl.addEventListener("click", () => {
    viewPromptTemplate().catch((error) => window.alert(`加载 Prompt 失败：${error.message}`));
  });
}
if (editPromptBtnEl) {
  editPromptBtnEl.addEventListener("click", () => {
    editPromptTemplate().catch((error) => window.alert(`加载 Prompt 失败：${error.message}`));
  });
}
if (savePromptBtnEl) {
  savePromptBtnEl.addEventListener("click", savePromptFile);
}
if (resetPromptBtnEl) {
  resetPromptBtnEl.addEventListener("click", resetPromptFile);
}
if (loadSubjectsBtnEl) {
  loadSubjectsBtnEl.addEventListener("click", () => {
    loadSubjectsJson().catch((error) => window.alert(`读取配置失败：${error.message}`));
  });
}
if (saveSubjectsFormBtnEl) {
  saveSubjectsFormBtnEl.addEventListener("click", saveSubjectsForm);
}
if (toggleSubjectsJsonBtnEl) {
  toggleSubjectsJsonBtnEl.addEventListener("click", toggleSubjectsJsonMode);
}
if (saveSubjectsJsonBtnEl) {
  saveSubjectsJsonBtnEl.addEventListener("click", saveSubjectsJson);
}
if (createWeekBtnEl) {
  createWeekBtnEl.addEventListener("click", createWeek);
}
if (newWeekNameInputEl) {
  newWeekNameInputEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      createWeek();
    }
  });
}
if (closeApiKeyModalBtnEl) {
  closeApiKeyModalBtnEl.addEventListener("click", closeApiKeyModal);
}
if (apiKeyModalEl) {
  apiKeyModalEl.addEventListener("click", (event) => {
    if (event.target === apiKeyModalEl) {
      closeApiKeyModal();
    }
  });
}
if (copyApiCmdLinuxBtnEl) {
  copyApiCmdLinuxBtnEl.addEventListener("click", () => {
    copyApiKeyCommand(apiCmdLinuxEl, "已复制 Linux API Key 命令");
  });
}
if (copyApiCmdPowershellBtnEl) {
  copyApiCmdPowershellBtnEl.addEventListener("click", () => {
    copyApiKeyCommand(apiCmdPowershellEl, "已复制 PowerShell API Key 命令");
  });
}
if (copyApiCmdCmdBtnEl) {
  copyApiCmdCmdBtnEl.addEventListener("click", () => {
    copyApiKeyCommand(apiCmdCmdEl, "已复制 CMD API Key 命令");
  });
}
if (saveApiKeyBtnEl) {
  saveApiKeyBtnEl.addEventListener("click", saveApiKeyToLocal);
}
if (copyApiKeyBtnEl) {
  copyApiKeyBtnEl.addEventListener("click", copyApiKeyValue);
}
if (apiKeyInputEl) {
  apiKeyInputEl.addEventListener("input", () => {
    const envName = String(apiKeyEnvNameEl?.textContent || "").trim();
    fillApiKeyModalCommands(envName || "DASHSCOPE_API_KEY", apiKeyInputEl.value);
    setConfigStatus(apiKeyStatusEl, "未保存");
  });
}



window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && apiKeyModalEl && !apiKeyModalEl.classList.contains("is-hidden")) {
    closeApiKeyModal();
  }
});

window.addEventListener("resize", () => {
  applyDashboardCardSizing();
});

initPromptAndSubjectsPanels();
initLayoutControls();
initNavTabs();
enterDashboardWithConfigInit();
