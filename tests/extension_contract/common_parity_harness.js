"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const sourcePath = process.argv[2];
const scenario = process.argv[3];

if (!sourcePath || !scenario) {
  throw new Error("usage: node common_parity_harness.js <common.js> <scenario>");
}

let source = fs.readFileSync(sourcePath, "utf8");
if (scenario.startsWith("public_")) {
  const panelNeedle = "    await createPanel();";
  if (!source.includes(panelNeedle)) {
    throw new Error("panel initialization seam not found");
  }
  source = source.replace(panelNeedle, "    await Promise.resolve();");
}
const exposureNeedle = "  window.biomemInjector = {";
if (!source.includes(exposureNeedle)) {
  throw new Error("injector exposure seam not found");
}
source = source.replace(
  exposureNeedle,
  `  globalThis.__commonParity = {
    containsControlArtifacts,
    finalizeAssistant,
    getCurrentAssistantResponseElements,
    prepareNativeSend,
    surgicalRemovePamTokens,
    extractPromptTimestamp,
    hasStrongSystemLeak,
    scoreStrongLeakText,
    setInputValue,
    attachSendHooks,
    enqueuePendingUser,
    isPendingUserSweepCurrent,
    removePendingUserItem,
    setState(patch) { Object.assign(STATE, patch); },
    getState() { return STATE; }
  };

${exposureNeedle}`,
);

const listeners = {};
let execCommandCalls = 0;
let nextTimerId = 1;
let virtualNow = 0;
const virtualEpoch = Date.now();
const mutationObservers = [];
const scheduledIntervals = new Map();
const scheduledTimeouts = new Map();
const transportCalls = { connect: 0, retrieve: [], store: [] };
const heldStoreResolvers = [];
let holdStoresOpen = false;
let retrieveMemories = [];

class TestEvent {
  constructor(type, init = {}) {
    this.type = type;
    Object.assign(this, init);
    this.defaultPrevented = false;
  }
}

class TestHTMLElement {
  static [Symbol.hasInstance](instance) {
    return !!(instance && instance.__testElement);
  }
}

class TestMutationObserver {
  constructor(callback) {
    this.callback = callback;
    mutationObservers.push(this);
  }

  observe() {}
}

function scheduleTimer(collection, callback, delay, repeating = false) {
  const id = nextTimerId++;
  const interval = Math.max(0, Number(delay) || 0);
  collection.set(id, {
    callback,
    dueAt: virtualNow + interval,
    interval,
    repeating,
  });
  return id;
}

async function settleMicrotasks(rounds = 8) {
  for (let index = 0; index < rounds; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function advanceTime(milliseconds) {
  const target = virtualNow + milliseconds;
  let callbacks = 0;
  while (true) {
    const due = [
      ...Array.from(scheduledTimeouts.entries()).map(([id, timer]) => ["timeout", id, timer]),
      ...Array.from(scheduledIntervals.entries()).map(([id, timer]) => ["interval", id, timer]),
    ]
      .filter(([, , timer]) => timer.dueAt <= target)
      .sort((left, right) => left[2].dueAt - right[2].dueAt || left[1] - right[1]);
    if (!due.length) break;
    if (callbacks++ > 250) throw new Error("virtual timer callback limit exceeded");
    const [kind, id, timer] = due[0];
    virtualNow = timer.dueAt;
    if (kind === "timeout") {
      scheduledTimeouts.delete(id);
    } else if (scheduledIntervals.has(id)) {
      timer.dueAt += Math.max(1, timer.interval);
    }
    const callbackResult = timer.callback();
    if (callbackResult && typeof callbackResult.catch === "function") {
      callbackResult.catch((error) => {
        process.stderr.write(`${error.stack || error}\n`);
        process.exitCode = 1;
      });
    }
    await settleMicrotasks(1);
  }
  virtualNow = target;
  await settleMicrotasks(1);
}

class TestBiomemClient {
  async connect() {
    transportCalls.connect += 1;
    return true;
  }

  async retrieve(...args) {
    transportCalls.retrieve.push(args);
    return { memories: retrieveMemories.slice() };
  }

  async store(...args) {
    transportCalls.store.push(args);
    if (holdStoresOpen) {
      return new Promise((resolve) => heldStoreResolvers.push(resolve));
    }
    return { status: "success" };
  }
}

class TestDate extends Date {
  static now() {
    return virtualEpoch + virtualNow;
  }
}

const document = {
  activeElement: null,
  addEventListener: (type, callback) => {
    listeners[`document:${type}`] = callback;
  },
  body: null,
  createRange: () => ({ selectNodeContents: () => {} }),
  documentElement: {},
  createElement: () => plainElement(),
  execCommand: (_command, _showUi, value) => {
    execCommandCalls += 1;
    if (document.activeElement && document.activeElement.isContentEditable) {
      document.activeElement.textContent = value;
      document.activeElement.innerText = value;
    } else if (document.activeElement) {
      document.activeElement.value = value;
    }
    return true;
  },
  getElementById: () => null,
  queryCommandSupported: () => true,
  querySelector: () => null,
  querySelectorAll: () => [],
  visibilityState: "visible",
};

const context = {
  ClipboardEvent: TestEvent,
  DataTransfer: class {
    setData() {}
  },
  Event: TestEvent,
  Date: TestDate,
  HTMLInputElement: class {},
  HTMLElement: TestHTMLElement,
  HTMLTextAreaElement: class {},
  InputEvent: TestEvent,
  KeyboardEvent: TestEvent,
  MutationObserver: TestMutationObserver,
  Node: { DOCUMENT_POSITION_FOLLOWING: 4 },
  NodeFilter: { SHOW_ELEMENT: 1, SHOW_TEXT: 4 },
  chrome: {
    runtime: {
      getURL: (path) => path,
      sendMessage: (_message, callback) => {
        if (callback) {
          callback({
            ok: true,
            config: {
              bdbmHttpUrl: "http://127.0.0.1:8766",
              bdbmWsUrl: "ws://127.0.0.1:8765",
              memoryEnabled: true,
              sites: { "contract-test": true, chatgpt: true },
            },
          });
        }
      },
    },
    storage: {
      local: {
        get: (_key, callback) => callback({ learnedSelectors: {} }),
        set: (_value, callback) => { if (callback) callback(); },
      },
    },
  },
  clearInterval: (id) => scheduledIntervals.delete(id),
  clearTimeout: (id) => scheduledTimeouts.delete(id),
  console,
  document,
  globalThis: null,
  setInterval: (callback, delay) => scheduleTimer(scheduledIntervals, callback, delay, true),
  setTimeout: (callback, delay) => scheduleTimer(scheduledTimeouts, callback, delay),
  window: {
    addEventListener: (type, callback) => {
      listeners[`window:${type}`] = callback;
    },
    getSelection: () => ({ addRange: () => {}, removeAllRanges: () => {} }),
    innerWidth: 1280,
    location: { host: "example.test", href: "https://example.test/" },
    biomemClient: TestBiomemClient,
  },
};
context.globalThis = context;

vm.createContext(context);
vm.runInContext(source, context, { filename: sourcePath });

function plainElement(properties = {}) {
  return {
    __testElement: true,
    className: "",
    classList: { add: () => {}, contains: () => false, remove: () => {}, toggle: () => {} },
    closest: () => null,
    contains: () => false,
    dispatchEvent: () => true,
    getAttribute: () => "",
    id: "",
    isConnected: true,
    isContentEditable: false,
    parentElement: null,
    appendChild: () => {},
    addEventListener: () => {},
    getBoundingClientRect: () => ({ bottom: 20, height: 20, left: 0, right: 100, top: 0, width: 100 }),
    querySelector: () => null,
    querySelectorAll: () => [],
    setAttribute: () => {},
    style: {},
    tagName: "DIV",
    ...properties,
  };
}

function textBackedElement(text, order) {
  const element = plainElement({
    childElementCount: 0,
    compareDocumentPosition(other) {
      if (!other || other.order === order) return 0;
      return order < other.order ? 4 : 2;
    },
    order,
  });
  const textNode = {
    isConnected: true,
    nodeValue: text,
    parentElement: element,
  };
  Object.defineProperty(element, "innerText", {
    get: () => textNode.nodeValue,
    set: (value) => { textNode.nodeValue = value; },
    configurable: true,
  });
  Object.defineProperty(element, "textContent", {
    get: () => textNode.nodeValue,
    set: (value) => { textNode.nodeValue = value; },
    configurable: true,
  });
  element.ownerDocument = {
    body: document.body,
    createTreeWalker(root) {
      let emitted = false;
      return {
        nextNode() {
          if (emitted || root !== element) return null;
          emitted = true;
          return textNode;
        },
      };
    },
  };
  return element;
}

function userContextScenario() {
  const api = context.__commonParity;
  const initialText = "before <user_context>private memory</user_context> after";
  let textNode;
  const root = plainElement({ tagName: "ARTICLE", textContent: initialText });
  const parent = plainElement({ parentElement: root, tagName: "SPAN" });
  textNode = {
    isConnected: true,
    nodeValue: initialText,
    parentElement: parent,
  };
  Object.defineProperty(root, "textContent", { get: () => textNode.nodeValue });
  Object.defineProperty(parent, "textContent", { get: () => textNode.nodeValue });
  const ownerDocument = {
    body: root,
    createTreeWalker: () => {
      let emitted = false;
      return {
        nextNode: () => {
          if (emitted) return null;
          emitted = true;
          return textNode;
        },
      };
    },
  };
  root.ownerDocument = ownerDocument;
  document.body = root;

  const cleaned = api.surgicalRemovePamTokens(root, null, false);
  return {
    cleaned,
    cleanedText: textNode.nodeValue.replace(/\s+/g, " ").trim(),
    controlDetected: api.containsControlArtifacts(initialText),
    strongLeakDetected: api.hasStrongSystemLeak("<relevant_memories>private</relevant_memories>"),
    timestamp: api.extractPromptTimestamp("<current_time>2026-08-27 12:34</current_time>"),
    leakScore: api.scoreStrongLeakText("<user_context><relevant_memories><response_format>"),
  };
}

function focusGuardScenario() {
  const api = context.__commonParity;
  const loginInput = plainElement({ tagName: "INPUT", value: "person@example.test" });
  const events = [];
  const composer = plainElement({
    isContentEditable: true,
    tagName: "DIV",
    textContent: "original prompt",
    innerText: "original prompt",
    focus: () => {},
    dispatchEvent: (event) => {
      events.push(event.type);
      return true;
    },
  });
  document.activeElement = loginInput;
  api.setState({ adapter: {} });
  const previous = api.setInputValue(composer, "enriched prompt");
  return {
    composerText: composer.textContent,
    execCommandCalls,
    inputEvents: events,
    loginValue: loginInput.value,
    previous,
  };
}

function handledPasteScenario() {
  const api = context.__commonParity;
  const beforeExecCalls = execCommandCalls;
  const events = [];
  const composer = plainElement({
    isContentEditable: true,
    tagName: "DIV",
    textContent: "original prompt",
    innerText: "original prompt",
    focus: () => { document.activeElement = composer; },
    dispatchEvent: (event) => {
      events.push(event.type);
      if (event.type === "paste") {
        event.defaultPrevented = true;
        composer.textContent = "enriched prompt";
        composer.innerText = "enriched prompt";
      }
      return true;
    },
  });
  document.activeElement = composer;
  api.setState({ adapter: {} });
  api.setInputValue(composer, "enriched prompt");
  api.setInputValue(composer, "enriched prompt");
  return {
    execCommandCalls: execCommandCalls - beforeExecCalls,
    events,
    text: composer.textContent,
  };
}

function handledBeforeInputScenario() {
  const api = context.__commonParity;
  const beforeExecCalls = execCommandCalls;
  let beforeInputCalls = 0;
  const composer = plainElement({
    isContentEditable: true,
    tagName: "DIV",
    textContent: "original prompt",
    innerText: "original prompt",
    focus: () => { document.activeElement = composer; },
    dispatchEvent: (event) => {
      if (event.type === "beforeinput") {
        beforeInputCalls += 1;
        event.defaultPrevented = true;
        composer.textContent = "enriched prompt";
        composer.innerText = "enriched prompt";
      }
      return true;
    },
  });
  document.activeElement = composer;
  api.setState({ adapter: {} });
  api.setInputValue(composer, "enriched prompt");
  return {
    beforeInputCalls,
    execCommandCalls: execCommandCalls - beforeExecCalls,
    text: composer.textContent,
  };
}

async function composerGuardScenario() {
  const api = context.__commonParity;
  let retrieveCalls = 0;
  const composer = plainElement({
    addEventListener: () => {},
    isContentEditable: true,
    tagName: "DIV",
  });
  const loginInput = plainElement({
    tagName: "INPUT",
    value: "person@example.test",
  });
  const submitButton = plainElement({
    closest: () => submitButton,
    getAttribute: (name) => name === "type" ? "submit" : "",
    tagName: "BUTTON",
  });
  const adapter = {
    findInput: () => composer,
    isFirstTurn: () => false,
  };
  document.activeElement = loginInput;
  api.setState({
    adapter,
    bypass: false,
    client: {
      retrieve: async () => {
        retrieveCalls += 1;
        return { memories: [] };
      },
    },
    connected: true,
    isSending: false,
    memoryEnabled: true,
    sendGuardUntil: 0,
  });
  api.attachSendHooks(adapter, composer, null);
  const event = {
    target: submitButton,
    preventDefault: () => { event.prevented = true; },
    stopImmediatePropagation: () => {},
    stopPropagation: () => {},
  };
  listeners["window:click"](event);
  await new Promise((resolve) => setImmediate(resolve));
  return {
    prevented: !!event.prevented,
    retrieveCalls,
  };
}

function assistantCleanupScopeScenario() {
  const api = context.__commonParity;
  const orderedElement = (id, order) => plainElement({
    id,
    compareDocumentPosition: (other) => order < other.order ? 4 : 0,
    order,
  });
  const oldUser = orderedElement("old-user", 1);
  const oldAssistant = orderedElement("old-assistant", 2);
  const currentUser = orderedElement("current-user", 3);
  const currentAssistantPart1 = orderedElement("current-assistant-1", 4);
  const currentAssistantPart2 = orderedElement("current-assistant-2", 5);
  const conversationContainer = orderedElement("conversation-container", 0);
  const adapter = {
    getAssistantMessageElements: () => [oldAssistant, currentAssistantPart1, currentAssistantPart2],
    getMessageContainer: () => conversationContainer,
    getUserMessageElements: () => [oldUser, currentUser],
  };

  return {
    targetIds: api.getCurrentAssistantResponseElements(adapter, currentAssistantPart2).map((element) => element.id),
  };
}

async function cachedSendRefireScenario(refireResult = true) {
  const api = context.__commonParity;
  const calls = [];
  let refireExpectedPrompt = null;
  let refireArgumentCount = 0;
  const input = plainElement({ tagName: "TEXTAREA", value: "visible prompt" });
  const event = {
    preventDefault() { calls.push("prevent"); },
    stopImmediatePropagation() { calls.push("stop"); },
  };
  const adapter = {
    findSendButton: () => ({ click() { calls.push("button-click"); } }),
    getAssistantMessageElements: () => [],
    getUserMessageElements: () => [],
    isFirstTurn: () => true,
    async refireAfterSend(...args) {
      calls.push("refire");
      refireArgumentCount = args.length;
      refireExpectedPrompt = args[3] || null;
      return refireResult;
    },
    clearInputAfterSend(target) { calls.push("clear"); target.value = ""; },
  };
  context.window.BdbmPromptBuilder = {
    buildEnrichedPrompt: ({ userText }) => ({
      combinedPrompt: `<user_context>cached memory</user_context>\n\n${userText}`,
      systemPrompt: "",
    }),
  };
  api.setState({
    adapter,
    connected: true,
    memoryEnabled: true,
    prefetchAt: Date.now(),
    prefetchMemories: [{ user: "cached", model: "memory" }],
    prefetchSessionId: "cached-session",
    prefetchText: "visible prompt",
  });

  await api.prepareNativeSend(adapter, input, "visible prompt", event);
  return { calls, inputValue: input.value, refireArgumentCount, refireExpectedPrompt };
}

async function candidateRetrievalRerankScenario() {
  const api = context.__commonParity;
  const userText = "Recall the exact two-part identifiers BIOMEM_FOUNDATION and ORBITAL-ANCHOR-731.";
  const exactUser = "BIOMEM_FOUNDATION";
  const exactModel = "ORBITAL-ANCHOR-731";
  const candidates = [
    { user: userText, model: "Unknown.", confidence: 0.99 },
    { user: "BIOMEM FOUNDATION deployment guide", model: "ORBITAL archive index", confidence: 0.98 },
    { user: "BIOMEM FOUNDATION status", model: "ANCHOR 731 diagnostics", confidence: 0.97 },
    { user: "BIOMEM memory foundation", model: "ORBITAL ANCHOR reference", confidence: 0.96 },
    { user: "FOUNDATION identifier", model: "BIOMEM ORBITAL 731", confidence: 0.95 },
    { user: "BIOMEM foundation anchor", model: "ORBITAL identifier history", confidence: 0.94 },
    { user: exactUser, model: exactModel, confidence: 0.93 },
    { user: "irrelevant-08", model: "archive-08", confidence: 0.50 },
    { user: "irrelevant-09", model: "archive-09", confidence: 0.49 },
    { user: "irrelevant-10", model: "archive-10", confidence: 0.48 },
    { user: "irrelevant-11", model: "archive-11", confidence: 0.47 },
    { user: "irrelevant-12", model: "archive-12", confidence: 0.46 },
    { user: "irrelevant-13", model: "archive-13", confidence: 0.45 },
    { user: "irrelevant-14", model: "archive-14", confidence: 0.44 },
    { user: "irrelevant-15", model: "archive-15", confidence: 0.43 },
    { user: "irrelevant-16", model: "archive-16", confidence: 0.42 },
    { user: "irrelevant-17", model: "archive-17", confidence: 0.41 },
    { user: "irrelevant-18", model: "archive-18", confidence: 0.40 },
    { user: "irrelevant-19", model: "archive-19", confidence: 0.39 },
    { user: "irrelevant-20", model: "archive-20", confidence: 0.38 },
  ];
  let requestedLimit = 0;
  let providerPrompt = "";
  const input = plainElement({ tagName: "TEXTAREA", value: userText });
  const adapter = {
    findInput: () => input,
    findSendButton: () => null,
    getAssistantMessageElements: () => [],
    getUserMessageElements: () => [],
    isFirstTurn: () => false,
    async refireAfterSend(_input, _button, _armBypass, expectedPrompt) {
      providerPrompt = expectedPrompt || input.value;
      return true;
    },
    writeInputValue(target, value) {
      const previous = target.value;
      target.value = value;
      return previous;
    },
  };
  const promptBuilderPath = path.join(path.dirname(sourcePath), "prompt-builder.js");
  vm.runInContext(fs.readFileSync(promptBuilderPath, "utf8"), context, {
    filename: promptBuilderPath,
  });
  document.body = plainElement();
  document.activeElement = input;
  api.setState({
    adapter,
    connected: true,
    isSending: false,
    memoryEnabled: true,
    pendingStore: null,
    pendingUserGeneration: 0,
    pendingUserQueue: [],
    prefetchAt: 0,
    prefetchInFlight: false,
    prefetchMemories: [],
    prefetchPromise: null,
    prefetchSessionId: null,
    prefetchText: "",
    client: {
      async retrieve(_text, _sessionId, limit) {
        requestedLimit = limit;
        return { memories: candidates.slice(0, limit) };
      },
    },
  });
  const event = {
    preventDefault() { event.defaultPrevented = true; },
    stopImmediatePropagation() {},
  };
  await api.prepareNativeSend(adapter, input, userText, event);
  return {
    candidateCount: candidates.length,
    exactModel,
    exactRank: candidates.findIndex((memory) => memory.user === exactUser) + 1,
    exactUser,
    providerPrompt,
    requestedLimit,
  };
}

function pendingUserCancellationIdentityScenario() {
  const api = context.__commonParity;
  const adapter = { getUserMessageElements: () => [] };
  api.setState({ pendingUserGeneration: 0, pendingUserQueue: [] });
  const first = api.enqueuePendingUser(adapter, {
    enrichedText: "<user_context>A</user_context>\n\nSend A",
    originalText: "Send A",
    sessionId: "session-a",
  });
  const second = api.enqueuePendingUser(adapter, {
    enrichedText: "<user_context>B</user_context>\n\nSend B",
    originalText: "Send B",
    sessionId: "session-b",
  });
  const beforeCancel = {
    first: api.isPendingUserSweepCurrent(first),
    second: api.isPendingUserSweepCurrent(second),
  };
  api.removePendingUserItem(first, true);
  return {
    afterCancel: {
      first: api.isPendingUserSweepCurrent(first),
      second: api.isPendingUserSweepCurrent(second),
    },
    beforeCancel,
  };
}

async function publicChatgptTemporaryComposerSendScenario(options = {}) {
  const api = context.__commonParity;
  if (typeof options === "boolean") options = { sendAvailable: options };
  const sendAvailable = options.sendAvailable !== false;
  let providerAcknowledges = options.providerAcknowledges !== false;
  const probeLateAssistant = options.probeLateAssistant === true;
  const providerAddsAssistant = options.providerAddsAssistant !== false;
  const providerUsesControlledText = options.providerUsesControlledText === true;
  const synchronizeControlledOnInput = options.synchronizeControlledOnInput === true;
  const retryAfterFailure = options.retryAfterFailure === true;
  const waitForRefireTimeout = options.waitForRefireTimeout === true;
  const userText = "Which constellation was saved in local memory?";
  const retryUserText = "Which nebula belongs to the second local turn?";
  const assistantText = "The saved constellation is Lyra.";
  const enrich = (text) => `<user_context>Local memory: constellation = Lyra</user_context>\n\n${text}`;
  const expectedEnriched = enrich(userText);
  retrieveMemories = [{ user: "constellation", model: "Lyra" }];
  const users = [];
  const assistants = [];
  const nativeRefires = [];
  const providerConsumedPrompts = [];
  let formRequestSubmitCalls = 0;
  let liveButtonClicks = 0;
  let composerDeleteCalls = 0;
  let nextDomOrder = 1;
  let providerControlledText = userText;

  const liveForm = {
    requestSubmit() {
      // Temporary Chat exposes this method, but its React send handler is not
      // attached here. Calling it is observably inert.
      formRequestSubmitCalls += 1;
    },
  };
  const composer = plainElement({
    id: "prompt-textarea",
    innerText: userText,
    isContentEditable: true,
    tagName: "DIV",
    textContent: userText,
    closest(selector) { return selector === "form" ? liveForm : null; },
    dispatchEvent(event) {
      if (event && event.type === "input" && synchronizeControlledOnInput) {
        providerControlledText = composer.innerText || composer.textContent || "";
      }
      return true;
    },
    focus() { document.activeElement = composer; },
  });
  const liveButton = plainElement({
    disabled: !sendAvailable,
    form: liveForm,
    tagName: "BUTTON",
    click() {
      liveButtonClicks += 1;
      const domPrompt = composer.innerText || composer.textContent || "";
      nativeRefires.push(domPrompt);
      if (!providerAcknowledges) return;
      const consumedPrompt = providerUsesControlledText ? providerControlledText : domPrompt;
      providerConsumedPrompts.push(consumedPrompt);
      const user = textBackedElement(consumedPrompt, nextDomOrder++);
      users.push(user);
      composer.innerText = "";
      composer.textContent = "";
      mutationObservers.forEach((observer) => observer.callback([{ addedNodes: [user] }]));
      if (!providerAddsAssistant) return;
      const assistant = textBackedElement(assistantText, nextDomOrder++);
      assistants.push(assistant);
      mutationObservers.forEach((observer) => observer.callback([{ addedNodes: [assistant] }]));
    },
    closest(selector) {
      if (selector === "form") return liveForm;
      return selector.includes("button") ? liveButton : null;
    },
    getAttribute(name) {
      if (name === "aria-disabled") return sendAvailable ? "false" : "true";
      if (name === "aria-label") return "Send prompt";
      if (name === "type") return "submit";
      return "";
    },
  });

  document.body = plainElement();
  document.activeElement = composer;
  document.querySelector = (selector) => {
    if (selector === "main" || selector === "body" || selector === "div[role='main']") {
      return document.body;
    }
    if (selector.includes("prompt-textarea") || selector.includes("ProseMirror") ||
        selector === "div[contenteditable='true']" || selector === "textarea") {
      return composer;
    }
    if (selector.includes("send-button") || selector.includes("Send prompt") ||
        selector.includes("Send message") || selector === "button[type='submit']") {
      return liveButton;
    }
    return null;
  };
  document.querySelectorAll = (selector) => {
    if (selector.includes("message-author-role='user'") || selector.includes("user-message")) {
      return users.slice();
    }
    if (selector.includes("message-author-role='assistant'") || selector.includes("assistant-message")) {
      return assistants.slice();
    }
    return [];
  };
  document.execCommand = (command, _showUi, value) => {
    execCommandCalls += 1;
    if (command === "insertText") {
      composer.innerText = value;
      composer.textContent = value;
    } else if (command === "delete") {
      composerDeleteCalls += 1;
      composer.innerText = "";
      composer.textContent = "";
    }
    return true;
  };
  context.window.location = { host: "chatgpt.com", href: "https://chatgpt.com/?temporary-chat=true" };
  context.window.BdbmPromptBuilder = {
    buildEnrichedPrompt: ({ userText: currentUserText }) => ({ combinedPrompt: enrich(currentUserText), systemPrompt: "" }),
    containsControlArtifacts: (text) => /<user_context/i.test(text || ""),
    parsePamTokens: (text) => ({
      displayText: text,
      hadArtifacts: false,
      hasTokens: false,
      modelSummary: "",
      threadTitle: "",
      userSummary: "",
    }),
  };

  const providerPath = path.join(path.dirname(sourcePath), "site-chatgpt.js");
  vm.runInContext(fs.readFileSync(providerPath, "utf8"), context, { filename: providerPath });
  await settleMicrotasks();

  const click = listeners["window:click"];
  if (!click) throw new Error("public click listener was not registered");
  const event = {
    composedPath: () => [liveButton],
    preventDefault() { event.defaultPrevented = true; },
    stopImmediatePropagation() {},
    target: liveButton,
  };
  click(event);
  await settleMicrotasks();

  let retryBubbleText = "";
  if (retryAfterFailure) {
    // The failed A transaction must have rolled back before B begins. A's
    // 1800/3200ms mask callbacks are still outstanding in the broken build.
    await advanceTime(1500);
    providerAcknowledges = true;
    composer.innerText = retryUserText;
    composer.textContent = retryUserText;
    const retryEvent = {
      composedPath: () => [liveButton],
      preventDefault() { retryEvent.defaultPrevented = true; },
      stopImmediatePropagation() {},
      target: liveButton,
    };
    click(retryEvent);
    await settleMicrotasks();
    await advanceTime(250);
    // Model ChatGPT remounting B's already-masked authoritative bubble just
    // before A's still-pending 1800ms fallback. A cancelled sweep must not
    // rewrite this newer provider-owned node.
    if (users.length) {
      users[users.length - 1].innerText = retryUserText;
      users[users.length - 1].textContent = retryUserText;
    }
    await advanceTime(100);
    retryBubbleText = users.length ? users[users.length - 1].innerText : "";
    await advanceTime(3300);
  } else {
    await advanceTime(
      waitForRefireTimeout || !providerAcknowledges || !sendAvailable ? 3400 : 125
    );
  }

  if (probeLateAssistant) {
    const lateAssistant = textBackedElement("Late assistant must not bind to a failed send.", nextDomOrder++);
    assistants.push(lateAssistant);
    mutationObservers.forEach((observer) => observer.callback([{ addedNodes: [lateAssistant] }]));
    await settleMicrotasks();
  }
  await advanceTime(1501);

  return {
    assistantText,
    composerDeleteCalls,
    composerText: composer.innerText || composer.textContent || "",
    eventPrevented: !!event.defaultPrevented,
    expectedEnriched,
    formRequestSubmitCalls,
    liveButtonClicks,
    nativeRefires,
    pendingActive: !!api.getState().pendingStore,
    providerConsumedPrompts,
    retrieveCalls: transportCalls.retrieve,
    retryBubbleText,
    retryUserText,
    storeCalls: transportCalls.store,
    userText,
  };
}

async function exactStoreWithoutPamScenario(streaming = false) {
  const api = context.__commonParity;
  const calls = [];
  const userText = "What exact phrase does the marker mean?";
  const assistantText = "the violet sextant points south at midnight.";
  const assistant = plainElement({ innerText: assistantText, textContent: assistantText });
  const adapter = {
    extractMessageText: (element) => element.innerText,
    getAssistantMessageElements: () => [assistant],
    getLastAssistantMessageElement: () => assistant,
    getUserMessageElements: () => [],
    isResponseStreaming: () => streaming,
  };
  context.window.BdbmPromptBuilder = {
    parsePamTokens: (text) => ({
      displayText: text,
      hadArtifacts: false,
      hasTokens: false,
      modelSummary: "",
      threadTitle: "",
      userSummary: "",
    }),
  };
  api.setState({
    client: {
      async store(...args) { calls.push(args); return { status: "success" }; },
    },
    lastTurn: { userText },
    pendingStore: { sessionId: "exact-session", createdAt: Date.now() },
  });

  await api.finalizeAssistant(adapter, assistant);
  return { calls };
}

async function publicAssistantLifecycleFixture({
  holdStoreOpen = false,
  isReactSite = false,
  memories = [],
  requiresAuthoritativeAssistantProvenance = true,
  streaming = false,
  useActualPromptBuilder = false,
  userText = "Visible user prompt",
} = {}) {
  holdStoresOpen = holdStoreOpen;
  retrieveMemories = memories.slice();
  let nextDomOrder = 1;
  let assistants = [];
  const users = [];
  let isStreaming = streaming;
  let assistantLookups = 0;
  let lastSentPayload = "";
  const composer = plainElement({
    innerText: userText,
    isContentEditable: true,
    tagName: "DIV",
    textContent: userText,
    focus() { document.activeElement = composer; },
  });
  const adapter = {
    siteId: "contract-test",
    isReactSite,
    isShadowDom: false,
    requiresAuthoritativeAssistantProvenance,
    extractMessageText: (element) => element ? (element.innerText || element.textContent || "") : "",
    findInput: () => composer,
    findSendButton: () => null,
    getAssistantMessageElements: () => assistants.slice(),
    getLastAssistantMessageElement: () => {
      assistantLookups += 1;
      return assistants.length ? assistants[assistants.length - 1] : null;
    },
    getMessageContainer: () => document.body,
    getUserMessageElements: () => users.slice(),
    getLastUserMessageElement: () => users.length ? users[users.length - 1] : null,
    isFirstTurn: () => false,
    isResponseStreaming: () => isStreaming,
    async refireAfterSend() { return true; },
    writeInputValue(input, value) {
      const previous = input.innerText;
      lastSentPayload = value;
      input.innerText = value;
      input.textContent = value;
      return previous;
    },
  };
  if (useActualPromptBuilder) {
    const promptBuilderPath = path.join(path.dirname(sourcePath), "prompt-builder.js");
    vm.runInContext(fs.readFileSync(promptBuilderPath, "utf8"), context, {
      filename: promptBuilderPath,
    });
  } else {
    context.window.BdbmPromptBuilder = {
      buildEnrichedPrompt: ({ userText: visibleText }) => ({
        combinedPrompt: visibleText,
        systemPrompt: "",
      }),
      parsePamTokens: (text) => ({
        displayText: text,
        hadArtifacts: false,
        hasTokens: false,
        modelSummary: "",
        threadTitle: "",
        userSummary: "",
      }),
    };
  }
  document.body = plainElement();
  document.activeElement = composer;
  context.window.biomemInjector.init(adapter);
  await settleMicrotasks();
  const submit = listeners["document:submit"];
  if (!submit) throw new Error("public submit listener was not registered");

  async function submitTurn(text) {
    composer.innerText = text;
    composer.textContent = text;
    document.activeElement = composer;
    const retrieveCountBefore = transportCalls.retrieve.length;
    const submitEvent = {
      composedPath: () => [composer],
      preventDefault() { submitEvent.defaultPrevented = true; },
      stopImmediatePropagation() {},
      target: composer,
    };
    submit(submitEvent);
    await settleMicrotasks();
    await advanceTime(0);
    if (transportCalls.retrieve.length !== retrieveCountBefore + 1) {
      throw new Error(
        `expected one public retrieve call for turn, got ${transportCalls.retrieve.length - retrieveCountBefore}`
      );
    }
    const userElement = textBackedElement(text, nextDomOrder++);
    users.push(userElement);
    const userMutation = { addedNodes: [userElement] };
    mutationObservers.forEach((observer) => observer.callback([userMutation]));
    await settleMicrotasks();
  }

  await submitTurn(userText);
  return {
    adapter,
    emit(element) {
      const mutation = { addedNodes: element ? [element] : [] };
      mutationObservers.forEach((observer) => observer.callback([mutation]));
    },
    getAssistantLookups: () => assistantLookups,
    getLastSentPayload: () => lastSentPayload,
    getRetrieveCalls: () => transportCalls.retrieve,
    getStoreCalls: () => transportCalls.store,
    releaseStores() {
      holdStoresOpen = false;
      heldStoreResolvers.splice(0).forEach((resolve) => resolve({ status: "success" }));
    },
    submitTurn,
    setAssistant(text) {
      assistants = text == null ? [] : [textBackedElement(text, nextDomOrder++)];
    },
    appendAssistant(text) {
      const assistant = textBackedElement(text, nextDomOrder++);
      assistants.push(assistant);
      return assistant;
    },
    remountLastAssistant(text) {
      if (!assistants.length) return null;
      const prior = assistants[assistants.length - 1];
      const replacement = textBackedElement(text, prior.order);
      assistants[assistants.length - 1] = replacement;
      return replacement;
    },
    updateLastAssistantText(text) {
      const assistant = assistants.length ? assistants[assistants.length - 1] : null;
      if (assistant) {
        assistant.innerText = text;
        assistant.textContent = text;
      }
      return assistant;
    },
    setStreaming(value) { isStreaming = value; },
  };
}

async function delayedAssistantSettlementScenario() {
  const fixture = await publicAssistantLifecycleFixture({ streaming: true });
  fixture.emit(plainElement({
    innerText: "Visible user prompt",
    textContent: "Visible user prompt",
  }));
  await advanceTime(300);
  fixture.setAssistant("A stable answer that appeared after streaming stopped.");
  fixture.setStreaming(false);
  await advanceTime(1499);
  const storesBeforeDebounce = fixture.getStoreCalls().length;
  await advanceTime(4201);
  const lookupsAtSettlement = fixture.getAssistantLookups();
  await advanceTime(10000);
  return {
    calls: fixture.getStoreCalls(),
    lookupsAfterHorizon: fixture.getAssistantLookups(),
    lookupsAtSettlement,
    storesBeforeDebounce,
  };
}

async function rejectedAssistantScenario(
  assistantText,
  userText = "Visible user prompt",
  advanceThroughFallbackHorizon = false,
) {
  const fixture = await publicAssistantLifecycleFixture({ userText });
  fixture.setAssistant(assistantText);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  const callsAfterInitialDebounce = fixture.getStoreCalls().length;
  if (advanceThroughFallbackHorizon) {
    await advanceTime(46000);
  }
  return {
    calls: fixture.getStoreCalls(),
    callsAfterFallbackHorizon: fixture.getStoreCalls().length,
    callsAfterInitialDebounce,
    retrieveCalls: fixture.getRetrieveCalls(),
  };
}

async function exactOnceAfterResolvedStoreScenario() {
  const fixture = await publicAssistantLifecycleFixture();
  fixture.setAssistant("A stable answer.");
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  const callsAfterResolvedStore = fixture.getStoreCalls().length;
  fixture.setAssistant("A stable answer. Re-rendered without semantic change.");
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(6000);
  return { calls: fixture.getStoreCalls(), callsAfterResolvedStore };
}

async function exactOnceWhileStoreInFlightScenario() {
  const fixture = await publicAssistantLifecycleFixture({ holdStoreOpen: true });
  fixture.setAssistant("A stable answer.");
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  const callsWhileFirstStoreIsPending = fixture.getStoreCalls().length;

  fixture.setAssistant("A stable answer with a harmless re-render.");
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  const callsBeforeRelease = fixture.getStoreCalls().length;
  fixture.releaseStores();
  await settleMicrotasks();
  return {
    calls: fixture.getStoreCalls(),
    callsBeforeRelease,
    callsWhileFirstStoreIsPending,
  };
}

async function providerShellProvenanceScenario() {
  const fixture = await publicAssistantLifecycleFixture();
  const providerShell = plainElement({
    innerText: "Search conversations\nSettings\nKeyboard shortcuts",
    textContent: "Search conversations\nSettings\nKeyboard shortcuts",
  });
  fixture.emit(providerShell);
  await advanceTime(1500);
  const callsAfterProviderShell = fixture.getStoreCalls().length;

  fixture.setAssistant("The authoritative assistant answer.");
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  return { calls: fixture.getStoreCalls(), callsAfterProviderShell };
}

async function twoSequentialTurnsScenario() {
  const firstUser = "First distinct user prompt";
  const firstAssistant = "First distinct assistant answer.";
  const secondUser = "Second distinct user prompt";
  const secondAssistant = "Second distinct assistant answer.";
  const fixture = await publicAssistantLifecycleFixture({ userText: firstUser });

  fixture.setAssistant(firstAssistant);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  const callsAfterFirstTurn = fixture.getStoreCalls().length;

  await fixture.submitTurn(secondUser);
  fixture.setAssistant(secondAssistant);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  return {
    calls: fixture.getStoreCalls(),
    callsAfterFirstTurn,
    expected: { firstAssistant, firstUser, secondAssistant, secondUser },
  };
}

async function staleAssistantAcrossTurnsScenario() {
  const firstUser = "First stale-overlap user prompt";
  const firstAssistant = "Prior assistant answer that remains mounted.";
  const secondUser = "Second stale-overlap user prompt";
  const secondAssistant = "New authoritative answer for the second turn.";
  const fixture = await publicAssistantLifecycleFixture({ userText: firstUser });

  fixture.setAssistant(firstAssistant);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  const firstCall = fixture.getStoreCalls()[0];

  await fixture.submitTurn(secondUser);
  fixture.emit(plainElement({
    innerText: "Navigation shell changed while waiting for the next answer.",
    textContent: "Navigation shell changed while waiting for the next answer.",
  }));
  await advanceTime(2000);
  const callsWhilePriorAssistantRemains = fixture.getStoreCalls().length;

  fixture.setAssistant(secondAssistant);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  return {
    calls: fixture.getStoreCalls(),
    callsWhilePriorAssistantRemains,
    expected: { firstAssistant, firstUser, secondAssistant, secondUser },
    firstSession: firstCall ? firstCall[2] : null,
  };
}

async function identicalAssistantTextAcrossTurnsScenario() {
  const answer = "The same legitimate answer can occur in two different turns.";
  const firstUser = "First question with the shared answer";
  const secondUser = "Second question with the shared answer";
  const fixture = await publicAssistantLifecycleFixture({ userText: firstUser });

  fixture.setAssistant(answer);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  const callsAfterFirstTurn = fixture.getStoreCalls().length;

  await fixture.submitTurn(secondUser);
  fixture.appendAssistant(answer);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  return {
    answer,
    calls: fixture.getStoreCalls(),
    callsAfterFirstTurn,
    firstUser,
    secondUser,
  };
}

async function genericProviderMutationFallbackScenario() {
  const answer = "Generic provider assistant answer delivered by a mutation.";
  const fixture = await publicAssistantLifecycleFixture({
    requiresAuthoritativeAssistantProvenance: false,
  });
  const mutationAssistant = plainElement({ innerText: answer, textContent: answer });
  fixture.emit(mutationAssistant);
  await advanceTime(1500);
  return { answer, calls: fixture.getStoreCalls() };
}

async function reactRemountBaselineScenario() {
  const firstUser = "First remount-baseline user prompt";
  const priorAssistant = "Prior mounted answer with stable text.";
  const secondUser = "Second remount-baseline user prompt";
  const secondAssistant = "Genuinely new authoritative assistant turn.";
  const fixture = await publicAssistantLifecycleFixture({
    isReactSite: true,
    userText: firstUser,
  });

  fixture.setAssistant(priorAssistant);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  const firstSession = fixture.getStoreCalls()[0][2];

  await fixture.submitTurn(secondUser);
  const remounted = fixture.remountLastAssistant(priorAssistant);
  fixture.emit(remounted);
  fixture.emit(plainElement({
    innerText: "Unrelated React shell mutation",
    textContent: "Unrelated React shell mutation",
  }));
  await advanceTime(2000);
  const callsWhileOnlyRemountExists = fixture.getStoreCalls().length;

  fixture.appendAssistant(secondAssistant);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  return {
    calls: fixture.getStoreCalls(),
    callsWhileOnlyRemountExists,
    expected: { firstUser, priorAssistant, secondAssistant, secondUser },
    firstSession,
  };
}

function pamResponse(displayText, userSummary, modelSummary, revision = 0) {
  return `${displayText}${" ".repeat(revision)}\n|STPAM|${userSummary}|MIDPAM|${modelSummary}|ENDPAM|`;
}

async function delayedPamCleanupCrossTurnScenario() {
  const firstUser = "First PAM cleanup user prompt";
  const firstDisplay = "First visible PAM answer.";
  const firstModel = "First PAM model summary";
  const secondUser = "Second PAM cleanup user prompt";
  const secondDisplay = "Second visible PAM answer.";
  const secondModel = "Second PAM model summary";
  const fixture = await publicAssistantLifecycleFixture({ userText: firstUser });
  context.window.BdbmPromptBuilder.parsePamTokens = (rawText) => {
    const match = /^(.*?)\s*\|STPAM\|(.*?)\|MIDPAM\|(.*?)\|ENDPAM\|\s*$/s.exec(rawText);
    if (!match) {
      return {
        displayText: rawText,
        hadArtifacts: false,
        hasTokens: false,
        modelSummary: "",
        threadTitle: "",
        userSummary: "",
      };
    }
    return {
      displayText: match[1].trim(),
      hadArtifacts: true,
      hasTokens: true,
      modelSummary: match[3].trim(),
      threadTitle: "",
      userSummary: match[2].trim(),
    };
  };

  fixture.setAssistant(pamResponse(firstDisplay, firstUser, firstModel));
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  const firstCall = fixture.getStoreCalls()[0] || null;
  const firstSession = firstCall ? firstCall[2] : null;

  await fixture.submitTurn(secondUser);
  fixture.setStreaming(true);
  let secondRaw = pamResponse(secondDisplay, secondUser, secondModel, 1);
  fixture.appendAssistant(secondRaw);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());

  const preservationChecks = [];
  for (let revision = 2; revision <= 9; revision += 1) {
    await advanceTime(1000);
    preservationChecks.push(
      fixture.adapter.getLastAssistantMessageElement().innerText === secondRaw
    );
    secondRaw = pamResponse(secondDisplay, secondUser, secondModel, revision);
    fixture.updateLastAssistantText(secondRaw);
    fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  }

  fixture.setStreaming(false);
  await advanceTime(1500);
  const calls = fixture.getStoreCalls();
  const secondCall = calls[1] || null;
  return {
    callCount: calls.length,
    calls,
    firstSession,
    preservationChecks,
    secondSession: secondCall ? secondCall[2] : null,
    secondDomText: fixture.adapter.getLastAssistantMessageElement().innerText,
    expected: { firstDisplay, firstModel, firstUser, secondDisplay, secondModel, secondUser },
  };
}

async function rapidNextTurnBeforeDebounceScenario() {
  const firstUser = "First rapid-turn user prompt";
  const firstAssistant = "Completed first answer awaiting debounce.";
  const secondUser = "Second rapid-turn user prompt";
  const secondAssistant = "Completed second answer after rapid submit.";
  const fixture = await publicAssistantLifecycleFixture({ userText: firstUser });

  fixture.setAssistant(firstAssistant);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1000);
  const callsBeforeSecondSubmit = fixture.getStoreCalls().length;

  await fixture.submitTurn(secondUser);
  fixture.appendAssistant(secondAssistant);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  return {
    calls: fixture.getStoreCalls(),
    callsBeforeSecondSubmit,
    expected: { firstAssistant, firstUser, secondAssistant, secondUser },
  };
}

async function enrichedPromptEchoScenario() {
  const userText = "Explain the private constellation marker.";
  const fixture = await publicAssistantLifecycleFixture({
    memories: [{
      confidence: 0.93,
      model: "The marker points north at dawn.",
      turn_distance: 1,
      user: "Remember the private constellation marker.",
    }],
    useActualPromptBuilder: true,
    userText,
  });
  const sentPayload = fixture.getLastSentPayload();
  fixture.setAssistant(sentPayload);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  const callsAfterInitialDebounce = fixture.getStoreCalls().length;
  await advanceTime(46000);
  return {
    calls: fixture.getStoreCalls(),
    callsAfterFallbackHorizon: fixture.getStoreCalls().length,
    callsAfterInitialDebounce,
    hasEndPam: sentPayload.includes("|ENDPAM|"),
    hasMidPam: sentPayload.includes("|MIDPAM|"),
    hasResponseFormat: sentPayload.includes("<response_format>"),
    hasStPam: sentPayload.includes("|STPAM|"),
    hasUserContext: sentPayload.includes("<user_context>"),
  };
}

async function legitimateCompletePamAnswerScenario() {
  const userText = "Summarize the launch checklist.";
  const displayText = "The launch checklist has three verified steps.";
  const userSummary = "launch checklist request";
  const modelSummary = "three verified launch steps";
  const fixture = await publicAssistantLifecycleFixture({
    useActualPromptBuilder: true,
    userText,
  });
  const rawAnswer = `${displayText}\n|STPAM|${userSummary}|MIDPAM|${modelSummary}|ENDPAM|`;
  fixture.setAssistant(rawAnswer);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1500);
  return {
    calls: fixture.getStoreCalls(),
    expected: { displayText, modelSummary, userSummary, userText },
    retrieveCalls: fixture.getRetrieveCalls(),
  };
}

async function debounceCancellationScenario() {
  const fixture = await publicAssistantLifecycleFixture({ streaming: true });
  fixture.setAssistant("Partial answer");
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1000);
  fixture.setAssistant("Final answer after the stream settled.");
  fixture.setStreaming(false);
  fixture.emit(fixture.adapter.getLastAssistantMessageElement());
  await advanceTime(1499);
  const callsBeforeFinalDebounce = fixture.getStoreCalls().length;
  await advanceTime(1);
  return { calls: fixture.getStoreCalls(), callsBeforeFinalDebounce };
}

(async () => {
  let result;
  if (scenario === "user_context") result = userContextScenario();
  else if (scenario === "focus_guard") result = focusGuardScenario();
  else if (scenario === "handled_paste") result = handledPasteScenario();
  else if (scenario === "handled_beforeinput") result = handledBeforeInputScenario();
  else if (scenario === "composer_guard") result = await composerGuardScenario();
  else if (scenario === "assistant_cleanup_scope") result = assistantCleanupScopeScenario();
  else if (scenario === "cached_send_refire") result = await cachedSendRefireScenario();
  else if (scenario === "candidate_retrieval_rerank") result = await candidateRetrievalRerankScenario();
  else if (scenario === "failed_send_keeps_draft") result = await cachedSendRefireScenario(false);
  else if (scenario === "pending_user_cancellation_identity") result = pendingUserCancellationIdentityScenario();
  else if (scenario === "public_chatgpt_temporary_composer_send") result = await publicChatgptTemporaryComposerSendScenario();
  else if (scenario === "public_chatgpt_stale_controlled_state") result = await publicChatgptTemporaryComposerSendScenario({ providerAddsAssistant: false, providerUsesControlledText: true, waitForRefireTimeout: true });
  else if (scenario === "public_chatgpt_input_event_sync") result = await publicChatgptTemporaryComposerSendScenario({ providerUsesControlledText: true, synchronizeControlledOnInput: true });
  else if (scenario === "public_chatgpt_unavailable_composer_send") result = await publicChatgptTemporaryComposerSendScenario({ sendAvailable: false });
  else if (scenario === "public_chatgpt_inert_enabled_composer_send") result = await publicChatgptTemporaryComposerSendScenario({ providerAcknowledges: false, probeLateAssistant: true });
  else if (scenario === "public_chatgpt_failed_send_then_retry") result = await publicChatgptTemporaryComposerSendScenario({ providerAcknowledges: false, retryAfterFailure: true });
  else if (scenario === "exact_store_without_pam") result = await exactStoreWithoutPamScenario();
  else if (scenario === "streaming_without_pam_defers_store") result = await exactStoreWithoutPamScenario(true);
  else if (scenario === "public_delayed_assistant_settlement") result = await delayedAssistantSettlementScenario();
  else if (scenario === "public_debounce_cancellation") result = await debounceCancellationScenario();
  else if (scenario === "public_reject_connection_error") result = await rejectedAssistantScenario("Unable to connect\nRetry");
  else if (scenario === "public_reject_security_verification") result = await rejectedAssistantScenario("Performing security verification\nChecking your browser before accessing ChatGPT");
  else if (scenario === "public_reject_captcha") result = await rejectedAssistantScenario("Verify you are human\nCloudflare\nPrivacy • Help");
  else if (scenario === "public_reject_login_ui") result = await rejectedAssistantScenario("Log in\nSign up\nContinue with Google");
  else if (scenario === "public_reject_user_prompt_echo") result = await rejectedAssistantScenario("Visible user prompt");
  else if (scenario === "public_reject_pam_user_prompt_echo") result = await rejectedAssistantScenario("Literal marker |STPAM| demo |ENDPAM|", "Literal marker |STPAM| demo |ENDPAM|");
  else if (scenario === "public_legitimate_error_words") result = await rejectedAssistantScenario("If your client is unable to connect through Cloudflare, retry with a fresh network session. A Request ID can help support teams correlate the diagnostic.");
  else if (scenario === "public_legitimate_security_words") result = await rejectedAssistantScenario("To log in or sign up, complete the security verification. If the guide says Verify you are human, follow the documented steps.");
  else if (scenario === "public_provider_shell_provenance") result = await providerShellProvenanceScenario();
  else if (scenario === "public_exact_once_while_store_in_flight") result = await exactOnceWhileStoreInFlightScenario();
  else if (scenario === "public_exact_once_after_resolved_store") result = await exactOnceAfterResolvedStoreScenario();
  else if (scenario === "public_two_sequential_turns") result = await twoSequentialTurnsScenario();
  else if (scenario === "public_stale_assistant_across_turns") result = await staleAssistantAcrossTurnsScenario();
  else if (scenario === "public_identical_assistant_text_across_turns") result = await identicalAssistantTextAcrossTurnsScenario();
  else if (scenario === "public_generic_provider_mutation_fallback") result = await genericProviderMutationFallbackScenario();
  else if (scenario === "public_react_remount_baseline") result = await reactRemountBaselineScenario();
  else if (scenario === "public_delayed_pam_cleanup_cross_turn") result = await delayedPamCleanupCrossTurnScenario();
  else if (scenario === "public_rapid_next_turn_before_debounce") result = await rapidNextTurnBeforeDebounceScenario();
  else if (scenario === "public_enriched_prompt_echo") result = await enrichedPromptEchoScenario();
  else if (scenario === "public_legitimate_complete_pam_answer") result = await legitimateCompletePamAnswerScenario();
  else if (scenario === "public_reject_connection_error_with_diagnostics") result = await rejectedAssistantScenario("ChatGPT said:\nUnable to connect\nRetry\nRequest ID: 2f60b98c-7f4a-4c21\nDiagnostic: upstream_timeout", "Visible user prompt", true);
  else if (scenario === "public_reject_exact_unknown") result = await rejectedAssistantScenario("Unknown", "Visible user prompt", true);
  else if (scenario === "public_reject_exact_unknown_period") result = await rejectedAssistantScenario("Unknown.", "Visible user prompt", true);
  else if (scenario === "public_reject_pam_unknown") result = await rejectedAssistantScenario(pamResponse("Unknown", "Visible user prompt", "Unknown"), "Visible user prompt", true);
  else if (scenario === "public_reject_pam_unknown_period") result = await rejectedAssistantScenario(pamResponse("Unknown.", "Visible user prompt", "Unknown."), "Visible user prompt", true);
  else if (scenario === "public_legitimate_unknown_prose") result = await rejectedAssistantScenario("The status label Unknown means the source did not provide a recognized value.");
  else throw new Error(`unknown scenario: ${scenario}`);
  process.stdout.write(JSON.stringify(result));
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
