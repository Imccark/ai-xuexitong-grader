const state = {
  students: [],
  filteredStudents: [],
  currentStudentId: null,
  originalPayload: null,
  isSaving: false,
  moduleEditorListenersBound: false,
  itemUiState: {},
  nextItemId: 1,
};
const ITEM_MUTABLE_MODULE_KEYWORDS = ["错误细节", "证明题审查", "改进建议"];

const studentCountEl = document.getElementById("studentCount");
const studentListEl = document.getElementById("studentList");
const studentSearchEl = document.getElementById("studentSearch");
const studentTitleEl = document.getElementById("studentTitle");
const pageMetaEl = document.getElementById("pageMeta");
const imagesContainerEl = document.getElementById("imagesContainer");
const modulesContainerEl = document.getElementById("modulesContainer");
const saveStatusEl = document.getElementById("saveStatus");
const saveBtnEl = document.getElementById("saveBtn");
const prevStudentBtnEl = document.getElementById("prevStudentBtn");
const nextStudentBtnEl = document.getElementById("nextStudentBtn");
const exportImageBtnEl = document.getElementById("exportImageBtn");
const appShellEl = document.getElementById("appShell");
const mainLayoutEl = document.getElementById("mainLayout");
const mainResizeHandleEl = document.getElementById("mainResizeHandle");
const toggleSidebarBtnEl = document.getElementById("toggleSidebarBtn");
const restoreSidebarBtnEl = document.getElementById("restoreSidebarBtn");

const SIDEBAR_COLLAPSED_KEY = "review_ui_sidebar_collapsed";
const MAIN_LEFT_WIDTH_KEY = "review_ui_main_left_width";

function fetchJson(url, options) {
  return fetch(url, options).then((response) => {
    if (!response.ok) {
      throw new Error(`请求失败: ${response.status}`);
    }
    return response.json();
  });
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

function payloadSignature(payload) {
  return JSON.stringify(payload);
}

function canonicalizePayload(payload) {
  const safePayload = payload && typeof payload === "object" ? payload : {};
  const safeModules = safePayload.modules && typeof safePayload.modules === "object" ? safePayload.modules : {};
  const normalizedModules = {};

  Object.keys(safeModules)
    .sort()
    .forEach((moduleName) => {
      const moduleData = safeModules[moduleName] && typeof safeModules[moduleName] === "object" ? safeModules[moduleName] : {};
      const items = Array.isArray(moduleData.items)
        ? moduleData.items.map((item) => String(item).trim()).filter(Boolean)
        : [];
      normalizedModules[moduleName] = {
        items,
      };
    });

  return {
    student_name_or_id: String(safePayload.student_name_or_id || "").trim(),
    overall: String(safePayload.overall || "").trim(),
    modules: normalizedModules,
  };
}

function getCurrentPayloadFromUI() {
  const studentNameInput = document.querySelector('[data-role="student-name"]');
  const overallInput = document.querySelector('[data-role="overall"]');
  const moduleCards = modulesContainerEl.querySelectorAll(".module-card[data-module]");

  const modules = {};
  moduleCards.forEach((card) => {
    const moduleName = card.dataset.module;
    const rows = card.querySelectorAll(".item-row[data-item-id]");
    const items = [];
    rows.forEach((row) => {
      const itemId = row.dataset.itemId;
      const itemState = itemId ? state.itemUiState[itemId] : null;
      const value = itemState ? String(itemState.rawText || "").trim() : "";
      if (value) {
        items.push(value);
      }
    });
    modules[moduleName] = {
      raw_text: items.join("\n"),
      items,
    };
  });

  return {
    student_name_or_id: studentNameInput ? studentNameInput.value.trim() : "",
    overall: overallInput ? overallInput.value.trim() : "",
    modules,
  };
}

function isDirty() {
  if (!state.originalPayload) {
    return false;
  }
  const currentPayload = canonicalizePayload(getCurrentPayloadFromUI());
  const originalPayload = canonicalizePayload(state.originalPayload);
  return payloadSignature(currentPayload) !== payloadSignature(originalPayload);
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

function initLayoutControls() {
  const collapsedSaved = window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1";
  setSidebarCollapsed(collapsedSaved);

  if (toggleSidebarBtnEl) {
    toggleSidebarBtnEl.addEventListener("click", () => {
      const isCollapsed = appShellEl?.classList.contains("sidebar-collapsed");
      setSidebarCollapsed(!isCollapsed);
    });
  }

  if (restoreSidebarBtnEl) {
    restoreSidebarBtnEl.addEventListener("click", () => setSidebarCollapsed(false));
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

  const onPointerDown = (event) => {
    if (window.matchMedia("(max-width: 1080px)").matches) {
      return;
    }
    event.preventDefault();
    const rect = mainLayoutEl.getBoundingClientRect();
    const style = getComputedStyle(mainLayoutEl);
    const currentLeftWidth = parseFloat(style.getPropertyValue("--main-left-width")) || rect.width * 0.58;
    const startX = event.clientX;
    const minLeft = 360;
    const minRight = 360;
    const handleWidth = mainResizeHandleEl.getBoundingClientRect().width || 10;
    const maxLeft = rect.width - minRight - handleWidth;

    mainResizeHandleEl.classList.add("is-dragging");
    mainResizeHandleEl.setPointerCapture(event.pointerId);

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
}

function currentIndex() {
  return state.filteredStudents.findIndex((student) => student.id === state.currentStudentId);
}

function renderStudentList() {
  studentListEl.innerHTML = "";
  const keyword = studentSearchEl.value.trim().toLowerCase();
  state.filteredStudents = state.students.filter((student) => student.id.toLowerCase().includes(keyword));
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
      <h3>${student.id}</h3>
      <p class="student-meta">${student.pageCount} 页 · ${student.hasResult ? "已有结果" : "待写结果"}</p>
    `;
    button.addEventListener("click", () => loadStudent(student.id));
    studentListEl.appendChild(button);
  });
}

function renderImages(images) {
  imagesContainerEl.innerHTML = "";
  if (!images.length) {
    imagesContainerEl.innerHTML = '<div class="empty-state">该学生暂无图片。</div>';
    return;
  }

  images.forEach((imageUrl, index) => {
    const wrapper = document.createElement("article");
    wrapper.className = "image-card";
    wrapper.innerHTML = `
      <h3>第 ${index + 1} 页</h3>
      <img src="${encodeURI(imageUrl)}" alt="第 ${index + 1} 页作业" loading="lazy" />
    `;
    imagesContainerEl.appendChild(wrapper);
  });
}

function autoResizeTextarea(textarea) {
  if (!textarea) {
    return;
  }
  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight}px`;
}

function renderMarkdownLatex(rawText, previewEl) {
  if (!previewEl) {
    return;
  }
  const source = String(rawText || "");
  const tokens = [];
  let tokenIndex = 0;
  const pushToken = (expr, displayMode) => {
    const key = `@@MATH_${tokenIndex++}@@`;
    tokens.push({ key, expr: String(expr || ""), displayMode });
    return key;
  };

  // 先抽离公式，避免 Markdown 解析破坏 LaTeX 内容。
  const sourceWithTokens = source
    .replace(/\$\$([\s\S]+?)\$\$/g, (_, expr) => pushToken(expr, true))
    .replace(/\$([^\n$]+?)\$/g, (_, expr) => pushToken(expr, false));

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
      const rendered = katex.renderToString(token.expr, {
        displayMode: token.displayMode,
        throwOnError: false,
      });
      safeHtml = safeHtml.replaceAll(token.key, rendered);
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

function getItemState(itemId) {
  if (!itemId || !state.itemUiState[itemId]) {
    return null;
  }
  return state.itemUiState[itemId];
}

function applyRowMode(row) {
  const itemId = row?.dataset.itemId;
  const itemState = getItemState(itemId);
  if (!row || !itemState) {
    return;
  }

  const previewEl = row.querySelector('[data-role="module-preview"]');
  const textarea = row.querySelector("textarea[data-role='module-item-editor']");
  const toggleBtn = row.querySelector('button[data-role="toggle-edit"]');

  if (!previewEl || !textarea || !toggleBtn) {
    return;
  }

  if (itemState.isEditing) {
    previewEl.classList.add("is-hidden");
    textarea.classList.remove("is-hidden");
    textarea.value = itemState.rawText;
    autoResizeTextarea(textarea);
    toggleBtn.textContent = "✓";
    toggleBtn.title = "保存";
    toggleBtn.setAttribute("aria-label", "保存");
  } else {
    previewEl.classList.remove("is-hidden");
    textarea.classList.add("is-hidden");
    renderMarkdownLatex(itemState.rawText, previewEl);
    toggleBtn.textContent = "✎";
    toggleBtn.title = "编辑";
    toggleBtn.setAttribute("aria-label", "编辑");
  }
}

function enterEditMode(row) {
  const itemId = row?.dataset.itemId;
  const itemState = getItemState(itemId);
  if (!row || !itemState) {
    return;
  }
  itemState.isEditing = true;
  applyRowMode(row);
  const textarea = row.querySelector("textarea[data-role='module-item-editor']");
  if (textarea) {
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
  }
}

function saveRowEdit(row) {
  const itemId = row?.dataset.itemId;
  const itemState = getItemState(itemId);
  if (!row || !itemState) {
    return;
  }
  const textarea = row.querySelector("textarea[data-role='module-item-editor']");
  itemState.rawText = textarea ? textarea.value : itemState.rawText;
  itemState.isEditing = false;
  applyRowMode(row);
}

function bindModuleEditorListeners() {
  if (!state.moduleEditorListenersBound) {
    modulesContainerEl.addEventListener("input", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) {
        return;
      }
      if (target instanceof HTMLTextAreaElement && target.dataset.role === "module-item-editor") {
        const row = target.closest(".item-row[data-item-id]");
        const itemId = row?.dataset.itemId;
        const itemState = getItemState(itemId);
        if (itemState) {
          itemState.rawText = target.value;
        }
        autoResizeTextarea(target);
      }
      updateSaveStatus(isDirty() ? "未保存" : "已保存");
    });

    modulesContainerEl.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) {
        return;
      }
      const button = target.closest("button[data-role]");
      if (!button) {
        return;
      }

      if (button.dataset.role === "add-item") {
        const card = button.closest(".module-card[data-module]");
        if (!card) {
          return;
        }
        const moduleItemsEl = card.querySelector(".module-items");
        if (!moduleItemsEl) {
          return;
        }
        moduleItemsEl.appendChild(
          createModuleItemRow("", moduleItemsEl.querySelectorAll(".item-row").length, true, { startEditing: true })
        );
        renumberModuleItems(card);
        updateSaveStatus(isDirty() ? "未保存" : "已保存");
        return;
      }

      if (button.dataset.role === "toggle-edit") {
        const row = button.closest(".item-row[data-item-id]");
        if (!row) {
          return;
        }
        const itemState = getItemState(row.dataset.itemId);
        if (!itemState) {
          return;
        }
        if (itemState.isEditing) {
          saveRowEdit(row);
        } else {
          enterEditMode(row);
        }
        updateSaveStatus(isDirty() ? "未保存" : "已保存");
        return;
      }

      if (button.dataset.role === "delete-item") {
        const row = button.closest(".item-row");
        const card = button.closest(".module-card[data-module]");
        if (!row || !card) {
          return;
        }
        const itemId = row.dataset.itemId;
        if (itemId) {
          delete state.itemUiState[itemId];
        }
        row.remove();
        renumberModuleItems(card);
        updateSaveStatus(isDirty() ? "未保存" : "已保存");
      }
    });

    modulesContainerEl.addEventListener("focusout", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLTextAreaElement) || target.dataset.role !== "module-item-editor") {
        return;
      }
      const row = target.closest(".item-row[data-item-id]");
      if (!row) {
        return;
      }
      if (row.contains(event.relatedTarget)) {
        return;
      }
      const itemState = getItemState(row.dataset.itemId);
      if (!itemState || !itemState.isEditing) {
        return;
      }
      saveRowEdit(row);
      updateSaveStatus(isDirty() ? "未保存" : "已保存");
    });

    state.moduleEditorListenersBound = true;
  }

  modulesContainerEl.querySelectorAll(".item-row[data-item-id]").forEach((row) => {
    applyRowMode(row);
  });
}

function moduleSupportsItemMutation(moduleName) {
  return ITEM_MUTABLE_MODULE_KEYWORDS.some((keyword) => moduleName.includes(keyword));
}

function createModuleItemRow(item, index, canDelete = false, options = {}) {
  const { startEditing = false } = options;
  const row = document.createElement("label");
  row.className = `item-row${canDelete ? " item-row-editable" : ""}`;
  const itemId = `item-${state.nextItemId++}`;
  row.dataset.itemId = itemId;
  state.itemUiState[itemId] = {
    id: itemId,
    rawText: String(item || ""),
    isEditing: Boolean(startEditing),
  };

  const order = document.createElement("span");
  order.className = "item-order";
  order.textContent = `${index + 1}.`;

  const content = document.createElement("div");
  content.className = "item-content";

  const preview = document.createElement("div");
  preview.className = "item-preview";
  preview.dataset.role = "module-preview";

  const textarea = document.createElement("textarea");
  textarea.dataset.role = "module-item-editor";
  textarea.rows = 1;
  textarea.value = String(item || "");
  textarea.classList.add("is-hidden");

  content.appendChild(preview);
  content.appendChild(textarea);

  row.appendChild(order);
  row.appendChild(content);

  if (canDelete) {
    const actions = document.createElement("div");
    actions.className = "item-actions";

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "item-edit-btn";
    editBtn.dataset.role = "toggle-edit";
    editBtn.textContent = "✎";
    editBtn.title = "编辑";
    editBtn.setAttribute("aria-label", "编辑");
    actions.appendChild(editBtn);

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "item-delete-btn";
    deleteBtn.dataset.role = "delete-item";
    deleteBtn.textContent = "🗑";
    deleteBtn.title = "删除";
    deleteBtn.setAttribute("aria-label", "删除");
    actions.appendChild(deleteBtn);

    row.appendChild(actions);
  }

  applyRowMode(row);
  return row;
}

function renumberModuleItems(card) {
  card.querySelectorAll(".item-row").forEach((row, index) => {
    const label = row.querySelector("span");
    if (label) {
      label.textContent = `${index + 1}.`;
    }
  });
}

function renderModules(payload) {
  modulesContainerEl.innerHTML = "";
  state.itemUiState = {};
  state.nextItemId = 1;

  const headerCard = document.createElement("article");
  headerCard.className = "module-card summary-card";
  headerCard.innerHTML = `
    <h3>基础信息</h3>
    <label class="field-block">
      <span>姓名/学号</span>
      <input data-role="student-name" type="text" value="${payload.student_name_or_id || ""}" />
    </label>
    <label class="field-block">
      <span>整体情况</span>
      <input data-role="overall" type="text" value="${payload.overall || ""}" />
    </label>
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
    card.dataset.module = moduleName;

    const items = Array.isArray(block.items) && block.items.length ? block.items : [block.raw_text || ""];
    const canMutateItems = moduleSupportsItemMutation(moduleName);

    card.innerHTML = `
      <div class="module-header">
        <h3>${moduleName}</h3>
        ${
          canMutateItems
            ? '<button type="button" class="module-add-btn" data-role="add-item" title="增加" aria-label="增加">+</button>'
            : ""
        }
      </div>
      <div class="module-items"></div>
    `;
    const moduleItemsEl = card.querySelector(".module-items");
    items.forEach((item, index) => {
      moduleItemsEl.appendChild(createModuleItemRow(item, index, canMutateItems));
    });
    renumberModuleItems(card);
    modulesContainerEl.appendChild(card);
  });

  bindModuleEditorListeners();
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

function buildExportSnapshotNode(payload) {
  const container = document.createElement("div");
  container.className = "export-snapshot";

  const title = document.createElement("h2");
  title.textContent = `批注导出 · ${payload.student_name_or_id || state.currentStudentId || ""}`;
  container.appendChild(title);

  const overall = document.createElement("p");
  overall.className = "export-overall";
  overall.textContent = `整体情况：${payload.overall || ""}`;
  container.appendChild(overall);

  Object.entries(payload.modules || {}).forEach(([moduleName, block]) => {
    const card = document.createElement("section");
    card.className = "export-module-card";

    const header = document.createElement("h3");
    header.textContent = moduleName;
    card.appendChild(header);

    const items = Array.isArray(block?.items) && block.items.length ? block.items : [block?.raw_text || ""];
    items
      .map((item) => String(item || "").trim())
      .filter(Boolean)
      .forEach((item, index) => {
        const itemWrap = document.createElement("div");
        itemWrap.className = "export-item-row";

        const idx = document.createElement("span");
        idx.className = "export-item-order";
        idx.textContent = `${index + 1}.`;
        itemWrap.appendChild(idx);

        const preview = document.createElement("div");
        preview.className = "item-preview";
        renderMarkdownLatex(item, preview);
        itemWrap.appendChild(preview);

        card.appendChild(itemWrap);
      });

    container.appendChild(card);
  });

  return container;
}

async function exportAnnotationsAsImage() {
  if (!state.currentStudentId) {
    return;
  }
  if (typeof html2canvas !== "function") {
    window.alert("缺少 html2canvas，无法导出图片。");
    return;
  }

  const payload = getCurrentPayloadFromUI();
  const snapshotNode = buildExportSnapshotNode(payload);
  const host = document.createElement("div");
  host.className = "export-render-host";
  host.appendChild(snapshotNode);
  document.body.appendChild(host);

  updateSaveStatus("导出中...");
  try {
    const canvas = await html2canvas(snapshotNode, {
      backgroundColor: "#fffdfa",
      scale: window.devicePixelRatio > 1 ? 2 : 1.5,
      useCORS: true,
      logging: false,
    });

    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!blob) {
      throw new Error("生成 PNG 失败");
    }

    if (!navigator.clipboard || typeof window.ClipboardItem === "undefined") {
      throw new Error("当前浏览器不支持写入图片到剪贴板");
    }

    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    updateSaveStatus("图片已复制");
    window.setTimeout(() => updateSaveStatus(isDirty() ? "未保存" : "已加载"), 1500);
  } catch (error) {
    updateSaveStatus("导出失败");
    window.alert(error.message || "导出失败");
  } finally {
    host.remove();
  }
}

async function loadStudents() {
  const data = await fetchJson("/api/students");
  state.students = data.students;
  state.filteredStudents = data.students;
  renderStudentList();
  if (data.students.length) {
    await loadStudent(data.students[0].id, true);
  }
}

async function loadStudent(studentId, silent = false) {
  if (!silent && isDirty()) {
    const confirmed = window.confirm("当前批注尚未保存，确定切换学生吗？");
    if (!confirmed) {
      return;
    }
  }

  const data = await fetchJson(`/api/student/${encodeURIComponent(studentId)}`);
  const payload = normalizePayload(data.resultJson);
  state.currentStudentId = data.id;
  state.originalPayload = canonicalizePayload({
    student_name_or_id: payload.student_name_or_id,
    overall: payload.overall,
    modules: payload.modules,
  });

  studentTitleEl.textContent = data.id;
  pageMetaEl.textContent = `${data.images.length} 页图片`;
  renderImages(data.images);
  renderModules(payload);
  renderStudentList();
  updateSaveStatus("已加载");
}

async function saveCurrentStudent() {
  if (!state.currentStudentId || state.isSaving) {
    return;
  }

  state.isSaving = true;
  updateSaveStatus("保存中...");
  try {
    const currentPayload = getCurrentPayloadFromUI();
    await fetchJson(`/api/student/${encodeURIComponent(state.currentStudentId)}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        resultJson: currentPayload,
        renderedText: renderResultText(currentPayload),
      }),
    });

    state.originalPayload = canonicalizePayload(currentPayload);
    const student = state.students.find((item) => item.id === state.currentStudentId);
    if (student) {
      student.hasResult = Object.keys(currentPayload.modules || {}).length > 0;
    }
    renderStudentList();
    updateSaveStatus("已保存");
  } catch (error) {
    updateSaveStatus("保存失败");
    window.alert(error.message);
  } finally {
    state.isSaving = false;
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
saveBtnEl.addEventListener("click", saveCurrentStudent);
prevStudentBtnEl.addEventListener("click", () => moveStudent(-1));
nextStudentBtnEl.addEventListener("click", () => moveStudent(1));
if (exportImageBtnEl) {
  exportImageBtnEl.addEventListener("click", exportAnnotationsAsImage);
}

window.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    saveCurrentStudent();
  }
});

window.addEventListener("beforeunload", (event) => {
  if (!isDirty()) {
    return;
  }
  event.preventDefault();
  event.returnValue = "";
});

initLayoutControls();

loadStudents().catch((error) => {
  studentCountEl.textContent = "加载失败";
  imagesContainerEl.innerHTML = `<div class="empty-state">${error.message}</div>`;
  modulesContainerEl.innerHTML = `<div class="empty-state">${error.message}</div>`;
  updateSaveStatus("加载失败");
});
