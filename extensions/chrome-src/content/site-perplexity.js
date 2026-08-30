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

  // ── Leftover enriched-prompt detector (used by clearInputAfterSend) ──
  // Perplexity's React composer strips pipe chars via markdown rendering,
  // so bare STPAM/ENDPAM tokens may survive without the | delimiters.
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

  const adapter = {
    siteId: "perplexity",

    // ── Site classification ──────────────────────────────────────────
    // These flags replace the SHADOW_DOM_SITES / REACT_SITES sets that
    // used to live in common.js, eliminating cross-site branching there.
    isReactSite: true,
    isShadowDom: false,

    getReactOverlayTarget(el) {
      if (!el) return el;
      // We want to hide the innermost text container so the bubble background
      // and layout remain visible. Perplexity uses .my-md, whitespace-pre-wrap, or p.
      const target = el.querySelector(".my-md, div[class*='whitespace-pre-wrap'], p, div.prose");
      if (target) return target;

      // Fallback: ignore sr-only labels and find the first visible layout child
      const children = Array.from(el.children).filter(c =>
        !c.classList.contains("sr-only") && !c.classList.contains("bdbm-overlay-text")
      );
      if (children.length > 0) return children[0];
      return el;
    },

    findInput() {
      return findFirst([
        "textarea[placeholder*='Ask']",
        "textarea[aria-label*='Ask']",
        "textarea[placeholder*='Search']",
        "textarea[placeholder*='search']",
        "textarea",
        "div[contenteditable='true'][role='textbox']",
        "div[contenteditable='true']"
      ]);
    },

    findSendButton() {
      return findFirst([
        "button[aria-label='Submit']",
        "button[aria-label='Send']",
        "button[type='submit']",
        "button[aria-label*='submit' i]",
        "button[aria-label*='send' i]"
      ]);
    },

    getMessageContainer() {
      return findFirst([
        "main",
        "[role='main']",
        "div[class*='conversation']",
        "body"
      ]);
    },

    getUserMessageElements() {
      return findAll([
        "[data-testid*='user-query']",
        "[data-testid*='user-message']",
        "[data-testid*='query']",
        ".my-md",
        "div[class*='UserMessage']",
        "div[class*='user-message']"
      ]);
    },

    getAssistantMessageElements() {
      return findAll([
        "[data-testid*='answer']",
        "[data-testid*='response-content']",
        ".prose",
        ".prose-sm",
        "[class*='AnswerBody']",
        "[class*='AssistantMessage']",
        "div.markdown"
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
      const target = el.querySelector &&
        el.querySelector("div[class*='whitespace-pre-wrap'], p");
      if (target) {
        target.innerText = text;
        return;
      }
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

    // Perplexity's React composer does not auto-clear after a JS-triggered
    // send (it receives the enriched value we wrote via JS and doesn't
    // re-clear the composer). Staggered retries handle async re-renders.
    // Must run for BOTH sync (cache-hit) and async send paths.
    clearInputAfterSend(input) {
      const clearDelays = [200, 500, 1000, 2000, 4000];
      clearDelays.forEach((delay) => {
        setTimeout(() => {
          const liveInput = this.findInput ? this.findInput() : input;
          if (!liveInput) return;
          const currentText = liveInput.isContentEditable
            ? (liveInput.innerText || liveInput.textContent || "")
            : (liveInput.value || "");
          if (!currentText.trim()) return;
          if (!looksLikeLeftoverEnriched(currentText)) return;

          // Try execCommand('delete') first — fires a trusted InputEvent
          try { liveInput.focus(); } catch (_) {}
          try {
            const sel = window.getSelection();
            if (sel) {
              sel.removeAllRanges();
              const range = document.createRange();
              range.selectNodeContents(liveInput);
              sel.addRange(range);
            }
            document.execCommand("delete", false);
          } catch (_) {}

          // Fallback: direct property set
          const afterRaw = liveInput.isContentEditable
            ? (liveInput.innerText || liveInput.textContent || "")
            : (liveInput.value || "");
          if (afterRaw.trim() && looksLikeLeftoverEnriched(afterRaw.trim())) {
            if (liveInput.isContentEditable) {
              liveInput.textContent = "";
              liveInput.innerHTML = "";
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
        }, delay);
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

  window.biomemInjector.init(adapter);
})();
