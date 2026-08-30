"use strict";

const fs = require("node:fs");
const vm = require("node:vm");

const clientPath = process.argv[2];
if (!clientPath) {
  throw new Error("usage: node content_runtime_error_harness.js BDBM_CLIENT_JS");
}

const runtime = {
  lastError: null,
  sendMessage(message, callback) {
    let response;
    let runtimeError = null;
    if (message.type === "localCommand" && message.command === "health") {
      response = {
        ok: true,
        status: 200,
        data: {
          product: "biomem",
          protocol_version: 1,
          ready: true,
          status: "success",
          transport: "http",
          version: "0.0.2"
        }
      };
    } else if (message.type === "localCommand") {
      runtimeError = { message: "The message port closed before a response was received." };
    }
    queueMicrotask(() => {
      runtime.lastError = runtimeError;
      callback(response);
      runtime.lastError = null;
    });
  }
};

const window = {};
const context = vm.createContext({
  chrome: { runtime },
  console,
  Promise,
  queueMicrotask,
  window
});

vm.runInContext(fs.readFileSync(clientPath, "utf8"), context, {
  filename: clientPath
});

(async () => {
  const client = new window.biomemClient({ commandTimeout: 250 });
  const disconnectEvents = [];
  client.onDisconnect = (status, message) => disconnectEvents.push({ status, message });
  await client.connect();
  const connectedBeforeFailure = client.connected;
  let error = null;
  try {
    await client.status();
  } catch (caught) {
    error = {
      code: caught.code || null,
      message: caught.message || String(caught),
      response: caught.response || null
    };
  }
  process.stdout.write(JSON.stringify({
    connectedBeforeFailure,
    connectedAfterFailure: client.connected,
    disconnectEvents,
    error
  }));
})().catch((error) => {
  process.stdout.write(JSON.stringify({ error: { message: error.message } }));
  process.exitCode = 1;
});
