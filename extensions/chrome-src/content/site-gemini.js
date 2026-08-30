(function () {
  // ── DIAGNOSTIC LOGGING (temporary) ─────────────────────────────────
  // Set to false to silence. Logs the masking surgery step-by-step so we
  // can find where the user's original prompt gets wiped from the bubble.
  const biomem_GEMINI_DIAG = false;
  function gdiag(label, data) {
    // Disabled for distribution
  }
  function elBrief(el) {
    if (!el) return "(null)";
    try {
      const tag = (el.tagName || el.nodeName || "?").toLowerCase();
      const cls = el.className && typeof el.className === "string"
        ? "." + el.className.trim().split(/\s+/).slice(0, 3).join(".")
        : "";
      const id = el.id ? "#" + el.id : "";
      return `${tag}${id}${cls}`;
    } catch (_) { return "(?)"; }
  }
  function preview(t, n) {
    return (t || "").slice(0, n || 120).replace(/\n/g, "\\n");
  }

  // ── Deep Shadow DOM traversal ──────────────────────────────────────
  // Gemini uses Angular/Lit web components with Shadow DOM.
  // We must recursively enter every shadow root to find chat elements.

  const _rootCache = { roots: [], ts: 0 };
  const ROOT_CACHE_TTL = 600; // ms

  function collectRoots(base) {
    const now = Date.now();
    if (_rootCache.roots.length && now - _rootCache.ts < ROOT_CACHE_TTL) {
      return _rootCache.roots;
    }
    const roots = [];
    const visited = new Set();
    const start = base || document;

    function walk(root) {
      if (!root || visited.has(root)) return;
      visited.add(root);
      roots.push(root);
      const els = root.querySelectorAll ? root.querySelectorAll("*") : [];
      for (const el of els) {
        if (el && el.shadowRoot) walk(el.shadowRoot);
      }
    }

    walk(start);
    _rootCache.roots = roots;
    _rootCache.ts = now;
    return roots;
  }

  function invalidateRootCache() {
    _rootCache.ts = 0;
  }

  function findFirst(selectors) {
    const roots = collectRoots(document);
    for (const root of roots) {
      for (const sel of selectors) {
        try {
          const el = root.querySelector ? root.querySelector(sel) : null;
          if (el) return el;
        } catch (_) { /* skip */ }
      }
    }
    return null;
  }

  function findAll(selectors) {
    const roots = collectRoots(document);
    for (const sel of selectors) {
      const merged = [];
      const seen = new Set();
      for (const root of roots) {
        if (!root.querySelectorAll) continue;
        try {
          const els = root.querySelectorAll(sel);
          for (const el of els) {
            if (!seen.has(el)) {
              seen.add(el);
              merged.push(el);
            }
          }
        } catch (_) { /* skip */ }
      }
      if (merged.length) return merged;
    }
    return [];
  }

  // ── Helpers ────────────────────────────────────────────────────────

  const SYSTEM_LEAK_RE = /<user_context|<\/user_context>|<System\s*-|<\/System\s*-|\|STPAM\||\|MIDPAM\||\|ENDPAM\||\|MEMQUERY\||\|ENDQUERY\||\|TITLE\|/i;
  const STRONG_SYSTEM_RE = /<user_context>|<current_time>|<relevant_memories>|<response_format>|<System\s*-\s*(Current Date and Time|associated memory context|Additional instruction|Deep Recall|New Conversation thread)/i;

  function getTextContentDeep(el) {
    if (!el) return "";
    let text = el.innerText || "";
    if (el.shadowRoot) {
      const shadowText = el.shadowRoot.textContent || "";
      if (shadowText.length > text.length) text = shadowText;
    }
    return text;
  }

  function isVisibleEl(el) {
    if (!el || !el.isConnected) return false;
    if (el.closest && el.closest(".bdbm-injector-panel")) return false;
    const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    if (style && (style.display === "none" || style.visibility === "hidden" || style.opacity === "0")) {
      return false;
    }
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    if (!rect) return true;
    return rect.width > 0 && rect.height > 0;
  }

  /**
   * Nuclear fallback: scan ALL text nodes across all shadow roots
   * for system leak patterns.
   */
  function findLeakedTextNodes() {
    const results = [];
    const roots = collectRoots(document);
    const seen = new Set();

    for (const root of roots) {
      if (!root.ownerDocument && root !== document) continue;
      const doc = root.ownerDocument || root;
      if (typeof doc.createTreeWalker !== "function") continue;

      let walker;
      try {
        walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
      } catch (_) { continue; }

      let textNode = walker.nextNode();
      while (textNode) {
        const value = textNode.nodeValue || "";
        if (STRONG_SYSTEM_RE.test(value) || SYSTEM_LEAK_RE.test(value)) {
          let el = textNode.parentElement;
          while (el && el !== document.body) {
            if (!isVisibleEl(el)) { el = el.parentElement; continue; }
            const tag = (el.tagName || "").toUpperCase();
            if (tag === "BODY" || tag === "HTML") break;
            if (el.childElementCount <= 80 && !seen.has(el)) {
              seen.add(el);
              results.push(el);
              break;
            }
            el = el.parentElement;
          }
        }
        textNode = walker.nextNode();
      }
    }

    if (results.length) {
      gdiag("findLeakedTextNodes RESULTS", results.map((el) => ({
        element: elBrief(el),
        textPreview: preview(getTextContentDeep(el), 150)
      })));
    }
    return results;
  }

  function replaceTextDeep(el, newText) {
    if (!el) return;

    gdiag("replaceTextDeep ENTER", {
      element: elBrief(el),
      newTextLen: (newText || "").length,
      newTextPreview: preview(newText, 200),
      elTextBefore: preview(getTextContentDeep(el), 300)
    });
    if (!(newText || "").trim()) {
      gdiag("replaceTextDeep WARNING: newText is EMPTY/whitespace — bubble would end up blank!");
    }

    // ── Phase 1: Replace text nodes in the element itself ──
    // Walk text nodes; replace the FIRST VISIBLE leaked one with cleanText,
    // empty subsequent leaked ones.  This preserves the element's
    // HTML structure (classes, padding, bubble styling).
    //
    // Screen-reader-only nodes (Gemini's h5.cdk-visually-hidden label holds a
    // full copy of the enriched prompt and precedes the visible text in DOM
    // order) must NOT absorb the replacement — otherwise the clean text lands
    // in an invisible element and the visible bubble text gets cleared as an
    // "enriched remnant", leaving the bubble blank. Such nodes get the clean
    // text written in-place (keeps the accessibility label correct) without
    // counting as the visible replacement.
    let replaced = false;
    let diagNodeIndex = 0;
    let diagClearedCount = 0;

    function isSrOnlyOrHiddenAncestor(elm, stopAt) {
      let cur = elm;
      while (cur && cur !== stopAt && cur.nodeType === 1) {
        const cls = typeof cur.className === "string" ? cur.className : "";
        if (/(?:^|\s)(?:cdk-visually-hidden|visually-hidden|sr-only)(?:\s|$)/i.test(cls)) return true;
        if (/screen-reader/i.test(cls)) return true;
        if (cur.getAttribute && cur.getAttribute("aria-hidden") === "true") return true;
        try {
          const st = window.getComputedStyle(cur);
          if (st && (st.display === "none" || st.visibility === "hidden")) return true;
          // clip:rect(0,0,0,0) pattern used by visually-hidden helpers
          if (st && st.position === "absolute" && st.clip && st.clip.indexOf("rect") === 0) return true;
        } catch (_) { /* cross-shadow style access may fail */ }
        cur = cur.parentElement;
      }
      return false;
    }

    function replaceTextNodes(root) {
      if (!root) return;
      const doc = root.ownerDocument || document;
      if (!doc || typeof doc.createTreeWalker !== "function") return;

      let walker;
      try { walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null); } catch (_) { return; }

      const toProcess = [];
      let tn = walker.nextNode();
      while (tn) { toProcess.push(tn); tn = walker.nextNode(); }

      for (const node of toProcess) {
        const val = node.nodeValue || "";
        if (!val.trim()) continue;
        diagNodeIndex++;

        // Screen-reader / hidden nodes: sync their leaked text with the clean
        // text, but never treat them as the visible replacement target and
        // never clear them as "post-replacement remnants".
        if (isSrOnlyOrHiddenAncestor(node.parentElement, root)) {
          if (SYSTEM_LEAK_RE.test(val)) {
            gdiag(`replaceTextDeep node#${diagNodeIndex} SR-HIDDEN leak -> synced with newText (not a visible replacement)`, {
              parent: elBrief(node.parentElement),
              valuePreview: preview(val, 100)
            });
            node.nodeValue = newText;
          } else {
            gdiag(`replaceTextDeep node#${diagNodeIndex} SR-HIDDEN kept`, {
              parent: elBrief(node.parentElement),
              valuePreview: preview(val, 100)
            });
          }
          continue;
        }

        if (replaced) {
          // After the first system-tag node was replaced with the user's clean
          // text, ALL subsequent non-empty text nodes belong to the enriched
          // prompt and must be cleared.
          gdiag(`replaceTextDeep node#${diagNodeIndex} CLEARED (after first replace)`, {
            parent: elBrief(node.parentElement),
            valuePreview: preview(val, 100)
          });
          node.nodeValue = "";
          diagClearedCount++;
        } else if (SYSTEM_LEAK_RE.test(val)) {
          // First leaked node — replace with the clean user text.
          gdiag(`replaceTextDeep node#${diagNodeIndex} LEAK MATCH -> replacing with newText`, {
            parent: elBrief(node.parentElement),
            valuePreview: preview(val, 100)
          });
          node.nodeValue = newText;
          replaced = true;
        } else {
          gdiag(`replaceTextDeep node#${diagNodeIndex} kept (no leak match yet)`, {
            parent: elBrief(node.parentElement),
            valuePreview: preview(val, 100)
          });
        }
        // Text nodes before any SYSTEM_LEAK_RE match are left unchanged.
      }

      // Recurse into nested shadow roots within this root
      if (root.querySelectorAll) {
        try {
          const nested = root.querySelectorAll("*");
          for (const child of nested) {
            if (child.shadowRoot) replaceTextNodes(child.shadowRoot);
          }
        } catch (_) { /* skip */ }
      }
    }

    replaceTextNodes(el);
    if (el.shadowRoot) replaceTextNodes(el.shadowRoot);

    gdiag("replaceTextDeep Phase1 DONE", {
      replaced,
      nodesVisited: diagNodeIndex,
      nodesCleared: diagClearedCount
    });

    // ── Phase 2: Collapse now-empty container elements ─────────────────
    // After clearing text nodes, their parent elements still occupy space.
    // Walk all descendants and hide any whose textContent is now empty.
    if (replaced) {
      let diagHiddenCount = 0;
      function hideEmptyDescendants(root) {
        if (!root || !root.querySelectorAll) return;
        try {
          const children = Array.from(root.querySelectorAll("*"));
          for (let i = children.length - 1; i >= 0; i--) {
            const child = children[i];
            if (!(child.textContent || "").trim()) {
              child.style.setProperty("display", "none", "important");
              diagHiddenCount++;
            }
          }
        } catch (_) { /* skip */ }
      }
      hideEmptyDescendants(el);
      if (el.shadowRoot) hideEmptyDescendants(el.shadowRoot);
      gdiag("replaceTextDeep Phase2 DONE (hideEmptyDescendants)", {
        elementsHidden: diagHiddenCount
      });
    }

    gdiag("replaceTextDeep EXIT", {
      elTextAfter: preview(getTextContentDeep(el), 300),
      elVisibleAfter: isVisibleEl(el)
    });
  }

  // ── Leftover enriched-prompt detector (shared by clearInputAfterSend) ──
  function looksLikeLeftoverEnriched(text) {
    if (!text) return false;
    if (window.BdbmPromptBuilder && window.BdbmPromptBuilder.containsControlArtifacts) {
      if (window.BdbmPromptBuilder.containsControlArtifacts(text)) return true;
    }
    // Bare STPAM/ENDPAM tokens (Gemini may strip pipe chars via markdown rendering)
    if (/\bSTPAM\b/i.test(text) && /\bENDPAM\b/i.test(text)) return true;
    return /<user_context|<System\s*-/i.test(text) ||
      /\bSystem\s*-\s*(?:User's personal memory|Current Date|associated memory|Additional instruction)/i.test(text) ||
      /Summary of (?:my query|the USER'S QUERY)/i.test(text) ||
      /Summary of (?:your response|YOUR RESPONSE)/i.test(text);
  }

  // ── The adapter ────────────────────────────────────────────────────

  const adapter = {
    siteId: "gemini",

    // ── Site classification ──────────────────────────────────────────
    // These flags replace the SHADOW_DOM_SITES / REACT_SITES sets that
    // used to live in common.js, eliminating cross-site branching there.
    isReactSite: false,
    isShadowDom: true,

    findInput() {
      return findFirst([
        "textarea[aria-label='Enter a prompt here']",
        "textarea[aria-label='Enter a prompt']",
        "textarea[aria-label*='prompt']",
        "textarea[aria-label*='chat']",
        "textarea[aria-label*='Ask']",
        "textarea",
        "div[role='textbox']",
        "div[contenteditable='true']"
      ]);
    },

    findSendButton() {
      return findFirst([
        "button[aria-label='Send']",
        "button[aria-label='Send message']",
        "button[aria-label='Submit']",
        "button[data-testid*='send']",
        "button[type='submit']"
      ]);
    },

    getMessageContainer() {
      return findFirst([
        "[role='log']",
        "[aria-live]",
        "main",
        "div[role='main']",
        "body"
      ]);
    },

    getUserMessageElements() {
      let els = findAll([
        "[data-message-author-role='user']",
        "[data-author='user']",
        "[data-testid*='user']",
        "div.user-message",
        "[class*='user-message']",
        "[class*='query-chip']",
        "[class*='user-turn']",
        "div[role='listitem'] div[role='heading']"
      ]);
      return els;
    },

    getAssistantMessageElements() {
      let els = findAll([
        "[data-message-author-role='assistant']",
        "[data-author='assistant']",
        "[data-testid*='model']",
        "div.model-message",
        "[class*='model-response']",
        "[class*='model-turn']",
        "div[role='listitem'] div.markdown",
        "div.markdown"
      ]);
      return els;
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
      return getTextContentDeep(el);
    },

    replaceMessageText(el, text) {
      replaceTextDeep(el, text);
    },

    findLeakedElements() {
      return findLeakedTextNodes();
    },

    invalidateCache() {
      invalidateRootCache();
    },

    // ── Shadow DOM input writing ──────────────────────────────────────
    // Gemini uses Lit/Angular with Shadow DOM. Direct textContent writes
    // + synthetic InputEvent are untrusted and the framework ignores them,
    // leaving its internal model out-of-sync. execCommand('insertText')
    // fires a TRUSTED InputEvent from the browser editing pipeline that
    // Lit ChildPart and Angular ngModel observe correctly.
    writeInputValue(input, value) {
      const _norm = (t) => (t || "").replace(/\s+/g, " ").trim();
      const lastValue = input.innerText || input.textContent || "";
      try { input.focus(); } catch (_) { }

      // Select all existing content so insertText replaces it
      try {
        const sel = window.getSelection();
        if (sel) {
          sel.removeAllRanges();
          const range = document.createRange();
          range.selectNodeContents(input);
          sel.addRange(range);
        }
      } catch (_) { }

      let handled = false;
      if (document.queryCommandSupported && document.queryCommandSupported("insertText")) {
        try {
          handled = document.execCommand("insertText", false, value);
        } catch (_) { }
      }

      if (!handled) {
        input.textContent = value;
      }

      // Verify the DOM matches what we intended
      const resulting = _norm(input.innerText || input.textContent || "");
      if (resulting !== _norm(value)) {
        input.textContent = value;
      }

      // Dispatch composed input event so it crosses shadow DOM boundaries
      try {
        input.dispatchEvent(new InputEvent("input", {
          bubbles: true, composed: true, inputType: "insertText", data: value
        }));
      } catch (_) {
        input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      }
      input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      return lastValue;
    },

    // ── Shadow DOM re-fire ────────────────────────────────────────────
    // Lit/Angular components process the input event asynchronously —
    // wait 80ms so the component's internal state syncs with the enriched
    // text before re-firing the send. armBypass is re-armed after the
    // wait because the original bypass timeout expires during the await.
    async refireAfterSend(input, liveBtn, armBypass) {
      await new Promise((r) => setTimeout(r, 80));
      armBypass(); // Re-arm after the async wait
      const sdBtn = liveBtn || (this.findSendButton ? this.findSendButton() : null);
      if (sdBtn) {
        sdBtn.click();
      } else {
        input.dispatchEvent(new KeyboardEvent("keydown", {
          key: "Enter", code: "Enter", keyCode: 13, which: 13,
          bubbles: true, composed: true, cancelable: true
        }));
        // Some frameworks listen on keypress/keyup rather than keydown
        setTimeout(() => {
          input.dispatchEvent(new KeyboardEvent("keypress", {
            key: "Enter", code: "Enter", keyCode: 13, which: 13,
            bubbles: true, composed: true, cancelable: true
          }));
        }, 10);
        setTimeout(() => {
          input.dispatchEvent(new KeyboardEvent("keyup", {
            key: "Enter", code: "Enter", keyCode: 13, which: 13,
            bubbles: true, composed: true
          }));
        }, 20);
      }
    },

    // ── Post-send input clearing ──────────────────────────────────────
    // Gemini's Lit/Angular composer does not auto-clear after a JS-triggered
    // send. Staggered retries handle async Lit re-renders. Uses
    // execCommand('delete') which fires a trusted InputEvent.
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

          // Try execCommand('delete') — fires trusted InputEvent
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

          // Fallback if execCommand didn't work
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
        try {
          const pos = a.el.compareDocumentPosition(b.el);
          if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
          if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
        } catch (_) { /* cross-shadow comparison may fail */ }
        return 0;
      });

      const lines = all.map((item) => `${item.role}: ${getTextContentDeep(item.el)}`);
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
