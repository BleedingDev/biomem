"use strict";

const fs = require("node:fs");
const vm = require("node:vm");

const backgroundPath = process.argv[2];
const configuredUrl = process.argv[3];
const commandKind = process.argv[4];

if (!backgroundPath || !configuredUrl || !["health", "command"].includes(commandKind)) {
  throw new Error("usage: node background_url_harness.js BACKGROUND_JS URL health|command");
}

let messageListener = null;
const fetchCalls = [];

function eventSlot(capture) {
  return {
    addListener(listener) {
      if (capture) capture(listener);
    }
  };
}

async function mockedFetch(url, options = {}) {
  fetchCalls.push({
    url: String(url),
    method: options.method || "GET"
  });
  const data = commandKind === "health"
    ? {
        product: "biomem",
        protocol_version: 1,
        ready: true,
        status: "success",
        transport: "http",
        version: "0.0.2"
      }
    : { status: "ok" };
  return {
    headers: { get: () => "application/json" },
    ok: true,
    status: 200,
    type: "basic",
    async json() { return data; }
  };
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
            bdbmHttpUrl: configuredUrl,
            sites: {}
          }
        };
      },
      async set() {}
    }
  },
  tabs: { create() {} },
  runtime: {
    lastError: null,
    getURL: (path) => `chrome-extension://test/${path}`,
    openOptionsPage() {},
    onInstalled: eventSlot(),
    onStartup: eventSlot(),
    onMessage: eventSlot((listener) => { messageListener = listener; })
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
      messageListener(message, {}, sendResponse);
    } catch (error) {
      sendResponse({ ok: false, error: error.message });
    }
  });
}

(async () => {
  const command = commandKind === "health" ? "health" : { command: "status" };
  const result = await dispatch({ type: "localCommand", command, timeout: 1000 });
  process.stdout.write(JSON.stringify({ result, fetchCalls }));
})().catch((error) => {
  process.stdout.write(JSON.stringify({
    result: { ok: false, error: error.message },
    fetchCalls
  }));
  process.exitCode = 1;
});
