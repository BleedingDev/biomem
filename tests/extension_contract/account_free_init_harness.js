"use strict";

const fs = require("fs");
const vm = require("vm");

const sourcePath = process.argv[2];
if (!sourcePath) {
  throw new Error("usage: node account_free_init_harness.js <common.js>");
}

const events = [];
const commands = [];
const config = {
  sites: { test: true },
  memoryEnabled: true,
  bdbmWsUrl: "ws://127.0.0.1:8765",
  bdbmHttpUrl: "http://127.0.0.1:8766",
};

let source = fs.readFileSync(sourcePath, "utf8");

const replacements = new Map([
  ["    await createPanel();", "    events.push(\"createPanel\");"],
  [
    "    STATE.learnedSelectors = await loadLearnedSelectors();",
    "    STATE.learnedSelectors = {};",
  ],
  [
    "    attachSendHooks(wrappedAdapter, input, sendBtn);",
    "    events.push(\"attachSendHooks\");",
  ],
  [
    "    startUserMessageObserver(wrappedAdapter);",
    "    events.push(\"startUserMessageObserver\");",
  ],
  [
    "    startAssistantObserver(wrappedAdapter);",
    "    events.push(\"startAssistantObserver\");",
  ],
  [
    "    startDeepLeakSweep(wrappedAdapter);",
    "    events.push(\"startDeepLeakSweep\");",
  ],
  [
    "    startShadowMutationObservers(wrappedAdapter);",
    "    events.push(\"startShadowMutationObservers\");",
  ],
  [
    "    startHistoryPamSweep(wrappedAdapter);",
    "    events.push(\"startHistoryPamSweep\");",
  ],
]);

for (const [needle, replacement] of replacements) {
  if (!source.includes(needle)) {
    throw new Error(`required initializer seam not found: ${needle.trim()}`);
  }
  source = source.replace(needle, replacement);
}

source = source.replace(
  "  window.biomemInjector = {",
  "  globalThis.__getInjectorState = () => STATE;\n\n  window.biomemInjector = {",
);

class TestClient {
  async connect() {
    events.push("connect");
  }

  async sendCommand(command) {
    commands.push(command);
    return { ok: true };
  }
}

const context = {
  chrome: {
    runtime: {
      getURL: (path) => path,
      sendMessage: (message, callback) => {
        if (message.type === "getConfig") {
          callback({ ok: true, config });
          return;
        }
        callback({ ok: true });
      },
    },
    storage: {
      local: {
        get: (_key, callback) => callback({}),
        set: (_value, callback) => callback(),
      },
    },
  },
  clearInterval: () => {},
  clearTimeout: () => {},
  console,
  document: {
    addEventListener: () => {},
    getElementById: () => null,
    querySelector: () => null,
    visibilityState: "visible",
  },
  events,
  globalThis: null,
  setInterval: () => 1,
  setTimeout: () => 1,
  window: {
    addEventListener: () => {},
    biomemClient: TestClient,
    location: { host: "example.test", href: "https://example.test/" },
  },
};
context.globalThis = context;

vm.createContext(context);
vm.runInContext(source, context, { filename: sourcePath });

const adapter = {
  siteId: "test",
  findInput: () => ({}),
  findSendButton: () => ({}),
  getMessageContainer: () => null,
};

(async () => {
  await context.window.biomemInjector.init(adapter);
  await new Promise((resolve) => setImmediate(resolve));
  const state = context.__getInjectorState();
  process.stdout.write(JSON.stringify({
    authStatePresent: Object.prototype.hasOwnProperty.call(state, "authState"),
    hooksSetUp: state.hooksSetUp,
    events,
    commands,
  }));
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
