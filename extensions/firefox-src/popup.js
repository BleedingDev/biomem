function qs(id) {
  return document.getElementById(id);
}

function setDot(kind) {
  const dot = qs("popup-dot");
  if (!dot) return;
  dot.classList.remove("ok", "warn", "fail", "pending");
  dot.classList.add(kind);
}

function setStateText(message) {
  const el = qs("popup-state-text");
  if (el) el.textContent = message;
}

function setDetail(message, showDownload = false) {
  const el = qs("popup-detail-text");
  if (el) el.textContent = message || "";
  const link = qs("popup-download");
  if (link) link.hidden = !showDownload;
}

async function probeBdbmServer() {
  try {
    const result = await chrome.runtime.sendMessage({
      type: "localCommand",
      command: "health",
      timeout: 2500
    });
    const data = result && result.data;
    return !!(result && result.ok && result.status === 200 && data &&
      data.status === "success" && data.product === "biomem" &&
      data.protocol_version === 1 && data.ready === true &&
      data.transport === "http" && typeof data.version === "string" &&
      data.version.trim());
  } catch (_) {
    return false;
  }
}

async function refreshStatus() {
  setDot("pending");
  setStateText("Checking…");
  setDetail("");

  if (await probeBdbmServer()) {
    setDot("ok");
    setStateText("Connected locally");
    setDetail("biomem Memory is connected to the local desktop application.");
    return;
  }

  setDot("fail");
  setStateText("biomem software not running");
  setDetail("Start the biomem Memory desktop application, then reopen this popup.", true);
}

document.addEventListener("DOMContentLoaded", () => {
  refreshStatus();

  qs("popup-open-options").addEventListener("click", () => {
    chrome.runtime.openOptionsPage();
    window.close();
  });

  qs("popup-open-wizard").addEventListener("click", () => {
    chrome.runtime.sendMessage({ type: "openWizard" }, () => window.close());
  });
});
