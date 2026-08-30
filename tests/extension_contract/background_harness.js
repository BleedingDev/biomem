"use strict";

const fs = require("node:fs");
const vm = require("node:vm");

const backgroundPath = process.argv[2];
const scenario = process.argv[3];

if (!backgroundPath || !scenario) {
  throw new Error("usage: node background_harness.js BACKGROUND_JS SCENARIO");
}

let messageListener = null;
const fetchCalls = [];
const storageWrites = [];

function eventSlot(capture) {
  return {
    addListener(listener) {
      if (capture) capture(listener);
    }
  };
}

function response({
  ok,
  status,
  type = "basic",
  data = {},
  jsonError = null,
  contentType = "application/json"
}) {
  return {
    headers: {
      get(name) {
        return String(name).toLowerCase() === "content-type" ? contentType : null;
      }
    },
    ok,
    status,
    type,
    async json() {
      if (jsonError) throw new Error(jsonError);
      return data;
    }
  };
}

async function mockedFetch(url, options = {}) {
  fetchCalls.push({
    url: String(url),
    method: options.method || "GET",
    mode: options.mode || null,
    headers: options.headers || null,
    body: options.body || null
  });

  const healthy = {
    product: "biomem",
    protocol_version: 1,
    ready: true,
    status: "success",
    transport: "http",
    version: "0.0.2"
  };
  if (scenario === "health_valid") {
    return response({ ok: true, status: 200, data: healthy });
  }
  if (scenario === "health_invalid_product") {
    return response({
      ok: true,
      status: 200,
      data: { ...healthy, product: "not-biomem" }
    });
  }
  if (scenario === "health_invalid_status") {
    return response({
      ok: true,
      status: 200,
      data: { ...healthy, status: "starting" }
    });
  }
  if (scenario === "health_invalid_protocol") {
    return response({
      ok: true,
      status: 200,
      data: { ...healthy, protocol_version: 2 }
    });
  }
  if (scenario === "health_not_ready") {
    return response({
      ok: true,
      status: 200,
      data: { ...healthy, ready: false }
    });
  }
  if (scenario === "health_wrong_transport") {
    return response({
      ok: true,
      status: 200,
      data: { ...healthy, transport: "ws" }
    });
  }
  if (scenario === "health_missing_version") {
    return response({
      ok: true,
      status: 200,
      data: { ...healthy, version: "" }
    });
  }
  if (scenario === "health_non_json") {
    return response({
      ok: true,
      status: 200,
      contentType: "text/html",
      data: healthy
    });
  }
  if (scenario === "health_opaque") {
    return response({ ok: false, status: 0, type: "opaque", jsonError: "opaque response" });
  }
  if (scenario === "health_503" || scenario === "command_503") {
    return response({
      ok: false,
      status: 503,
      data: { status: "error", code: "SERVICE_UNAVAILABLE", error: "server unavailable" }
    });
  }
  if (scenario === "network_error") {
    throw new TypeError("fetch failed");
  }
  if (scenario === "command_success") {
    return response({
      ok: true,
      status: 200,
      data: { status: "ok", result: { active_stm: 4 } }
    });
  }
  throw new Error(`unknown scenario: ${scenario}`);
}

const chrome = {
  permissions: {
    contains: async () => true,
    onAdded: eventSlot(),
    onRemoved: eventSlot()
  },
  scripting: {
    getRegisteredContentScripts: async () => [],
    registerContentScripts: async () => {},
    unregisterContentScripts: async () => {}
  },
  storage: {
    local: {
      async get() {
        return {
          config: {
            bdbmHttpUrl: "http://127.0.0.1:8766/api",
            bdbmWsUrl: "ws://127.0.0.1:8765",
            memoryEnabled: false,
            unexpectedLegacySetting: "must-not-survive",
            sites: {}
          }
        };
      },
      async set(value) {
        storageWrites.push(value);
      }
    }
  },
  tabs: { create() {} },
  runtime: {
    lastError: null,
    getURL: (path) => `chrome-extension://test/${path}`,
    openOptionsPage() {},
    onInstalled: eventSlot(),
    onStartup: eventSlot(),
    onMessage: eventSlot((listener) => {
      messageListener = listener;
    })
  }
};

const context = vm.createContext({
  AbortController,
  AbortSignal,
  URL,
  chrome,
  clearTimeout,
  console,
  encodeURIComponent,
  fetch: mockedFetch,
  setTimeout
});

vm.runInContext(fs.readFileSync(backgroundPath, "utf8"), context, {
  filename: backgroundPath
});

if (!messageListener) {
  throw new Error("background script did not register a runtime message listener");
}

function dispatch(message) {
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        resolve({ ok: false, error: "no_response" });
      }
    }, 750);

    const sendResponse = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };

    try {
      const result = messageListener(message, {}, sendResponse);
      if (result && typeof result.then === "function") {
        result.then(sendResponse, (error) => sendResponse({ ok: false, error: error.message }));
      }
    } catch (error) {
      sendResponse({ ok: false, error: error.message });
    }
  });
}

(async () => {
  const message = scenario === "config_whitelist"
    ? { type: "getConfig" }
    : scenario.startsWith("command_")
      ? { type: "localCommand", command: { command: "status" }, timeout: 1234 }
      : { type: "localCommand", command: "health", timeout: 1234 };
  const result = await dispatch(message);
  process.stdout.write(JSON.stringify({ result, fetchCalls, storageWrites }));
})().catch((error) => {
  process.stdout.write(JSON.stringify({
    result: { ok: false, error: error.message },
    fetchCalls
  }));
  process.exitCode = 1;
});
