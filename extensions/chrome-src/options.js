function qs(id) {
  return document.getElementById(id);
}

function setValue(id, value) {
  const el = qs(id);
  if (!el) return;
  if (el.type === "checkbox") el.checked = !!value;
  else el.value = value != null ? value : "";
}

function getValue(id) {
  const el = qs(id);
  if (!el) return null;
  if (el.type === "checkbox") return !!el.checked;
  return el.value;
}

const SITE_PERMISSIONS = {
  gemini: ["https://gemini.google.com/*"],
  chatgpt: ["https://chatgpt.com/*", "https://chat.openai.com/*"],
  claude: ["https://claude.ai/*"],
  perplexity: ["https://www.perplexity.ai/*", "https://perplexity.ai/*"]
};

async function hasPermission(origins) {
  return new Promise((resolve) => {
    chrome.permissions.contains({ origins }, (result) => resolve(!!result));
  });
}

async function requestPermission(origins) {
  return new Promise((resolve) => {
    chrome.permissions.request({ origins }, (result) => resolve(!!result));
  });
}

async function refreshPermissionsFor(prefix) {
  for (const [siteId, origins] of Object.entries(SITE_PERMISSIONS)) {
    const granted = await hasPermission(origins);
    const statusEl = qs(`${prefix}-${siteId}-status`);
    const button = qs(`${prefix}-${siteId}`);
    if (statusEl) {
      statusEl.textContent = granted ? "Access granted" : "No access";
      statusEl.classList.toggle("ok", granted);
      statusEl.classList.toggle("warn", !granted);
    }
    if (button) {
      button.textContent = granted ? "Granted" : "Grant access";
      button.disabled = granted;
    }
  }
}

async function loadConfig() {
  const response = await chrome.runtime.sendMessage({ type: "getConfig" });
  return response && response.ok ? response.config : null;
}

async function saveConfig(patch) {
  const response = await chrome.runtime.sendMessage({ type: "setConfig", patch });
  return response && response.ok ? response.config : null;
}

function getUrlParams() {
  return new URLSearchParams(window.location.search);
}

function isWizardMode() {
  return getUrlParams().get("wizard") === "1";
}

function setWizardStep(step) {
  const stepNumber = Math.max(1, Math.min(2, parseInt(step, 10) || 1));
  document.querySelectorAll(".wizard-step").forEach((el) => {
    el.hidden = parseInt(el.dataset.step, 10) !== stepNumber;
  });
  document.querySelectorAll(".stepper .step").forEach((el) => {
    const number = parseInt(el.dataset.step, 10);
    el.classList.toggle("active", number === stepNumber);
    el.classList.toggle("done", number < stepNumber);
  });
  const params = getUrlParams();
  params.set("wizard", "1");
  params.set("step", String(stepNumber));
  history.replaceState(null, "", `${location.pathname}?${params.toString()}`);
}

function setStatusLine(iconEl, textEl, kind, message) {
  if (iconEl) {
    iconEl.classList.remove("ok", "fail", "pending");
    iconEl.classList.add(kind);
    iconEl.textContent = kind === "ok" ? "✓" : kind === "fail" ? "✕" : "…";
  }
  if (textEl) textEl.textContent = message;
}

async function probeBdbmServer() {
  try {
    const result = await chrome.runtime.sendMessage({
      type: "localCommand",
      command: "health",
      timeout: 3000
    });
    const data = result && result.data;
    const valid = !!(result && result.ok && result.status === 200 && data &&
      data.status === "success" && data.product === "biomem" &&
      data.protocol_version === 1 && data.ready === true &&
      data.transport === "http" && typeof data.version === "string" &&
      data.version.trim());
    return { ok: valid, data };
  } catch (_) {
    return { ok: false };
  }
}

async function runConnectivityProbe() {
  const icon = qs("conn-icon");
  const text = qs("conn-text");
  const help = qs("conn-help");
  const nextButton = qs("conn-next");

  setStatusLine(icon, text, "pending", "Checking connection…");
  if (help) help.hidden = true;
  if (nextButton) nextButton.disabled = true;

  const result = await probeBdbmServer();
  if (result.ok) {
    setStatusLine(icon, text, "ok", "biomem memory software detected and running.");
    if (nextButton) nextButton.disabled = false;
  } else {
    setStatusLine(icon, text, "fail", "biomem memory software not running.");
    if (help) help.hidden = false;
    if (nextButton) nextButton.disabled = false;
  }
}

async function finishWizard() {
  await saveConfig({
    sites: {
      gemini: getValue("wiz-site-gemini"),
      chatgpt: getValue("wiz-site-chatgpt"),
      claude: getValue("wiz-site-claude"),
      perplexity: getValue("wiz-site-perplexity")
    }
  });
  location.href = location.pathname;
}

function wirePermissionButtons(prefix, refresh) {
  for (const [siteId, origins] of Object.entries(SITE_PERMISSIONS)) {
    const button = qs(`${prefix}-${siteId}`);
    if (!button) continue;
    button.addEventListener("click", async () => {
      await requestPermission(origins);
      await refresh();
    });
  }
}

async function initWizard() {
  qs("wizard-view").hidden = false;
  qs("options-view").hidden = true;
  qs("page-title").textContent = "biomem plugin — Setup";

  const config = await loadConfig();
  if (config) {
    setValue("wiz-site-gemini", config.sites?.gemini ?? true);
    setValue("wiz-site-chatgpt", config.sites?.chatgpt ?? true);
    setValue("wiz-site-claude", config.sites?.claude ?? true);
    setValue("wiz-site-perplexity", config.sites?.perplexity ?? true);
  }

  setWizardStep(getUrlParams().get("step") || "1");
  await runConnectivityProbe();

  qs("conn-retry").addEventListener("click", runConnectivityProbe);
  qs("conn-next").addEventListener("click", () => setWizardStep(2));
  qs("conn-skip").addEventListener("click", () => setWizardStep(2));
  qs("sites-back").addEventListener("click", () => setWizardStep(1));
  qs("sites-finish").addEventListener("click", finishWizard);

  wirePermissionButtons("wiz-perm", () => refreshPermissionsFor("wiz-perm"));
  await refreshPermissionsFor("wiz-perm");
}

async function initOptions() {
  qs("wizard-view").hidden = true;
  qs("options-view").hidden = false;

  const config = await loadConfig();
  if (!config) return;

  setValue("bdbm-ws-url", config.bdbmWsUrl);
  setValue("bdbm-http-url", config.bdbmHttpUrl);
  setValue("memory-enabled", config.memoryEnabled);
  setValue("site-gemini", config.sites?.gemini);
  setValue("site-chatgpt", config.sites?.chatgpt);
  setValue("site-claude", config.sites?.claude);
  setValue("site-perplexity", config.sites?.perplexity);

  wirePermissionButtons("perm", () => refreshPermissionsFor("perm"));
  await refreshPermissionsFor("perm");

  qs("open-wizard-btn").addEventListener("click", () => {
    location.href = `${location.pathname}?wizard=1&step=1`;
  });
}

async function onSave() {
  const saved = await saveConfig({
    bdbmWsUrl: getValue("bdbm-ws-url"),
    bdbmHttpUrl: getValue("bdbm-http-url"),
    memoryEnabled: getValue("memory-enabled"),
    sites: {
      gemini: getValue("site-gemini"),
      chatgpt: getValue("site-chatgpt"),
      claude: getValue("site-claude"),
      perplexity: getValue("site-perplexity")
    }
  });

  const status = qs("status");
  status.textContent = saved ? "Saved" : "Save failed";
  setTimeout(() => { status.textContent = ""; }, 1500);
}

document.addEventListener("DOMContentLoaded", () => {
  if (isWizardMode()) {
    initWizard();
  } else {
    initOptions();
    qs("save-btn").addEventListener("click", onSave);
  }
});
