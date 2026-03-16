const state = {
  students: [],
  filteredStudents: [],
  currentStudentId: null,
  originalText: "",
  isSaving: false,
};

const studentCountEl = document.getElementById("studentCount");
const studentListEl = document.getElementById("studentList");
const studentSearchEl = document.getElementById("studentSearch");
const studentTitleEl = document.getElementById("studentTitle");
const pageMetaEl = document.getElementById("pageMeta");
const imagesContainerEl = document.getElementById("imagesContainer");
const resultEditorEl = document.getElementById("resultEditor");
const saveStatusEl = document.getElementById("saveStatus");
const saveBtnEl = document.getElementById("saveBtn");
const prevStudentBtnEl = document.getElementById("prevStudentBtn");
const nextStudentBtnEl = document.getElementById("nextStudentBtn");

function isDirty() {
  return resultEditorEl.value !== state.originalText;
}

function updateSaveStatus(message) {
  saveStatusEl.textContent = message;
}

function currentIndex() {
  return state.filteredStudents.findIndex((student) => student.id === state.currentStudentId);
}

function renderStudentList() {
  studentListEl.innerHTML = "";
  const keyword = studentSearchEl.value.trim().toLowerCase();
  state.filteredStudents = state.students.filter((student) => student.id.toLowerCase().includes(keyword));
  studentCountEl.textContent = `共 ${state.filteredStudents.length} 位学生`;

  if (state.filteredStudents.length === 0) {
    studentListEl.innerHTML = `<div class="empty-state">没有匹配的学生。</div>`;
    return;
  }

  for (const student of state.filteredStudents) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `student-item${student.id === state.currentStudentId ? " active" : ""}`;
    button.innerHTML = `
      <h3>${student.id}</h3>
      <p class="student-meta">${student.pageCount} 页 · ${student.hasResult ? "已有结果" : "待写结果"}</p>
    `;
    button.addEventListener("click", () => loadStudent(student.id));
    studentListEl.appendChild(button);
  }
}

function renderImages(images) {
  imagesContainerEl.innerHTML = "";
  if (!images.length) {
    imagesContainerEl.innerHTML = `<div class="empty-state">该学生暂无图片。</div>`;
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

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`请求失败: ${response.status}`);
  }
  return response.json();
}

async function loadStudents() {
  const data = await fetchJson("/api/students");
  state.students = data.students;
  state.filteredStudents = data.students;
  renderStudentList();
  if (data.students.length > 0) {
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
  state.currentStudentId = data.id;
  state.originalText = data.resultText;
  studentTitleEl.textContent = data.id;
  pageMetaEl.textContent = `${data.images.length} 页图片`;
  resultEditorEl.value = data.resultText;
  renderImages(data.images);
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
    await fetchJson(`/api/student/${encodeURIComponent(state.currentStudentId)}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ content: resultEditorEl.value }),
    });
    state.originalText = resultEditorEl.value;
    const student = state.students.find((item) => item.id === state.currentStudentId);
    if (student) {
      student.hasResult = resultEditorEl.value.trim().length > 0;
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

studentSearchEl.addEventListener("input", () => {
  renderStudentList();
});

resultEditorEl.addEventListener("input", () => {
  updateSaveStatus(isDirty() ? "未保存" : "已保存");
});

saveBtnEl.addEventListener("click", saveCurrentStudent);
prevStudentBtnEl.addEventListener("click", () => moveStudent(-1));
nextStudentBtnEl.addEventListener("click", () => moveStudent(1));

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

loadStudents().catch((error) => {
  studentCountEl.textContent = "加载失败";
  imagesContainerEl.innerHTML = `<div class="empty-state">${error.message}</div>`;
  updateSaveStatus("加载失败");
});
