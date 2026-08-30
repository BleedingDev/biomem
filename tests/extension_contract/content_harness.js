"use strict";

const fs = require("node:fs");
const vm = require("node:vm");

const clientPath = process.argv[2];
const scenario = process.argv[3];

if (!clientPath || !scenario) {
  throw new Error("usage: node content_harness.js BDBM_CLIENT_JS SCENARIO");
}

const messages = [];
let directFetchAttempts = 0;
let webSocketAttempts = 0;

function replyFor(message) {
  if (message.type === "localCommand" && message.command === "health") {
    if (scenario === "available" || scenario === "exact_store") {
      return {
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
    }
    return {
      ok: false,
      status: 503,
      error: "server unavailable",
      data: { code: "SERVICE_UNAVAILABLE", error: "server unavailable" }
    };
  }
  if (message.type === "localCommand" && message.command && message.command.command === "status") {
    return { ok: true, status: 200, data: { status: "ok", active_stm: 4 } };
  }
  if (message.type === "localCommand" && message.command && message.command.command === "store") {
    return { ok: true, status: 200, data: { status: "success" } };
  }
  return { ok: false, error: "unexpected_message" };
}

const chrome = {
  runtime: {
    lastError: null,
    sendMessage(message, callback) {
      messages.push(message);
      const reply = replyFor(message);
      if (typeof callback === "function") {
        queueMicrotask(() => callback(reply));
        return undefined;
      }
      return Promise.resolve(reply);
    }
  }
};

class ForbiddenWebSocket {
  static OPEN = 1;

  constructor() {
    webSocketAttempts += 1;
    throw new Error("content scripts must route loopback traffic through the background");
  }
}

async function forbiddenFetch() {
  directFetchAttempts += 1;
  throw new Error("content scripts must route loopback traffic through the background");
}

const window = {};
const context = vm.createContext({
  AbortController,
  AbortSignal,
  Error,
  Promise,
  WebSocket: ForbiddenWebSocket,
  chrome,
  clearTimeout,
  console,
  fetch: forbiddenFetch,
  queueMicrotask,
  setTimeout,
  window
});

vm.runInContext(fs.readFileSync(clientPath, "utf8"), context, {
  filename: clientPath
});

(async () => {
  const client = new window.biomemClient({ commandTimeout: 250 });
  let value = null;
  let error = null;
  try {
    await client.connect();
    if (scenario === "exact_store") {
      value = await client.store(
        "lossy query summary",
        "lossy answer summary",
        "browser-session",
        "BIOMEM_LOSSLESS_4107 means the silver compass points east."
      );
    } else {
      value = await client.status();
    }
  } catch (caught) {
    error = {
      name: caught.name || null,
      code: caught.code || null,
      message: caught.message || String(caught)
    };
  }

  process.stdout.write(JSON.stringify({
    connected: client.connected,
    directFetchAttempts,
    error,
    messages,
    value,
    webSocketAttempts
  }));
})().catch((error) => {
  process.stdout.write(JSON.stringify({ error: { message: error.message } }));
  process.exitCode = 1;
});
