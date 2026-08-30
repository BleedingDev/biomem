(function () {
  let sendTransactionActive = false;
  const SEND_BUTTON_SELECTORS = [
    "button[data-testid='send-button']",
    "button[aria-label='Send prompt']",
    "button[aria-label='Send message']",
    "button[type='submit']"
  ];
  const COMPOSER_SCOPE_SELECTOR = "[data-testid='composer'], [data-composer], [class*='composer']";

  function findFirst(selectors) {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function findAll(selectors) {
    for (const sel of selectors) {
      const els = Array.from(document.querySelectorAll(sel));
      if (els.length) return els;
    }
    return [];
  }

  function findFirstWithin(root, selectors) {
    if (!root || typeof root.querySelector !== "function") return null;
    for (const sel of selectors) {
      const el = root.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function normalizeComposerText(text) {
    return (text || "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
  }

  function rawUserMessageMatchesExpected(element, expectedText) {
    if (!element || !expectedText) return false;
    const candidates = [element];
    if (typeof element.querySelectorAll === "function") {
      candidates.push(...element.querySelectorAll(
        "[data-bdbm-react-hidden], [data-message-content], [data-testid='collapsible-user-message-content'] span, div[class*='whitespace-pre-wrap']"
      ));
    }
    return candidates.some((candidate) =>
      normalizeComposerText(candidate && candidate.textContent) === expectedText
    );
  }

  // ── Leftover enriched-prompt detector (composer draft guard) ────────
  // ChatGPT now persists the composer content as a draft (restored after
  // send AND after F5/page load). Because we write the enriched prompt
  // into the composer to send it, ChatGPT saves that enriched text as a
  // draft and keeps restoring it into the input field. These helpers
  // detect and remove such leftovers. User-typed drafts never contain
  // control artifacts, so artifact-gated clearing is safe.
  function looksLikeLeftoverEnriched(text) {
    if (!text) return false;
    if (window.BdbmPromptBuilder && window.BdbmPromptBuilder.containsControlArtifacts) {
      if (window.BdbmPromptBuilder.containsControlArtifacts(text)) return true;
    }
    if (/\bSTPAM\b/i.test(text) && /\bENDPAM\b/i.test(text)) return true;
    return /<user_context|<System\s*-/i.test(text) ||
      /\bSystem\s*-\s*(?:User's personal memory|Current Date|associated memory|Additional instruction)/i.test(text) ||
      /Summary of (?:my query|the USER'S QUERY)/i.test(text) ||
      /Summary of (?:your response|YOUR RESPONSE)/i.test(text);
  }

  function getComposerText(el) {
    if (!el) return "";
    return el.isContentEditable || typeof el.value === "undefined"
      ? (el.innerText || el.textContent || "")
      : (el.value || "");
  }

  // Best-effort: ChatGPT keeps composer drafts in origin storage. Remove
  // any localStorage entries that contain our control artifacts so the
  // enriched draft cannot be re-hydrated on the next page load.
  function scrubDraftStorage() {
    try {
      const doomed = [];
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (!key) continue;
        let val = "";
        try { val = localStorage.getItem(key) || ""; } catch (_) { continue; }
        if (/\|STPAM\|/i.test(val) || /<user_context/i.test(val) || /<System\s*-\s*(?:Current Date|associated memory|User's personal memory|Additional instruction)/i.test(val)) {
          doomed.push(key);
        }
      }
      for (const key of doomed) {
        try { localStorage.removeItem(key); } catch (_) { }
      }
    } catch (_) { }
  }

  function clearComposerElement(liveInput) {
    // execCommand path first — ProseMirror processes it like real typing,
    // so ChatGPT's React state (and its draft autosave) update too.
    try { liveInput.focus(); } catch (_) { }
    try {
      const sel = window.getSelection();
      if (sel) {
        sel.removeAllRanges();
        const range = document.createRange();
        range.selectNodeContents(liveInput);
        sel.addRange(range);
      }
      document.execCommand("delete", false);
    } catch (_) { }

    const after = getComposerText(liveInput);
    if (after.trim() && looksLikeLeftoverEnriched(after.trim())) {
      if (liveInput.isContentEditable) {
        // ProseMirror empty-document shape
        try { liveInput.innerHTML = "<p><br></p>"; } catch (_) { liveInput.textContent = ""; }
      } else {
        const proto = liveInput.tagName === "TEXTAREA"
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
        if (setter) setter.call(liveInput, "");
        else liveInput.value = "";
      }
      try {
        liveInput.dispatchEvent(new InputEvent("input", {
          bubbles: true, composed: true, inputType: "deleteContent"
        }));
      } catch (_) {
        liveInput.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      }
    }
  }

  function sweepComposerDraft() {
    if (sendTransactionActive) return;
    const liveInput = adapter.findInput();
    if (!liveInput) return;
    const currentText = getComposerText(liveInput);
    if (!currentText.trim()) return;
    if (!looksLikeLeftoverEnriched(currentText)) return;
    clearComposerElement(liveInput);
    scrubDraftStorage();
  }

  // F5 / page-load guard: ChatGPT restores the persisted draft (containing
  // our enriched prompt) into the composer after load — sometimes with a
  // delay, and again on SPA navigation between conversations. A periodic
  // artifact-gated sweep is safe because refireAfterSend marks the async
  // write→React-click→provider-acknowledgement window as an active transaction.
  // Interval ticks resume only after acknowledgement or a bounded timeout.
  function startComposerDraftGuard() {
    scrubDraftStorage();
    setInterval(sweepComposerDraft, 1000);
  }

  const adapter = {
    siteId: "chatgpt",

    // ── Site classification ──────────────────────────────────────────
    // These flags replace the SHADOW_DOM_SITES / REACT_SITES sets that
    // used to live in common.js, eliminating cross-site branching there.
    isReactSite: true,
    isShadowDom: false,
    requiresAuthoritativeAssistantProvenance: true,

    getReactOverlayTarget(el) {
      if (!el) return el;
      // We want to hide the innermost text container so the bubble background
      // and action buttons remain visible.
      const target = el.querySelector("div[class*='whitespace-pre-wrap'], div[data-message-content]");
      if (target) return target;

      // Fallback: ignore sr-only labels and find the first visible layout child
      const children = Array.from(el.children).filter(c =>
        !c.classList.contains("sr-only") && !c.classList.contains("bdbm-overlay-text")
      );
      if (children.length > 0) return children[0];
      return el;
    },

    findInput() {
      // Prefer ChatGPT's current ProseMirror composer (#prompt-textarea is a
      // contenteditable div). Generic fallbacks kept for older UI variants.
      return findFirst([
        "div#prompt-textarea[contenteditable='true']",
        "#prompt-textarea",
        "div.ProseMirror[contenteditable='true']",
        "div[contenteditable='true']",
        "textarea"
      ]);
    },
    writeInputValue(input, value) {
      const previous = input ? (input.innerText || input.textContent || "") : "";
      if (!input || previous === value) return previous;
      try { input.focus(); } catch (_) { }
      try {
        const selection = window.getSelection();
        if (selection) {
          selection.removeAllRanges();
          const range = document.createRange();
          range.selectNodeContents(input);
          selection.addRange(range);
        }
        document.execCommand("insertText", false, value);
      } catch (_) {
        input.textContent = value;
      }
      if (typeof input.dispatchEvent === "function") {
        try {
          input.dispatchEvent(new InputEvent("input", {
            bubbles: true, composed: true, data: value, inputType: "insertText"
          }));
        } catch (_) {
          if (typeof Event === "function") {
            input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
          }
        }
      }
      return previous;
    },
    findSendButton() {
      const input = this.findInput();
      if (!input || typeof input.closest !== "function") return null;
      const inputForm = input && input.closest ? input.closest("form") : null;
      const composerScope = inputForm || input.closest(COMPOSER_SCOPE_SELECTOR);
      if (!composerScope) return null;
      const candidate = findFirstWithin(composerScope, SEND_BUTTON_SELECTORS) || findFirst(SEND_BUTTON_SELECTORS);
      if (!candidate) return null;
      if (inputForm) {
        const buttonForm = candidate.form || (candidate.closest && candidate.closest("form"));
        return buttonForm === inputForm ? candidate : null;
      }
      const buttonScope = candidate.closest && candidate.closest(COMPOSER_SCOPE_SELECTOR);
      return buttonScope === composerScope ? candidate : null;
    },
    isResponseStreaming() {
      return !!findFirst([
        "button[data-testid='stop-button']",
        "button[aria-label='Stop generating']",
        "button[aria-label='Stop response']"
      ]);
    },
    getMessageContainer() {
      return findFirst([
        "main",
        "div[role='main']",
        "body"
      ]);
    },
    getUserMessageElements() {
      return findAll([
        "article[data-testid^='conversation-turn-'] [data-message-author-role='user']",
        "article [data-message-author-role='user']",
        "div[data-message-author-role='user']",
        "div.user-message"
      ]);
    },
    getAssistantMessageElements() {
      return findAll([
        "article[data-testid^='conversation-turn-'] [data-message-author-role='assistant']",
        "article [data-message-author-role='assistant']",
        "div[data-message-author-role='assistant']",
        "div.assistant-message"
      ]);
    },
    getLastUserMessageElement() {
      const els = this.getUserMessageElements();
      return els.length ? els[els.length - 1] : null;
    },
    getLastAssistantMessageElement() {
      const els = this.getAssistantMessageElements();
      return els.length ? els[els.length - 1] : null;
    },
    extractMessageText(el) {
      return el ? el.innerText : "";
    },
    replaceMessageText(el, text) {
      if (!el) return;
      const target = el.querySelector && el.querySelector(
        "div[class*='whitespace-pre-wrap'], div[data-message-content], p"
      );
      if (target) {
        target.innerText = text;
        return;
      }
      el.innerText = text;
    },

    // F5 / hard-reload guard: ChatGPT's user-message wrapper contains sibling
    // UI controls ("Show more" / "Show less") whose labels appear in innerText
    // AFTER the enriched user text. Reading from the inner content element
    // excludes those labels, preventing the overlay from showing polluted text.
    extractSourceText(el) {
      if (!el || !el.querySelector) return el ? (el.innerText || "") : "";
      const contentEl = el.querySelector(
        "div[data-message-content], div[class*='whitespace-pre-wrap']"
      );
      if (contentEl) {
        const t = contentEl.innerText || "";
        if (t) return t;
      }
      return el.innerText || "";
    },

    // Re-fire the send event after async memory enrichment (React path).
    // armBypass is passed from common.js so we can re-arm the bypass guard
    // immediately before firing — the original timeout may have expired
    // during async retrieve operations.
    async refireAfterSend(input, liveBtn, armBypass, expectedPrompt) {
      // Keep the periodic draft guard from erasing the enriched composer while
      // React is still enabling its live send control. requestSubmit() can be
      // inert in Temporary Chat because the actual send path is button onClick.
      sendTransactionActive = true;
      try {
        let currentInput = null;
        let currentBtn = null;
        let readyToClick = false;
        const expectedComposerText = normalizeComposerText(expectedPrompt);
        for (let attempt = 0; attempt < 6; attempt++) {
          await new Promise((resolve) => setTimeout(resolve, 75));
          currentInput = this.findInput() || input;
          currentBtn = this.findSendButton() || (!expectedComposerText ? findFirst(SEND_BUTTON_SELECTORS) : null);
          const ariaDisabled = currentBtn && typeof currentBtn.getAttribute === "function"
            ? currentBtn.getAttribute("aria-disabled")
            : null;
          if (!currentInput || !currentBtn || currentBtn.isConnected === false ||
            currentBtn.disabled || ariaDisabled === "true" || typeof currentBtn.click !== "function") {
            continue;
          }
          if (expectedComposerText && normalizeComposerText(getComposerText(currentInput)) !== expectedComposerText) {
            continue;
          }

          const inputForm = currentInput.closest ? currentInput.closest("form") : null;
          const buttonForm = currentBtn.form || (currentBtn.closest && currentBtn.closest("form"));
          if (inputForm && buttonForm !== inputForm) continue;
          readyToClick = true;
          break;
        }
        if (!readyToClick || !currentInput || !currentBtn) return false;

        const baselineUsers = new Set(this.getUserMessageElements());
        const baselineStreaming = this.isResponseStreaming();
        if (baselineStreaming) return false;
        const sentComposerText = normalizeComposerText(getComposerText(currentInput));
        if (typeof armBypass === "function") armBypass();
        try {
          currentBtn.click();
        } catch (_) {
          return false;
        }

        const providerAcknowledged = () => {
          return this.getUserMessageElements().some((userElement) =>
            !baselineUsers.has(userElement) && rawUserMessageMatchesExpected(userElement, sentComposerText)
          );
        };
        if (providerAcknowledged()) return true;
        for (let attempt = 0; attempt < 8; attempt++) {
          await new Promise((resolve) => setTimeout(resolve, 75));
          if (providerAcknowledged()) return true;
        }
        return false;
      } finally {
        sendTransactionActive = false;
      }
    },

    // ChatGPT's composer draft-persistence re-populates the input with the
    // sent (enriched) prompt shortly after sending. Staggered artifact-gated
    // sweeps remove it as soon as it reappears; the periodic guard interval
    // below covers late restores and F5 hydration.
    clearInputAfterSend() {
      const clearDelays = [300, 800, 1600, 3000, 5000];
      clearDelays.forEach((delay) => {
        setTimeout(sweepComposerDraft, delay);
      });
    },

    getConversationText(limitWords) {
      const users = this.getUserMessageElements();
      const assistants = this.getAssistantMessageElements();
      const all = [];
      users.forEach((el) => all.push({ el, role: "User" }));
      assistants.forEach((el) => all.push({ el, role: "Model" }));

      all.sort((a, b) => {
        if (a.el === b.el) return 0;
        const pos = a.el.compareDocumentPosition(b.el);
        if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
        if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
        return 0;
      });

      const lines = all.map((item) => `${item.role}: ${item.el.innerText || ""}`);
      let text = lines.join("\n\n");
      const words = text.split(/\s+/);
      if (words.length > limitWords) {
        text = "... " + words.slice(-limitWords).join(" ");
      }
      return text;
    },
    isFirstTurn() {
      return this.getAssistantMessageElements().length === 0;
    }
  };

  startComposerDraftGuard();
  window.biomemInjector.init(adapter);
})();
