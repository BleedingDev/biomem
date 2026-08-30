(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const daemonPort = Number(params.get("daemon_port"));
  const scenario = params.get("scenario") || "turns";
  const recallTarget = params.get("recall_target") || "first";
  const userCanary = params.get("user_canary") || "fixture-user-canary";
  const answerCanary = params.get("answer_canary") || "fixture-answer-canary";
  const secondUserCanary = params.get("second_user_canary") || "fixture-second-user-canary";
  const secondAnswerCanary = params.get("second_answer_canary") || "fixture-second-answer-canary";
  const invalidCanary = params.get("invalid_canary") || "fixture-invalid-canary";
  const daemonUrl = `http://127.0.0.1:${daemonPort}/api`;
  const commandLog = [];
  const storage = Object.create(null);

  window.__fixture = {
    status: "booting",
    scenario,
    userCanary,
    answerCanary,
    secondUserCanary,
    secondAnswerCanary,
    invalidCanary,
    commandLog,
    submittedPrompts: [],
    providerSubmissions: [],
    providerCompletions: 0,
    error: null,
    result: null,
  };

  function config() {
    return {
      bdbmWsUrl: `ws://127.0.0.1:${daemonPort - 1}`,
      bdbmHttpUrl: daemonUrl,
      memoryEnabled: true,
      sites: { chatgpt: true, gemini: false, claude: false, perplexity: false },
    };
  }

  async function localCommand(message) {
    const command = message.command;
    const isHealth = command === "health";
    const response = await fetch(isHealth ? `${daemonUrl}/health` : daemonUrl, {
      method: isHealth ? "GET" : "POST",
      headers: { "Accept": "application/json", "Content-Type": "application/json" },
      body: isHealth ? undefined : JSON.stringify(command),
      cache: "no-store",
    });
    const data = await response.json();
    commandLog.push({
      command: isHealth ? "health" : command.command,
      request: isHealth ? null : JSON.parse(JSON.stringify(command)),
      ok: response.ok && data.status !== "error",
      status: response.status,
      data,
    });
    if (!response.ok || data.status === "error") {
      return {
        ok: false,
        status: response.status,
        code: data.code || "HTTP_ERROR",
        error: data.error || "Local command failed",
        data,
      };
    }
    return { ok: true, status: response.status, data };
  }

  const runtime = {
    lastError: null,
    getURL(path) { return `/extensions/chrome-src/${path}`; },
    sendMessage(message, callback) {
      let operation;
      if (message && message.type === "getConfig") {
        operation = Promise.resolve({ ok: true, config: config() });
      } else if (message && message.type === "localCommand") {
        operation = localCommand(message);
      } else {
        operation = Promise.resolve({ ok: true });
      }
      operation.then(
        (value) => callback && callback(value),
        (error) => {
          runtime.lastError = { message: error && error.message ? error.message : String(error) };
          try { if (callback) callback(null); } finally { runtime.lastError = null; }
        },
      );
    },
  };

  window.chrome = window.chrome || {};
  window.chrome.runtime = runtime;
  window.chrome.storage = {
    local: {
      get(key, callback) {
        const keys = Array.isArray(key) ? key : [key];
        const result = {};
        keys.forEach((name) => { if (name in storage) result[name] = storage[name]; });
        if (callback) callback(result);
        return Promise.resolve(result);
      },
      set(values, callback) {
        Object.assign(storage, values || {});
        if (callback) callback();
        return Promise.resolve();
      },
    },
  };

  const form = document.getElementById("composer-form");
  const composer = document.getElementById("prompt-textarea");
  const conversation = document.getElementById("conversation");
  let providerMode = scenario === "recall" ? "recall" : "valid";
  let validResponseIndex = 0;

  function appendMessage(role, text, className) {
    const article = document.createElement("article");
    article.dataset.testid = `conversation-turn-${conversation.children.length}`;
    const message = document.createElement("div");
    if (role) message.dataset.messageAuthorRole = role;
    if (className) message.className = className;
    if (role === "user") {
      // ChatGPT exposes the provider-consumed raw prompt through a dedicated
      // message-content node. Keep its textContent byte-for-byte equivalent to
      // the composer payload so the strict provider acknowledgement exercises
      // the production adapter's real public seam.
      const content = document.createElement("div");
      content.dataset.messageContent = "";
      content.textContent = text;
      message.appendChild(content);
    } else {
      message.innerText = text;
    }
    article.appendChild(message);
    conversation.appendChild(article);
    return message;
  }

  function emitValidResponse(responseIndex) {
    const responseUserCanary = responseIndex === 0 ? userCanary : secondUserCanary;
    const responseAnswerCanary = responseIndex === 0 ? answerCanary : secondAnswerCanary;
    const responseText = responseIndex === 0
      ? `Astronomy calibration ${responseAnswerCanary}: align the telescope aperture with a spectrograph reference star.`
      : `Cooking technique ${responseAnswerCanary}: toast the risotto rice, add saffron stock gradually, then emulsify.`;
    const userSummary = responseIndex === 0
      ? `observatory telescope aperture calibration ${responseUserCanary}`
      : `saffron risotto emulsification technique ${responseUserCanary}`;
    const modelSummary = responseIndex === 0
      ? `spectrograph reference-star alignment ${responseAnswerCanary}`
      : `toast rice and gradually emulsify saffron stock ${responseAnswerCanary}`;
    const stop = document.createElement("button");
    stop.dataset.testid = "stop-button";
    stop.setAttribute("aria-label", "Stop generating");
    stop.textContent = "Stop";
    conversation.appendChild(stop);

    setTimeout(() => {
      const assistant = appendMessage("assistant", "Synthetic response is streaming", "assistant-message");
      setTimeout(() => { assistant.innerText = responseText.slice(0, 58); }, 120);
      setTimeout(() => { assistant.innerText += " with repeated mutation"; }, 240);
      setTimeout(() => {
        assistant.innerText = [
          responseText,
          `|STPAM| ${userSummary} |MIDPAM| ${modelSummary} |ENDPAM|`,
        ].join("\n");
        stop.remove();
        window.__fixture.providerCompletions += 1;
      }, 360);
    }, 350);
  }

  function emitInvalidProviderUi() {
    setTimeout(() => {
      const shell = document.createElement("section");
      shell.className = "provider-error-shell";
      shell.setAttribute("role", "alert");
      const markdown = document.createElement("div");
      markdown.className = "markdown";
      markdown.innerText = `Unable to connect\nRetry\n${invalidCanary}`;
      shell.appendChild(markdown);
      conversation.appendChild(shell);
      setTimeout(() => { markdown.innerText += "\nRetry"; }, 120);
    }, 350);
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const submitted = composer.innerText || composer.textContent || "";
    window.__fixture.submittedPrompts.push(submitted);
    const userMessage = appendMessage("user", submitted, "user-message");
    const submission = {
      submitted,
      rawTextImmediately: userMessage.textContent || "",
      authoritativeImmediately: Array.from(document.querySelectorAll(
        "article[data-testid^='conversation-turn-'] [data-message-author-role='user']"
      )).includes(userMessage),
      observations: [],
    };
    window.__fixture.providerSubmissions.push(submission);
    [0, 75, 650].forEach((delayMs) => {
      setTimeout(() => {
        submission.observations.push({
          delayMs,
          connected: userMessage.isConnected,
          rawText: userMessage.textContent || "",
          visibleText: userMessage.innerText || "",
          authoritative: Array.from(document.querySelectorAll(
            "article[data-testid^='conversation-turn-'] [data-message-author-role='user']"
          )).includes(userMessage),
        });
      }, delayMs);
    });
    composer.innerHTML = "<p><br></p>";
    if (providerMode === "valid") {
      emitValidResponse(validResponseIndex);
      validResponseIndex += 1;
    }
    else if (providerMode === "invalid") emitInvalidProviderUi();
  });

  function waitFor(predicate, timeoutMs, label) {
    const started = Date.now();
    return new Promise((resolve, reject) => {
      const check = () => {
        let value;
        try { value = predicate(); } catch (_) { value = null; }
        if (value) { resolve(value); return; }
        if (Date.now() - started >= timeoutMs) {
          reject(new Error(`Timed out waiting for ${label}`));
          return;
        }
        setTimeout(check, 50);
      };
      check();
    });
  }

  function setComposer(text) {
    composer.focus();
    composer.innerText = text;
    composer.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      composed: true,
      data: text,
      inputType: "insertText",
    }));
  }

  function clickSend() {
    document.querySelector("button[data-testid='send-button']").click();
  }

  async function listRecords() {
    const response = await localCommand({
      type: "localCommand",
      command: { command: "list_memories", layer: "both", limit: 100 },
    });
    if (!response.ok) throw new Error(response.error || "list_memories failed");
    return response.data.records || [];
  }

  async function run() {
    await waitFor(() => commandLog.some((entry) => entry.command === "health" && entry.ok), 15000, "extension connection");
    window.__fixture.status = "connected";

    if (scenario === "recall") {
      const recallQuery = recallTarget === "second"
        ? `Recall the saffron risotto emulsification technique ${secondUserCanary}`
        : `Recall the telescope aperture calibration ${userCanary}`;
      setComposer(recallQuery);
      clickSend();
      await waitFor(() => window.__fixture.submittedPrompts.length === 1, 15000, "recall submit");
      // Remain alive beyond the finalize debounce so any late/duplicate store
      // after a retrieve-only new conversation is captured in commandLog.
      await new Promise((resolve) => setTimeout(resolve, 2500));
      const retrieve = [...commandLog].reverse().find((entry) => entry.command === "retrieve" && entry.ok);
      window.__fixture.result = {
        submittedPrompt: window.__fixture.submittedPrompts[0],
        retrievedMemories: retrieve ? (retrieve.data.memories || []) : [],
        commandLog: commandLog.map((entry) => JSON.parse(JSON.stringify(entry))),
        commandNames: commandLog.map((entry) => entry.command),
      };
      window.__fixture.status = "complete";
      return;
    }

    setComposer(`Record observatory telescope aperture calibration ${userCanary}`);
    clickSend();
    await waitFor(() => window.__fixture.providerCompletions === 1, 15000, "first provider completion");
    const storesBeforeRapidSecond = commandLog.filter((entry) => entry.command === "store").length;

    // Submit the second valid turn after the first DOM stream has settled but
    // before its normal 1500ms store debounce fires.
    setComposer(`Save saffron risotto emulsification cooking technique ${secondUserCanary}`);
    clickSend();
    await waitFor(() => window.__fixture.submittedPrompts.length === 2, 15000, "rapid second valid submit");
    await waitFor(() => window.__fixture.providerCompletions === 2, 15000, "second provider completion");
    await waitFor(() => commandLog.filter((entry) => entry.command === "store" && entry.ok).length === 2, 20000, "two valid stores");
    const afterValid = await listRecords();

    // Simulate a provider-side new-conversation navigation before the error.
    // This removes the prior valid assistant so an error-page markdown node
    // cannot be mistaken for (or replaced by) a stale earlier response.
    conversation.replaceChildren();
    providerMode = "invalid";
    setComposer(`Trigger invalid UI ${invalidCanary}`);
    await new Promise((resolve) => setTimeout(resolve, 400));
    clickSend();
    await waitFor(() => window.__fixture.submittedPrompts.length === 3, 15000, "invalid turn submit");
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const afterInvalid = await listRecords();

    window.__fixture.result = {
      afterValid,
      afterInvalid,
      storeSuccesses: commandLog.filter((entry) => entry.command === "store" && entry.ok).length,
      retrieveSuccesses: commandLog.filter((entry) => entry.command === "retrieve" && entry.ok).length,
      storesBeforeRapidSecond,
      submittedPrompts: window.__fixture.submittedPrompts.slice(),
      commandLog: commandLog.map((entry) => JSON.parse(JSON.stringify(entry))),
      commandNames: commandLog.map((entry) => entry.command),
    };
    window.__fixture.status = "complete";
  }

  window.addEventListener("load", () => {
    run().catch((error) => {
      window.__fixture.error = error && error.stack ? error.stack : String(error);
      window.__fixture.status = "failed";
    });
  });
})();
