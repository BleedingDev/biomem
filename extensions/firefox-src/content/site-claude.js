(function () {
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

  const adapter = {
    siteId: "claude",

    // ── Site classification ──────────────────────────────────────────
    // These flags replace the SHADOW_DOM_SITES / REACT_SITES sets that
    // used to live in common.js, eliminating cross-site branching there.
    isReactSite: true,
    isShadowDom: false,

    isSafeToMask(el) {
      // Reject anything outside the main chat container. Claude's left sidebar
      // frequently receives the raw user prompt as a temporary chat title before
      // the backend generates the final title. If biomem sweeps and masks that
      // sidebar element, it crashes React's reconciliation.

      // Use the composer input as an anchor to find the main chat column
      const input = this.findInput();
      if (input) {
        let mainArea = input;
        while (mainArea && mainArea !== document.body) {
          // Claude's main chat container is usually a flex-1 column
          if (mainArea.classList && mainArea.classList.contains("flex-1")) {
            if (!mainArea.contains(el)) return false; // Not in main chat area -> unsafe!
            break;
          }
          mainArea = mainArea.parentElement;
        }
      } else {
        // Fallback if input not found
        const container = this.getMessageContainer();
        if (container && container !== document.body) {
          if (!container.contains(el)) return false;
        }
      }

      // Extra explicit rejection for sidebar items just in case
      if (el.closest && el.closest("a[href*='/chat/'], [data-testid*='menu'], [data-testid*='sidebar']")) {
        return false;
      }

      return true;
    },

    findInput() {
      return findFirst([
        "textarea",
        "div[contenteditable='true']"
      ]);
    },
    findSendButton() {
      return findFirst([
        "button[aria-label='Send']",
        "button[aria-label='Send message']",
        "button[type='submit']"
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
        "div[data-testid='user-message']",
        "div[data-message-author-role='user']"
      ]);
    },
    getAssistantMessageElements() {
      return findAll([
        "div[data-testid='assistant-message']",
        "div[data-message-author-role='assistant']"
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
      el.innerText = text;
    },

    // Re-fire the send event after async memory enrichment (React path).
    // armBypass is passed from common.js so we can re-arm the bypass guard
    // immediately before firing — the original timeout may have expired
    // during async retrieve operations.
    async refireAfterSend(input, liveBtn, armBypass) {
      armBypass();
      if (liveBtn) {
        liveBtn.click();
      } else {
        input.dispatchEvent(
          new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true })
        );
      }
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

  window.biomemInjector.init(adapter);
})();
