const form = document.querySelector("#download");
const dateRange = document.querySelector("#date-range");
const dateFrom = document.querySelector("#date-from");
const dateTo = document.querySelector("#date-to");
const username = document.querySelector("#username");
const directory = document.querySelector("#directory");
const memo = document.querySelector("#memo");
const scopeHelp = document.querySelector("#scope-help");
const choose = document.querySelector("#choose");
const start = document.querySelector("#start");
const message = document.querySelector("#message");
const accounts = document.querySelector("#accounts");
const runs = document.querySelector("#runs");
const logPanel = document.querySelector("#log-panel");
const logTitle = document.querySelector("#log-title");
const log = document.querySelector("#log");
const previewButton = document.querySelector("#preview-photos");
const previewPanel = document.querySelector("#photo-preview");
const photoGrid = document.querySelector("#photo-grid");
const moreButton = document.querySelector("#more-photos");
const saveSelection = document.querySelector("#save-selection");
const previewMessage = document.querySelector("#preview-message");
let previewId = null;
let previewVersion = 0;
let previewFolder = "";
let previewLoading = false;
const selectedPhotos = new Set();
const statusNames = {
  running: "진행 중", completed: "완료", partial: "부분 완료", failed: "오류", interrupted: "중단",
};
const modeNames = {photos: "사진", videos: "동영상", both: "사진 + 동영상"};
const scopeHelpText = {
  all: "선택한 기간의 게시물 전체를 확인합니다. 폴더에 저장된 파일은 다시 받지 않습니다.",
  unseen: "같은 계정과 폴더에서 이미 처리한 게시물을 건너뜁니다.",
  since: "같은 계정과 폴더에서 마지막으로 저장한 게시물의 등록일 이후부터 받습니다.",
};
const dateFormat = new Intl.DateTimeFormat("ko-KR", {dateStyle: "short", timeStyle: "short"});
const timeFormat = new Intl.DateTimeFormat("ko-KR", {timeStyle: "medium"});
let selectedRun = null;
let previousState = "";
let submitting = false;
let busy = false;
let accountSettings = new Map();
let loadedMemoFor = null;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function asDate(value) {
  return new Date(value.replace(" ", "T") + "Z");
}

async function request(path, payload) {
  const options = payload === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Local-App": "1"},
    body: JSON.stringify(payload),
  };
  const response = await fetch(path, options);
  if (response.status === 401) window.location.replace("/login");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "요청을 처리하지 못했습니다.");
  return data;
}

function renderAccounts(rows) {
  accountSettings = new Map(rows.map(account => [account.username, account]));
  document.querySelector("#account-count").textContent = rows.length;
  document.querySelector("#accounts-empty").hidden = rows.length > 0;
  accounts.replaceChildren(...rows.map(account => {
    const item = element("li");
    const button = element("button", undefined, "account-button");
    button.type = "button";
    button.append(element("strong", "@" + account.username));
    if (account.memo) button.append(element("span", account.memo));
    button.addEventListener("click", () => {
      closePreview();
      username.value = account.username;
      memo.value = account.memo;
      loadedMemoFor = account.username;
      directory.value = account.directory;
      form.elements.mode.value = account.mode;
      form.elements.download_scope.value = "unseen";
      updateScope();
      message.textContent = "";
      username.focus();
    });
    item.append(button);
    return item;
  }));
}

username.addEventListener("input", () => {
  if (previewId || previewLoading) closePreview();
  const entered = username.value.trim().toLowerCase();
  const name = entered.match(/^https?:\/\/(?:www\.)?instagram\.com\/([^/?#]+)/)?.[1]
    || entered.replace(/^@/, "");
  if (name === loadedMemoFor) return;
  const saved = accountSettings.get(name);
  if (saved || loadedMemoFor !== null) memo.value = saved?.memo || "";
  loadedMemoFor = saved ? name : null;
});

function renderRuns(rows) {
  document.querySelector("#runs-empty").hidden = rows.length > 0;
  runs.replaceChildren(...rows.map(run => {
    const row = element("article", undefined, "run");
    const details = element("div");
    const heading = element("h3", "@" + run.username + " · ");
    heading.append(element("span", statusNames[run.status], "status-" + run.status));
    const count = run.status === "running" ? "" : " · 새 파일 " + run.added + "개";
    const mode = run.since_last ? "지난 다운로드 이후" : run.incremental ? "미처리 게시물" : "전체 재확인";
    const period = run.date_from ? run.date_from + " ~ " + run.date_to : "전체";
    details.append(
      heading,
      element("p", dateFormat.format(asDate(run.started)) + " · " + modeNames[run.mode] + " · " + mode + count),
      element("p", "게시물 등록일 " + period),
      element("p", run.directory),
    );
    const button = element("button", "로그 보기", "secondary");
    button.type = "button";
    button.setAttribute("aria-label", "@" + run.username + " 작업 " + run.id + " 로그 보기");
    button.addEventListener("click", () => openLog(run.id, run.username));
    row.append(details, button);
    return row;
  }));
}

async function refreshLog() {
  const id = selectedRun;
  if (id === null) return;
  const data = await request("/api/runs/" + id + "/events");
  if (id !== selectedRun) return;
  const text = data.events.map(event => timeFormat.format(asDate(event.time)) + "  " + event.message).join("\n");
  if (text === log.textContent) return;
  const atBottom = log.scrollTop + log.clientHeight + 16 >= log.scrollHeight;
  log.textContent = text;
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function openLog(id, name) {
  selectedRun = id;
  logTitle.textContent = "@" + name + " 실행 로그";
  log.textContent = "";
  logPanel.hidden = false;
  refreshLog().catch(error => { message.textContent = error.message; });
}

function updateStart() {
  start.disabled = submitting || busy || previewLoading;
  previewButton.disabled = submitting || busy || previewLoading || form.elements.mode.value !== "photos";
  moreButton.disabled = submitting || busy || previewLoading;
  saveSelection.disabled = submitting || busy || previewLoading || selectedPhotos.size === 0;
  saveSelection.textContent = selectedPhotos.size
    ? "선택한 게시물 " + selectedPhotos.size + "개 저장" : "선택한 게시물 저장";
  start.firstChild.nodeValue = busy ? "다운로드 진행 중 " : "다운로드 시작 ";
}

async function refresh() {
  const state = await request("/api/state");
  busy = state.runs.some(run => run.status === "running");
  updateStart();
  const serialized = JSON.stringify(state);
  if (serialized !== previousState) {
    renderAccounts(state.accounts);
    renderRuns(state.runs);
    previousState = serialized;
  }
  await refreshLog();
}

choose.addEventListener("click", async () => {
  choose.disabled = true;
  message.textContent = "PC에 열린 폴더 선택 창에서 저장 위치를 선택해 주세요.";
  try {
    const data = await request("/api/folder", {});
    if (data.directory) {
      directory.value = data.directory;
      if (previewFolder && previewFolder !== directory.value.trim()) closePreview();
    }
    message.textContent = "";
  } catch (error) {
    message.textContent = error.message;
  } finally {
    choose.disabled = false;
  }
});

function updatePeriod() {
  const custom = form.elements.period.value === "custom";
  dateRange.hidden = !custom;
  dateFrom.disabled = dateTo.disabled = !custom;
  dateFrom.required = dateTo.required = custom;
  dateTo.min = custom ? dateFrom.value : "";
}

function updateScope() {
  scopeHelp.textContent = scopeHelpText[form.elements.download_scope.value];
}

form.addEventListener("change", event => {
  if (event.target.name === "period" || event.target === dateFrom) updatePeriod();
  if (event.target.name === "download_scope") updateScope();
  if (["period", "download_scope", "mode"].includes(event.target.name) ||
      event.target === dateFrom || event.target === dateTo ||
      (event.target === directory && previewFolder && previewFolder !== directory.value.trim())) {
    if (previewId || previewLoading) closePreview();
  }
  updateStart();
});
updatePeriod();
updateScope();

function requestOptions() {
  const scope = form.elements.download_scope.value;
  return {
    username: username.value, memo: memo.value, directory: directory.value.trim(),
    mode: form.elements.mode.value, incremental: scope !== "all", since_last: scope === "since",
    period: form.elements.period.value, date_from: dateFrom.value, date_to: dateTo.value,
  };
}

function closePreview() {
  previewVersion++;
  previewId = null;
  previewFolder = "";
  selectedPhotos.clear();
  previewPanel.hidden = true;
  photoGrid.replaceChildren();
  updateStart();
}

async function loadPreview(more = false) {
  if (previewLoading || busy || !form.reportValidity()) return;
  if (!more) closePreview();
  const version = previewVersion;
  const options = more ? {preview_id: previewId} : requestOptions();
  if (!more) previewFolder = options.directory;
  previewLoading = true;
  updateStart();
  message.textContent = "사진 미리보기를 불러오는 중입니다.";
  previewMessage.textContent = more ? "더 불러오는 중입니다." : "";
  try {
    const data = await request("/api/photo-preview", options);
    if (version !== previewVersion) return;
    previewId = data.id;
    previewPanel.hidden = false;
    moreButton.hidden = !data.more;
    for (const item of data.photos) {
      const tile = element("label", undefined, "photo-tile");
      const image = element("img");
      image.src = item.image;
      image.loading = "lazy";
      image.alt = "게시물 사진";
      const toggle = element("input");
      toggle.type = "checkbox";
      const label = item.date + (item.carousel ? " · 여러 장" : "");
      toggle.setAttribute("aria-label", label + " 게시물 선택");
      toggle.addEventListener("change", () => {
        if (toggle.checked) selectedPhotos.add(item.shortcode);
        else selectedPhotos.delete(item.shortcode);
        updateStart();
      });
      tile.append(image, toggle, element("span", label));
      photoGrid.append(tile);
    }
    previewMessage.textContent = data.error || (photoGrid.childElementCount ? "" :
      data.more ? "이 구간에는 해당 사진이 없습니다. 더 보기로 계속 확인하세요." : "조건에 맞는 사진이 없습니다.");
    if (!more) previewPanel.scrollIntoView({block: "start"});
    message.textContent = "";
  } catch (error) {
    if (version === previewVersion) {
      if (more) previewMessage.textContent = error.message;
      else message.textContent = error.message;
    }
  } finally {
    previewLoading = false;
    updateStart();
    if (version !== previewVersion) message.textContent = "";
  }
}

async function submitDownload(selection = false) {
  if (busy || submitting) return;
  if (!directory.value.trim()) {
    message.textContent = "저장 폴더를 먼저 선택해 주세요.";
    directory.focus();
    return;
  }
  submitting = true;
  updateStart();
  message.textContent = "";
  const name = username.value;
  const options = requestOptions();
  if (selection) {
    options.preview_id = previewId;
    options.selected = [...selectedPhotos];
  }
  try {
    const data = await request("/api/runs", options);
    closePreview();
    await refresh();
    const run = JSON.parse(previousState).runs.find(row => row.id === data.id);
    openLog(data.id, run?.username || name.replace(/^@/, ""));
  } catch (error) {
    (selection ? previewMessage : message).textContent = error.message;
  } finally {
    submitting = false;
    updateStart();
  }
}

form.addEventListener("submit", event => {
  event.preventDefault();
  submitDownload();
});
previewButton.addEventListener("click", () => loadPreview());
moreButton.addEventListener("click", () => loadPreview(true));
saveSelection.addEventListener("click", () => {
  if (previewId && selectedPhotos.size > 0 && form.reportValidity()) submitDownload(true);
});
document.querySelector("#close-preview").addEventListener("click", closePreview);

document.querySelector("#close-log").addEventListener("click", () => {
  selectedRun = null;
  logPanel.hidden = true;
});

async function poll() {
  try {
    await refresh();
  } catch {
    message.textContent = "로컬 앱에 연결할 수 없습니다. 실행 창을 확인해 주세요.";
  } finally {
    setTimeout(poll, 1800);
  }
}

poll();
