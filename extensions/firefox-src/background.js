const DEFAULT_CONFIG = {
  bdbmWsUrl: "ws://127.0.0.1:8765",
  bdbmHttpUrl: "http://127.0.0.1:8766/api",
  memoryEnabled: true,
  sites: {
    gemini: true,
    chatgpt: true,
    claude: true,
    perplexity: true
  }
};

const LOCAL_HTTP_CONTRACT = Object.freeze({
  product: "biomem",
  protocolVersion: 1,
  transport: "http"
});

const LOCAL_HTTP_ORIGIN = "http://127.0.0.1:8766";
const LOCAL_HTTP_API_PATH = "/api";

const BASE_SCRIPTS = [
  "content/bdbm-client.js",
  "content/prompt-builder.js",
  "content/common.js"
];

const SITE_DEFS = {
  gemini: {
    id: "bdbm-gemini",
    matches: ["https://gemini.google.com/*"],
    js: [...BASE_SCRIPTS, "content/site-gemini.js"]
  },

  chatgpt: {
    id: "bdbm-chatgpt",
    matches: ["https://chatgpt.com/*", "https://chat.openai.com/*"],
    js: [...BASE_SCRIPTS, "content/site-chatgpt.js"]
  },
  claude: {
    id: "bdbm-claude",
    matches: ["https://claude.ai/*"],
    js: [...BASE_SCRIPTS, "content/site-claude.js"]
  },
  perplexity: {
    id: "bdbm-perplexity",
    matches: ["https://www.perplexity.ai/*", "https://perplexity.ai/*"],
    js: [...BASE_SCRIPTS, "content/site-perplexity.js"]
  }
};

async function hasSitePermission(siteId) {
  const def = SITE_DEFS[siteId];
  if (!def) return false;
  return chrome.permissions.contains({ origins: def.matches });
}

async function updateRegistrations(config) {
  const cfg = config || await getConfig();
  const registered = await chrome.scripting.getRegisteredContentScripts();
  const registeredIds = new Set(registered.map((item) => item.id));
  const toRegister = [];
  const toUnregister = [];

  const siteIds = Object.keys(SITE_DEFS);
  const permissionChecks = await Promise.all(siteIds.map((id) => hasSitePermission(id)));
  const permissions = siteIds.reduce((acc, id, idx) => {
    acc[id] = permissionChecks[idx];
    return acc;
  }, {});

  for (const siteId of siteIds) {
    const def = SITE_DEFS[siteId];
    const enabled = !!(cfg.sites && cfg.sites[siteId]);
    const shouldRegister = enabled && permissions[siteId];
    const isRegistered = registeredIds.has(def.id);
    if (shouldRegister && !isRegistered) {
      toRegister.push(def);
    } else if (!shouldRegister && isRegistered) {
      toUnregister.push(def.id);
    }
  }

  if (toUnregister.length) {
    await chrome.scripting.unregisterContentScripts({ ids: toUnregister });
  }
  if (toRegister.length) {
    await chrome.scripting.registerContentScripts(
      toRegister.map((def) => ({
        id: def.id,
        matches: def.matches,
        js: def.js,
        css: ["content/inject.css"],
        runAt: "document_idle",
        allFrames: true
      }))
    );
  }
}

function normalizeConfig(value) {
  const candidate = value && typeof value === "object" ? value : {};
  const candidateSites = candidate.sites && typeof candidate.sites === "object"
    ? candidate.sites
    : {};
  return {
    bdbmWsUrl: typeof candidate.bdbmWsUrl === "string" && candidate.bdbmWsUrl.trim()
      ? candidate.bdbmWsUrl
      : DEFAULT_CONFIG.bdbmWsUrl,
    bdbmHttpUrl: typeof candidate.bdbmHttpUrl === "string" && candidate.bdbmHttpUrl.trim()
      ? candidate.bdbmHttpUrl
      : DEFAULT_CONFIG.bdbmHttpUrl,
    memoryEnabled: candidate.memoryEnabled !== false,
    sites: { ...DEFAULT_CONFIG.sites, ...candidateSites }
  };
}

async function getConfig() {
  const stored = await chrome.storage.local.get("config");
  const current = stored.config || {};
  const normalized = normalizeConfig(current);
  if (JSON.stringify(current) !== JSON.stringify(normalized)) {
    await chrome.storage.local.set({ config: normalized });
  }
  return normalized;
}

async function setConfig(patch) {
  const current = await getConfig();
  const change = patch && typeof patch === "object" ? patch : {};
  const next = normalizeConfig({
    ...current,
    ...change,
    sites: { ...current.sites, ...(change.sites || {}) }
  });
  await chrome.storage.local.set({ config: next });
  return next;
}

function localTransportError(code, error, status = 0, data = null) {
  const detail = data && typeof data === "object" ? data : {};
  const payload = {
    ok: false,
    status,
    error: error || code,
    code,
    data: {
      ...detail,
      code: detail.code || code,
      error: detail.error || error || code
    }
  };
  return payload;
}

function normalizeLocalApiUrl(value) {
  const endpoint = value || DEFAULT_CONFIG.bdbmHttpUrl;
  const expectedEndpoint = `${LOCAL_HTTP_ORIGIN}${LOCAL_HTTP_API_PATH}`;
  let parsed;
  try {
    parsed = new URL(endpoint);
  } catch (_) {
    throw new Error("Invalid local biomem HTTP URL");
  }
  if (parsed.origin !== LOCAL_HTTP_ORIGIN || parsed.username || parsed.password) {
    throw new Error(`Local biomem transport must use ${LOCAL_HTTP_ORIGIN}`);
  }
  if (endpoint !== expectedEndpoint || parsed.href !== expectedEndpoint ||
      parsed.pathname !== LOCAL_HTTP_API_PATH || parsed.search || parsed.hash) {
    throw new Error(`Local biomem command endpoint must be ${expectedEndpoint}`);
  }
  return parsed;
}

async function readJsonResponse(response) {
  if (!response || response.type === "opaque") {
    throw new Error("Opaque local transport response rejected");
  }
  const contentType = response.headers && response.headers.get
    ? response.headers.get("content-type")
    : null;
  if (!contentType || !contentType.toLowerCase().includes("application/json")) {
    throw new Error("Local transport did not return JSON");
  }
  const data = await response.json();
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("Local transport returned a non-object JSON response");
  }
  return data;
}

function validHealthPayload(data) {
  return data.status === "success" &&
    data.product === LOCAL_HTTP_CONTRACT.product &&
    data.protocol_version === LOCAL_HTTP_CONTRACT.protocolVersion &&
    typeof data.version === "string" && data.version.trim().length > 0 &&
    data.ready === true &&
    data.transport === LOCAL_HTTP_CONTRACT.transport;
}

async function dispatchLocalCommand(message) {
  const cfg = await getConfig();
  let apiUrl;
  try {
    apiUrl = normalizeLocalApiUrl(cfg.bdbmHttpUrl);
  } catch (err) {
    return localTransportError("INVALID_LOCAL_URL", err.message);
  }

  const requestedTimeout = Number(message && message.timeout);
  const timeout = Number.isFinite(requestedTimeout)
    ? Math.max(250, Math.min(requestedTimeout, 60000))
    : 30000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  try {
    if (message.command === "health") {
      const healthUrl = new URL("/api/health", apiUrl);
      const response = await fetch(healthUrl.toString(), {
        method: "GET",
        headers: { "Accept": "application/json" },
        cache: "no-store",
        signal: controller.signal
      });
      let data;
      try {
        data = await readJsonResponse(response);
      } catch (err) {
        return localTransportError("INVALID_HEALTH_RESPONSE", err.message, response ? response.status : 0);
      }
      if (!response.ok) {
        return localTransportError(
          data.code || "SERVICE_UNAVAILABLE",
          data.error || `Local biomem health check failed with HTTP ${response.status}`,
          response.status,
          data
        );
      }
      if (!validHealthPayload(data)) {
        return localTransportError(
          "INVALID_HEALTH_RESPONSE",
          "Local service did not return the biomem HTTP protocol v1 readiness contract",
          response.status,
          data
        );
      }
      return { ok: true, status: response.status, data };
    }

    const command = message.command;
    if (!command || typeof command !== "object" || Array.isArray(command) ||
        typeof command.command !== "string" || !command.command.trim()) {
      return localTransportError("INVALID_COMMAND", "localCommand requires a command object");
    }

    const response = await fetch(apiUrl.toString(), {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/json"
      },
      body: JSON.stringify(command),
      cache: "no-store",
      signal: controller.signal
    });
    let data;
    try {
      data = await readJsonResponse(response);
    } catch (err) {
      return localTransportError("INVALID_COMMAND_RESPONSE", err.message, response ? response.status : 0);
    }
    if (!response.ok || data.status === "error") {
      return localTransportError(
        data.code || (response.status === 503 ? "SERVICE_UNAVAILABLE" : "HTTP_ERROR"),
        data.error || `Local biomem command failed with HTTP ${response.status}`,
        response.status,
        data
      );
    }
    return { ok: true, status: response.status, data };
  } catch (err) {
    const aborted = err && err.name === "AbortError";
    return localTransportError(
      aborted ? "TIMEOUT" : "SERVICE_UNAVAILABLE",
      aborted ? "Local biomem request timed out" : (err && err.message ? err.message : "Local biomem request failed")
    );
  } finally {
    clearTimeout(timer);
  }
}

chrome.runtime.onInstalled.addListener(async (details) => {
  const cfg = await getConfig();
  await chrome.storage.local.set({ config: cfg });
  await updateRegistrations(cfg);

  // First-install: open the setup wizard so the user is not stranded
  // in chrome://extensions looking for the Options page.
  if (details && details.reason === "install") {
    try {
      chrome.tabs.create({ url: chrome.runtime.getURL("options.html?wizard=1") });
    } catch (_) { /* ignore */ }
  }
});

chrome.runtime.onStartup.addListener(() => {
  updateRegistrations().catch(() => { });
});

chrome.permissions.onAdded.addListener(() => {
  updateRegistrations().catch(() => { });
});

chrome.permissions.onRemoved.addListener(() => {
  updateRegistrations().catch(() => { });
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg && msg.type === "getConfig") {
      const cfg = await getConfig();
      sendResponse({ ok: true, config: cfg });
      return;
    }

    if (msg && msg.type === "setConfig") {
      const next = await setConfig(msg.patch || {});
      await updateRegistrations(next);
      sendResponse({ ok: true, config: next });
      return;
    }

    if (msg && msg.type === "localCommand") {
      sendResponse(await dispatchLocalCommand(msg));
      return;
    }

    if (msg && msg.type === "openOptions") {
      try {
        chrome.runtime.openOptionsPage();
        sendResponse({ ok: true });
      } catch (err) {
        sendResponse({ ok: false, error: err && err.message ? err.message : "openOptions_failed" });
      }
      return;
    }

    if (msg && msg.type === "openWizard") {
      try {
        chrome.tabs.create({ url: chrome.runtime.getURL("options.html?wizard=1") });
        sendResponse({ ok: true });
      } catch (err) {
        sendResponse({ ok: false, error: err && err.message ? err.message : "openWizard_failed" });
      }
      return;
    }

    sendResponse({ ok: false, error: "unknown_message" });
  })().catch((err) => {
    sendResponse({ ok: false, error: err && err.message ? err.message : "unknown_error" });
  });

  return true;
});
