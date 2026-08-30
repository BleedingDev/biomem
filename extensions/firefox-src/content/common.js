(function () {
  const STATE = {
    config: null,
    client: null,
    adapter: null,
    connected: false,
    memoryEnabled: true,
    isSending: false,
    bypass: false,
    bypassTimer: null,
    sendGuardUntil: 0,
    pendingUserQueue: [],
    pendingStore: null,
    assistantTimer: null,
    lastAssistantText: "",
    lastTurn: null,
    learnedSelectors: null,
    lastMemoryCount: null,
    prefetchTimer: null,
    prefetchText: "",
    prefetchMemories: [],
    prefetchSessionId: "",
    prefetchAt: 0,
    prefetchInFlight: false,
    prefetchPromise: null,
    leakSweepTimer: null,
    hooksSetUp: false,
    panelUi: {
      collapsed: false,
      left: null,
      top: null,
      expandedWidth: null
    }
  };

  function log(msg) {
    // Silenced for distribution
  }

  const DEBUG_PROMPT_IO = false;
  const PANEL_UI_STATE_KEY = "injectorPanelUi";
  const PANEL_DRAG_THRESHOLD = 4;

  function debugIo(label, payload) {
    // Production no-op: callers may pass prompt, response, or memory content.
  }

  async function loadPanelUiState() {
    return new Promise((resolve) => {
      chrome.storage.local.get(PANEL_UI_STATE_KEY, (res) => {
        const value = res ? res[PANEL_UI_STATE_KEY] : null;
        if (value && typeof value === "object") resolve(value);
        else resolve({});
      });
    });
  }

  async function savePanelUiState(state) {
    return new Promise((resolve) => {
      chrome.storage.local.set({ [PANEL_UI_STATE_KEY]: state }, () => resolve());
    });
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function clampPanelPosition(panel, left, top) {
    const margin = 8;
    const width = panel.offsetWidth || panel.getBoundingClientRect().width || 0;
    const height = panel.offsetHeight || panel.getBoundingClientRect().height || 0;
    const maxLeft = Math.max(margin, window.innerWidth - width - margin);
    const maxTop = Math.max(margin, window.innerHeight - height - margin);
    return {
      left: clamp(left, margin, maxLeft),
      top: clamp(top, margin, maxTop)
    };
  }

  function applyPanelPosition(panel, left, top) {
    if (!Number.isFinite(left) || !Number.isFinite(top)) return null;
    const clamped = clampPanelPosition(panel, left, top);
    panel.style.left = `${Math.round(clamped.left)}px`;
    panel.style.top = `${Math.round(clamped.top)}px`;
    panel.style.right = "auto";
    panel.style.bottom = "auto";
    panel.style.transform = "none";
    STATE.panelUi.left = clamped.left;
    STATE.panelUi.top = clamped.top;
    return clamped;
  }

  async function persistPanelUiState() {
    const next = { collapsed: !!STATE.panelUi.collapsed };
    if (Number.isFinite(STATE.panelUi.left) && Number.isFinite(STATE.panelUi.top)) {
      next.left = Math.round(STATE.panelUi.left);
      next.top = Math.round(STATE.panelUi.top);
    }
    await savePanelUiState(next);
  }

  function formatMemoryLine(m) {
    const userContent = m.user || "[no user]";
    const modelContent = m.model || "[no model]";
    const td = typeof m.turn_distance === "number" ? m.turn_distance : "?";
    const conf = typeof m.confidence === "number" ? m.confidence : "?";
    return `User: ${userContent} | Model: ${modelContent} | Turn distance: ${td} | Confidence: ${conf}`;
  }

  async function createPanel() {
    const panel = document.createElement("div");
    panel.className = "bdbm-injector-panel";
    panel.innerHTML = `
      <img class="bdbm-injector-logo" id="bdbm-injector-logo" alt="biomem" role="button" tabindex="0" aria-label="Toggle panel">
      <div class="bdbm-injector-body" id="bdbm-injector-body">
        <span class="bdbm-injector-dot" id="bdbm-injector-dot"></span>
        <span id="bdbm-injector-text">biomem: connecting…</span>
        <button class="bdbm-injector-toggle" id="bdbm-injector-toggle">Memory ON</button>
        <button class="bdbm-injector-btn" id="bdbm-injector-learn">Learn UI</button>
        <button class="bdbm-injector-btn bdbm-injector-action" id="bdbm-injector-action" type="button" hidden></button>
      </div>
    `;
    document.body.appendChild(panel);

    const isPanelInRightHalf = (rect) => {
      const sourceRect = rect || panel.getBoundingClientRect();
      return (sourceRect.left + sourceRect.width / 2) >= (window.innerWidth / 2);
    };

    const updateCollapseDirection = (rect) => {
      const collapseRight = isPanelInRightHalf(rect);
      panel.classList.toggle("collapse-right", collapseRight);
      return collapseRight;
    };

    function setCollapsed(collapsed, persist = true) {
      const targetCollapsed = !!collapsed;
      const rectBefore = panel.getBoundingClientRect();
      const collapseRight = updateCollapseDirection(rectBefore);

      if (targetCollapsed) {
        if (!STATE.panelUi.collapsed && rectBefore.width > 0) {
          STATE.panelUi.expandedWidth = rectBefore.width;
        }
        STATE.panelUi.collapsed = true;
        panel.classList.add("collapsed");
        panel.setAttribute("aria-expanded", "false");

        const rectAfter = panel.getBoundingClientRect();
        if (rectBefore.width > 0 && rectAfter.width > 0) {
          const anchoredLeft = collapseRight
            ? (rectBefore.right - rectAfter.width)
            : rectBefore.left;
          applyPanelPosition(panel, anchoredLeft, rectBefore.top);
        }
        updateCollapseDirection();
      } else {
        const expectedExpandedWidth = Number.isFinite(STATE.panelUi.expandedWidth)
          ? STATE.panelUi.expandedWidth
          : rectBefore.width;
        if (rectBefore.width > 0 && expectedExpandedWidth > 0) {
          const preExpandLeft = collapseRight
            ? (rectBefore.right - expectedExpandedWidth)
            : rectBefore.left;
          // Move first while still collapsed so expanded width is not shrink-fitted.
          applyPanelPosition(panel, preExpandLeft, rectBefore.top);
        }

        STATE.panelUi.collapsed = false;
        panel.classList.remove("collapsed");
        panel.setAttribute("aria-expanded", "true");

        const rectAfter = panel.getBoundingClientRect();
        if (rectAfter.width > 0) {
          STATE.panelUi.expandedWidth = rectAfter.width;
          if (rectBefore.width > 0) {
            const anchoredLeft = collapseRight
              ? (rectBefore.right - rectAfter.width)
              : rectBefore.left;
            applyPanelPosition(panel, anchoredLeft, rectBefore.top);
          }
        }
      }

      if (persist) {
        persistPanelUiState().catch(() => { });
      }

      const logoEl = panel.querySelector("#bdbm-injector-logo");
      if (logoEl) {
        logoEl.src = chrome.runtime.getURL(STATE.panelUi.collapsed ? "assets/icon128.png" : "assets/icon128.png");
      }
    }

    function toggleCollapsed() {
      setCollapsed(!STATE.panelUi.collapsed, true);
    }

    const logo = panel.querySelector("#bdbm-injector-logo");
    if (logo) {
      logo.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggleCollapsed();
        }
      });
    }

    const toggle = panel.querySelector("#bdbm-injector-toggle");
    toggle.addEventListener("click", () => {
      STATE.memoryEnabled = !STATE.memoryEnabled;
      toggle.textContent = STATE.memoryEnabled ? "Memory ON" : "Memory OFF";
      toggle.style.opacity = STATE.memoryEnabled ? "1" : "0.6";
    });

    const learnBtn = panel.querySelector("#bdbm-injector-learn");
    learnBtn.addEventListener("click", () => startLearning());
    learnBtn.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      resetLearning();
    });

    let dragState = null;
    const isDragExcluded = (target) => {
      if (!target || !target.closest) return false;
      return !!target.closest("button, select, option");
    };

    panel.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      const target = e.target;
      const isLogoTarget = !!(target && target.closest && target.closest("#bdbm-injector-logo"));
      if (!isLogoTarget && isDragExcluded(target)) return;
      const rect = panel.getBoundingClientRect();
      dragState = {
        pointerId: e.pointerId,
        startX: e.clientX,
        startY: e.clientY,
        originLeft: rect.left,
        originTop: rect.top,
        dragging: false,
        logoCandidate: isLogoTarget
      };
      try {
        panel.setPointerCapture(e.pointerId);
      } catch (_) { }
    });

    panel.addEventListener("pointermove", (e) => {
      if (!dragState || dragState.pointerId !== e.pointerId) return;
      const dx = e.clientX - dragState.startX;
      const dy = e.clientY - dragState.startY;
      if (!dragState.dragging) {
        if (Math.abs(dx) < PANEL_DRAG_THRESHOLD && Math.abs(dy) < PANEL_DRAG_THRESHOLD) return;
        dragState.dragging = true;
        panel.classList.add("dragging");
      }
      e.preventDefault();
      applyPanelPosition(panel, dragState.originLeft + dx, dragState.originTop + dy);
      if (STATE.panelUi.collapsed) {
        updateCollapseDirection();
      }
    });

    const finishPointer = (e) => {
      if (!dragState || dragState.pointerId !== e.pointerId) return;
      const finished = dragState;
      dragState = null;
      panel.classList.remove("dragging");
      try {
        panel.releasePointerCapture(e.pointerId);
      } catch (_) { }
      if (finished.dragging) {
        persistPanelUiState().catch(() => { });
        return;
      }
      if (finished.logoCandidate) {
        toggleCollapsed();
      }
    };

    panel.addEventListener("pointerup", finishPointer);
    panel.addEventListener("pointercancel", finishPointer);

    const savedUi = await loadPanelUiState();
    const savedCollapsed = !!savedUi.collapsed;
    STATE.panelUi.collapsed = false;
    if (Number.isFinite(savedUi.left) && Number.isFinite(savedUi.top)) {
      applyPanelPosition(panel, savedUi.left, savedUi.top);
    } else {
      STATE.panelUi.left = null;
      STATE.panelUi.top = null;
    }
    setCollapsed(savedCollapsed, false);

    window.addEventListener("resize", () => {
      if (!Number.isFinite(STATE.panelUi.left) || !Number.isFinite(STATE.panelUi.top)) return;
      const prevLeft = STATE.panelUi.left;
      const prevTop = STATE.panelUi.top;
      const next = applyPanelPosition(panel, prevLeft, prevTop);
      if (!next) return;
      if (STATE.panelUi.collapsed) {
        updateCollapseDirection();
      }
      if (Math.round(next.left) !== Math.round(prevLeft) || Math.round(next.top) !== Math.round(prevTop)) {
        persistPanelUiState().catch(() => { });
      }
    });
  }

  let toastEl = null;
  function showToast(text) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "bdbm-injector-toast";
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = text;
    toastEl.style.display = "block";
  }
  function hideToast() {
    if (toastEl) toastEl.style.display = "none";
  }

  function openOptionsPage() {
    try {
      chrome.runtime.sendMessage({ type: "openOptions" }, () => { /* ignore response */ });
    } catch (_) { /* ignore */ }
  }

  function openWizardPage() {
    try {
      chrome.runtime.sendMessage({ type: "openWizard" }, () => { /* ignore response */ });
    } catch (_) { /* ignore */ }
  }

  function setActionButton(label, handler) {
    const btn = document.getElementById("bdbm-injector-action");
    if (!btn) return;
    if (!label) {
      btn.hidden = true;
      btn.textContent = "";
      btn.onclick = null;
      btn.classList.remove("bdbm-action-help");
      return;
    }
    btn.hidden = false;
    btn.textContent = label;
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (typeof handler === "function") handler();
    };
    btn.classList.toggle("bdbm-action-help", label === "?");
  }

  function updatePanel(connected) {
    const dot = document.getElementById("bdbm-injector-dot");
    const text = document.getElementById("bdbm-injector-text");
    if (!dot || !text) return;
    dot.classList.toggle("connected", !!connected);
    dot.classList.remove("warning");

    if (!connected) {
      const lastError = STATE.client && STATE.client.lastError;
      text.textContent = lastError ? `biomem: ${lastError}` : "biomem: software not running";
      setActionButton("?", openWizardPage);
    } else {
      let label = "biomem: connected";
      if (typeof STATE.lastMemoryCount === "number") {
        label += ` • mem:${STATE.lastMemoryCount}`;
      }
      text.textContent = label;
      setActionButton(null);
    }

    if (!STATE.panelUi.collapsed) {
      const panel = document.querySelector(".bdbm-injector-panel");
      if (panel) {
        const rect = panel.getBoundingClientRect();
        if (rect.width > 0) {
          STATE.panelUi.expandedWidth = rect.width;
        }
      }
    }
  }

  async function loadConfig() {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: "getConfig" }, (resp) => {
        if (resp && resp.ok) resolve(resp.config);
        else resolve(null);
      });
    });
  }

  async function loadLearnedSelectors() {
    return new Promise((resolve) => {
      chrome.storage.local.get("learnedSelectors", (res) => {
        resolve(res.learnedSelectors || {});
      });
    });
  }

  async function saveLearnedSelectors(next) {
    return new Promise((resolve) => {
      chrome.storage.local.set({ learnedSelectors: next }, () => resolve());
    });
  }

  function getHostKey() {
    return window.location.host;
  }

  function getLearnedForHost() {
    if (!STATE.learnedSelectors) return null;
    return STATE.learnedSelectors[getHostKey()] || null;
  }

  function buildSelector(el) {
    if (!el || !el.tagName) return null;
    const stableAttrs = ["data-testid", "data-test-id", "data-qa", "aria-label", "role", "name", "id"];
    for (const attr of stableAttrs) {
      const val = el.getAttribute(attr);
      if (val) {
        if (attr === "id") return `#${CSS.escape(val)}`;
        return `${el.tagName.toLowerCase()}[${attr}="${CSS.escape(val)}"]`;
      }
    }

    let parent = el.parentElement;
    while (parent && parent !== document.body) {
      for (const attr of stableAttrs) {
        const val = parent.getAttribute(attr);
        if (val) {
          const parentSel = attr === "id"
            ? `#${CSS.escape(val)}`
            : `${parent.tagName.toLowerCase()}[${attr}="${CSS.escape(val)}"]`;
          return `${parentSel} ${el.tagName.toLowerCase()}`;
        }
      }
      parent = parent.parentElement;
    }
    return el.tagName.toLowerCase();
  }

  function isValidInput(el) {
    if (!el) return false;
    if (el.disabled) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 40 || rect.height < 20) return false;
    return true;
  }

  function findHeuristicInput() {
    const active = getDeepActiveElement();
    if (active && (active.tagName === "TEXTAREA" || active.isContentEditable) && isValidInput(active)) {
      return active;
    }
    const candidates = Array.from(document.querySelectorAll("textarea, [contenteditable='true']"))
      .filter(isValidInput);
    if (!candidates.length) return null;
    candidates.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
    return candidates[0];
  }

  function findHeuristicContainer() {
    const candidates = Array.from(document.querySelectorAll("[role='log'], [aria-live], main, [role='main']"));
    for (const el of candidates) {
      if (el && el.innerText && el.innerText.length > 20) return el;
    }
    return document.body;
  }

  function findAddedTextNode(mutations) {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (!(node instanceof HTMLElement)) continue;
        if (node.classList && node.classList.contains("bdbm-injector-panel")) continue;
        if (node.innerText && node.innerText.trim().length > 0) {
          return node;
        }
        const child = node.querySelector && node.querySelector("*");
        if (child && child.innerText && child.innerText.trim().length > 0) {
          return child;
        }
      }
    }
    return null;
  }

  function startLearning() {
    let step = 1;
    showToast("Learning mode: click the message input.");
    const onClick = async (e) => {
      if (e.target && e.target.closest && e.target.closest(".bdbm-injector-panel")) return;
      if (step === 1) {
        const inputEl = e.target.closest("textarea, [contenteditable='true']") || e.target;
        const inputSelector = buildSelector(inputEl);
        if (!inputSelector) return;
        step = 2;
        const learned = STATE.learnedSelectors || {};
        const host = getHostKey();
        learned[host] = { ...(learned[host] || {}), inputSelector };
        await saveLearnedSelectors(learned);
        STATE.learnedSelectors = learned;
        showToast("Step 2/2: click the latest assistant message.");
        return;
      }
      if (step === 2) {
        const assistantEl = e.target.closest("div, article, p, span") || e.target;
        const assistantSelector = buildSelector(assistantEl);
        const containerEl = assistantEl ? assistantEl.parentElement : null;
        const containerSelector = buildSelector(containerEl);
        const learned = STATE.learnedSelectors || {};
        const host = getHostKey();
        learned[host] = {
          ...(learned[host] || {}),
          assistantSelector,
          containerSelector
        };
        await saveLearnedSelectors(learned);
        STATE.learnedSelectors = learned;
        showToast("Saved. Auto-detection improved.");
        setTimeout(hideToast, 1500);
        document.removeEventListener("click", onClick, true);
      }
    };
    document.addEventListener("click", onClick, true);
  }

  async function resetLearning() {
    const learned = STATE.learnedSelectors || {};
    const host = getHostKey();
    if (learned[host]) {
      delete learned[host];
      await saveLearnedSelectors(learned);
      STATE.learnedSelectors = learned;
      showToast("Learning reset for this site.");
      setTimeout(hideToast, 1500);
    }
  }

  function normalizeText(text) {
    return (text || "").replace(/\s+/g, " ").trim();
  }

  function containsControlArtifacts(text) {
    if (window.BdbmPromptBuilder && window.BdbmPromptBuilder.containsControlArtifacts) {
      return window.BdbmPromptBuilder.containsControlArtifacts(text || "");
    }
    return /<user_context|<\/user_context>|<System\s*-|\|STPAM\||\|MIDPAM\||\|ENDPAM\||\|MEMQUERY\||\|ENDQUERY\||\|TITLE\|/i.test(text || "");
  }

  function extractUserPrompt(text) {
    if (window.BdbmPromptBuilder && window.BdbmPromptBuilder.extractUserPrompt) {
      return window.BdbmPromptBuilder.extractUserPrompt(text || "");
    }
    return (text || "").trim();
  }



  function replaceMessageText(adapter, el, text) {
    if (!el) return false;
    // Safety net: never overwrite an input/composer element
    if (isComposerElement(el)) {
      debugIo("UI MASK (BLOCKED - composer element)", { element: elementBrief(el) });
      return false;
    }

    // React sites: use CSS-overlay instead of direct DOM mutation
    if (adapter.isReactSite) {
      let safeTarget = resolveReactUserMaskTarget(el);
      if (!safeTarget) {
        debugIo("UI MASK (BLOCKED - unsafe react target)", { element: elementBrief(el) });
        return false;
      }

      // Allow the adapter to specify the exact inner container to hide.
      // This prevents hiding the entire wrapper (which contains the bubble
      // background and action buttons) when applying the React safe overlay.
      if (adapter.getReactOverlayTarget) {
        safeTarget = adapter.getReactOverlayTarget(safeTarget) || safeTarget;
      }

      return applyReactSafeOverlay(safeTarget, text);
    }

    if (adapter.replaceMessageText) {
      adapter.replaceMessageText(el, text);
      const after = adapter.extractMessageText ? adapter.extractMessageText(el) : el.innerText;
      debugIo("UI MASK (replaceMessageText result)", {
        element: elementBrief(el),
        requestedText: (text || "").slice(0, 200),
        afterText: (after || "").slice(0, 200),
        matched: normalizeText(after) === normalizeText(text),
        isShadowDom: !!adapter.isShadowDom
      });
      if (normalizeText(after) === normalizeText(text)) return true;
      // For Shadow DOM sites: skip the destructive textContent/innerText fallback
      // entirely. Text-node surgery in adapter.replaceMessageText is correct;
      // a text extraction mismatch here is a shadow-DOM artifact, not a failure.
      if (adapter.isShadowDom) return true;
      // Non-Shadow DOM fallback: drill down to the deepest single-child element.
      try {
        let target = el;
        while (target.children && target.children.length === 1) {
          target = target.children[0];
        }
        target.textContent = text;
      } catch (_) {
        // Last resort — destructive but better than showing leaked prompt
        try { el.innerText = text; } catch (_2) { }
      }
      return normalizeText(el.innerText || "") === normalizeText(text);
    }
    // Fallback for sites without adapter.replaceMessageText
    // Same structure-preserving approach
    let target = el;
    while (target.children && target.children.length === 1) {
      target = target.children[0];
    }
    target.textContent = text;
    return normalizeText(el.innerText || "") === normalizeText(text);
  }

  /**
   * React-safe masking: hide all React-managed child nodes via CSS
   * and insert a clean overlay element that shows the original user text.
   * The overlay sits outside React's managed tree, so React reconciliation
   * is never triggered.
   */
  function applyReactSafeOverlay(el, cleanText) {
    if (!el) return false;
    ensureBdbmHideStyle();

    // Reuse existing overlay if present (don't re-create on every call).
    let overlay = el.querySelector(".bdbm-overlay-text");
    if (overlay) {
      overlay.textContent = cleanText;
    }

    // Hide all React-managed children. We MUST re-iterate every call (not
    // early-return) because Claude/React re-render the bubble on conversation
    // navigation (sidebar switching) and add fresh <p> children that carry the
    // leaked enriched prompt. Setting data-bdbm-react-hidden is idempotent —
    // safe to call repeatedly on the same node.
    for (let i = 0; i < el.childNodes.length; i++) {
      const child = el.childNodes[i];
      if (child === overlay) continue;
      if (child.nodeType === Node.ELEMENT_NODE) {
        if (!child.hasAttribute("data-bdbm-react-hidden")) {
          child.setAttribute("data-bdbm-react-hidden", "true");
        }
      } else if (child.nodeType === Node.TEXT_NODE && child.nodeValue && child.nodeValue.trim()) {
        // Wrap bare text nodes in a span so we can hide them
        const wrapper = document.createElement("span");
        wrapper.setAttribute("data-bdbm-react-hidden", "true");
        child.parentNode.insertBefore(wrapper, child);
        wrapper.appendChild(child);
      }
    }

    // Create overlay if not already present.
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.className = "bdbm-overlay-text";
      overlay.textContent = cleanText;
      el.appendChild(overlay);
    }

    return true;
  }

  /**
   * Inject a persistent <style> that hides elements marked with [data-bdbm-pam].
   * Called once; subsequent calls are no-ops.
   */
  function ensureBdbmHideStyle() {
    if (document.getElementById("bdbm-pam-hide-style")) return;
    try {
      const style = document.createElement("style");
      style.id = "bdbm-pam-hide-style";
      style.textContent =
        "[data-bdbm-pam='true']{display:none!important;visibility:hidden!important;}" +
        "[data-bdbm-react-hidden='true']{display:none!important;}" +
        ".bdbm-overlay-text{white-space:pre-wrap;unicode-bidi:plaintext;}";
      (document.head || document.documentElement).appendChild(style);
    } catch (_) { }
  }

  /**
   * React-safe PAM hiding: never modifies text node content (which would
   * trigger React's reconciler). Instead, marks elements containing PAM
   * blocks with data-bdbm-pam="true" and hides them via the injected stylesheet.
   *
   * Also handles Claude's markdown table rendering: Claude's parser treats
   * |STPAM|text|MIDPAM|text|ENDPAM| as a markdown table, so the pipe chars
   * are stripped and the tokens appear as bare table-cell text ("STPAM", etc.).
   */
  function hideReactPamBlocks(el) {
    if (!el) return false;
    ensureBdbmHideStyle();

    const PAM_PIPE_RE = /\|STPAM\|[\s\S]*?\|ENDPAM\|/gi;
    const TITLE_PIPE_RE = /\|TITLE\|[^\n]*/gi;
    const ORPHAN_PIPE_RE = /\|(?:STPAM|MIDPAM|ENDPAM|MEMQUERY|ENDQUERY|TITLE)\|/gi;

    function isPamContainer(elem) {
      const txt = (elem.textContent || "").trim();
      if (!txt || txt.length < 3) return false;

      // Standard pipe-notation: |STPAM|...|ENDPAM|
      if (/\|STPAM\|/.test(txt) && /\|ENDPAM\|/.test(txt)) {
        const rest = txt
          .replace(PAM_PIPE_RE, "")
          .replace(TITLE_PIPE_RE, "")
          .replace(ORPHAN_PIPE_RE, "")
          .trim();
        return rest.length < 40; // element is mostly PAM
      }

      // Claude table rendering: markdown converts |TOKEN|text|TOKEN| to <table>
      // → pipe chars are gone, only the token words remain as cell text
      if (/\bSTPAM\b/.test(txt) && /\bENDPAM\b/.test(txt) && /\bMIDPAM\b/.test(txt)) {
        if (elem.tagName === "TABLE" || elem.tagName === "TBODY" || elem.tagName === "TR") return true;
        if (/^\s*STPAM\b/.test(txt)) return true;
        return false;
      }

      // Standalone |TITLE| line (pipe-notation or table cell)
      if (/^\|TITLE\|/.test(txt)) return true;
      if (/^\s*TITLE\s/.test(txt) && txt.split("\n").length < 3 &&
        (/\bMIDPAM\b/.test(txt) || /\bSTPAM\b/.test(txt))) return true;

      return false;
    }

    let hidden = false;
    const hiddenSet = new Set();

    try {
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT, null);
      const descendants = [];
      let node = walker.nextNode();
      while (node) { descendants.push(node); node = walker.nextNode(); }

      for (const child of descendants) {
        if (child === el || isComposerElement(child)) continue;
        if (isUnsafeMaskTarget(child)) continue;
        // Skip if already covered by a hidden ancestor
        if ([...hiddenSet].some((h) => h !== child && h.contains(child))) continue;

        if (isPamContainer(child)) {
          // Prefer hiding at table level when inside a table (Claude case)
          let target = child;
          let p = child.parentElement;
          while (p && p !== el) {
            if ((p.tagName === "TABLE" || p.tagName === "TBODY" || p.tagName === "TR")
              && isPamContainer(p)) {
              target = p;
            }
            p = p.parentElement;
          }
          target.setAttribute("data-bdbm-pam", "true");
          hiddenSet.add(target);
          hidden = true;
        }
      }
    } catch (_) { }

    return hidden;
  }

  /**
   * Surgically remove PAM tokens and system artifacts from assistant response DOM.
   *
   * @param {HTMLElement} el         - The assistant message element
   * @param {object}  [parsedInfo]   - Parsed PAM info (userSummary, modelSummary, threadTitle)
   * @param {boolean} [conservative] - If true (React sites): use CSS-hiding only,
   *                                   never mutate text nodes (avoids React reconciler).
   *                                   If false (Shadow DOM): full surgical text removal.
   * Returns true if any cleaning was performed.
   */
  function surgicalRemovePamTokens(el, parsedInfo, conservative) {
    if (!el) return false;

    // React sites: use pure CSS-hiding approach — never touch text node content
    if (conservative) {
      return hideReactPamBlocks(el);
    }

    const PAM_BLOCK_RE = /\|STPAM\|[\s\S]*?\|ENDPAM\|/gi;
    const TITLE_RE = /\|TITLE\|\s*[^\n\r]*/gi;
    const ORPHAN_TOKEN_RE = /\|(?:STPAM|MIDPAM|ENDPAM|MEMQUERY|ENDQUERY|TITLE)\|/gi;
    const SYSTEM_BLOCK_RE = /<System\s*-\s*[^>\n]*>[\s\S]*?<\/System\s*-\s*[^>]*>/gi;
    const SYSTEM_OPEN_RE = /<System\s*-[\s\S]*?>/gi;
    const SYSTEM_CLOSE_RE = /<\/System\s*-\s*[^>]*>/gi;
    const USER_CTX_BLOCK_RE = /<user_context>[\s\S]*?<\/user_context>/gi;
    const USER_CTX_OPEN_RE = /<\/?(?:user_context|current_time|relevant_memories|response_format)[^>]*>/gi;

    let cleaned = false;

    // Helper: safely remove a text node and optionally its now-empty parent
    function removeTextNode(node) {
      const parent = node.parentElement;
      // Clear the text node content (always safe — doesn't remove React elements)
      node.nodeValue = "";
      // In conservative mode, stop here. React manages its own element lifecycle.
      if (conservative) return;
      // In non-conservative mode, also remove the element if now truly empty
      if (parent && parent !== el) {
        const parentText = (parent.textContent || "").trim();
        if (!parentText && parent.childElementCount === 0) {
          try { parent.remove(); } catch (_) { /* skip */ }
        }
      }
    }

    // Helper: check if an element is entirely a PAM block (whole content is PAM)
    function isEntirePamBlock(elem) {
      const txt = (elem.textContent || "").trim();
      if (!txt) return false;
      // Whole content is wrapped in |STPAM|...|ENDPAM| or is a |TITLE| line
      return /^\s*\|STPAM\|[\s\S]*\|ENDPAM\|\s*$/.test(txt) ||
        /^\s*\|TITLE\|/.test(txt);
    }

    // Collect all search roots (including shadow roots)
    const searchRoots = [el];
    if (el.shadowRoot) searchRoots.push(el.shadowRoot);

    for (const root of searchRoots) {
      const doc = root.ownerDocument || document;
      if (typeof doc.createTreeWalker !== "function") continue;

      let walker;
      try {
        walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
      } catch (_) { continue; }

      // Collect all text nodes first (walking while mutating is unsafe)
      const textNodes = [];
      let tn = walker.nextNode();
      while (tn) {
        textNodes.push(tn);
        tn = walker.nextNode();
      }

      // ── Phase 1: Single-node regex removal (fast path) ─────────────
      for (let i = textNodes.length - 1; i >= 0; i--) {
        const node = textNodes[i];
        if (node.parentElement && isUnsafeMaskTarget(node.parentElement)) continue;
        const value = node.nodeValue || "";

        let newValue = value;
        newValue = newValue.replace(PAM_BLOCK_RE, "");
        newValue = newValue.replace(TITLE_RE, "");
        newValue = newValue.replace(ORPHAN_TOKEN_RE, "");
        newValue = newValue.replace(SYSTEM_BLOCK_RE, "");
        newValue = newValue.replace(SYSTEM_OPEN_RE, "");
        newValue = newValue.replace(SYSTEM_CLOSE_RE, "");
        newValue = newValue.replace(USER_CTX_BLOCK_RE, "");
        newValue = newValue.replace(USER_CTX_OPEN_RE, "");

        if (newValue !== value) {
          cleaned = true;
          if (!newValue.trim()) {
            removeTextNode(node);
            textNodes.splice(i, 1);
          } else {
            node.nodeValue = newValue;
          }
        }
      }

      // ── Phase 2: Cross-node PAM block removal ──────────────────────
      // Handles |STPAM| and |ENDPAM| in different text nodes.
      let inPamBlock = false;
      const nodesToClear = [];

      for (let i = 0; i < textNodes.length; i++) {
        const node = textNodes[i];
        if (!node.isConnected) continue;
        const value = node.nodeValue || "";

        if (!inPamBlock) {
          const stpamIdx = value.indexOf("|STPAM|");
          if (stpamIdx !== -1) {
            inPamBlock = true;
            cleaned = true;
            const endpamIdx = value.indexOf("|ENDPAM|");
            if (endpamIdx !== -1) {
              const before = value.substring(0, stpamIdx);
              const after = value.substring(endpamIdx + "|ENDPAM|".length);
              node.nodeValue = before + after;
              if (!(before + after).trim()) nodesToClear.push(node);
              inPamBlock = false;
            } else {
              const before = value.substring(0, stpamIdx);
              if (before.trim()) {
                node.nodeValue = before;
              } else {
                nodesToClear.push(node);
              }
            }
          }
        } else {
          const endpamIdx = value.indexOf("|ENDPAM|");
          if (endpamIdx !== -1) {
            const after = value.substring(endpamIdx + "|ENDPAM|".length);
            if (after.trim()) {
              node.nodeValue = after;
            } else {
              nodesToClear.push(node);
            }
            inPamBlock = false;
          } else {
            nodesToClear.push(node);
          }
        }
      }

      // ── Phase 3: Cross-node |TITLE| removal ────────────────────────
      for (let i = 0; i < textNodes.length; i++) {
        const node = textNodes[i];
        if (!node.isConnected) continue;
        if (node.parentElement && isUnsafeMaskTarget(node.parentElement)) continue;
        const value = node.nodeValue || "";
        const titleIdx = value.indexOf("|TITLE|");
        if (titleIdx !== -1) {
          cleaned = true;
          const before = value.substring(0, titleIdx);
          if (before.trim()) {
            node.nodeValue = before;
          } else {
            nodesToClear.push(node);
          }
          for (let j = i + 1; j < textNodes.length; j++) {
            const nextNode = textNodes[j];
            if (!nextNode.isConnected) continue;
            const nextVal = nextNode.nodeValue || "";
            const nlIdx = nextVal.indexOf("\n");
            if (nlIdx !== -1) {
              nextNode.nodeValue = nextVal.substring(nlIdx + 1);
              break;
            }
            nodesToClear.push(nextNode);
          }
        }
      }

      // ── Phase 4: Clear/remove collected nodes ──────────────────────
      for (const node of nodesToClear) {
        if (!node.isConnected) continue;
        removeTextNode(node);
      }

      // ── Phase 5: Element-level hiding for residual PAM blocks ──────
      // If an element's ENTIRE content is PAM text, hide it visually.
      // This is a last resort for cases where text nodes couldn't be
      // fully matched (e.g. rendered markdown splitting the block).
      if (conservative) {
        // Walk child elements of the assistant message looking for
        // elements whose full text content is a PAM/summary block
        const elWalker = doc.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, null);
        const elemCandidates = [];
        let ce = elWalker.nextNode();
        while (ce) { elemCandidates.push(ce); ce = elWalker.nextNode(); }

        for (const elem of elemCandidates) {
          if (elem === root || elem === el) continue;
          if (!isComposerElement(elem) && isEntirePamBlock(elem)) {
            try {
              elem.style.display = "none";
              elem.setAttribute("data-bdbm-hidden", "pam");
              cleaned = true;
            } catch (_) { /* skip */ }
          }
        }
      }

      // ── Phase 6: Remove remaining summary text fragments ───────────
      if (parsedInfo) {
        const summaryFragments = [];
        if (parsedInfo.userSummary) summaryFragments.push(parsedInfo.userSummary.trim());
        if (parsedInfo.modelSummary) summaryFragments.push(parsedInfo.modelSummary.trim());
        if (parsedInfo.threadTitle) summaryFragments.push(parsedInfo.threadTitle.trim());

        if (summaryFragments.length > 0) {
          let walker2;
          try {
            walker2 = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
          } catch (_) { continue; }
          const remainingNodes = [];
          let rn = walker2.nextNode();
          while (rn) { remainingNodes.push(rn); rn = walker2.nextNode(); }

          for (let i = remainingNodes.length - 1; i >= 0; i--) {
            const node = remainingNodes[i];
            if (!node.isConnected) continue;
            // ── biomem overlay guard ──────────────────────────────────────
            // Skip text nodes inside .bdbm-overlay-text elements — these
            // contain the user's original (unmasked) text.  userSummary is
            // a semantic subset of that text, so indexOf() would match and
            // incorrectly strip part of the visible user query (e.g. leaving
            // only ":)" when the full message was "test masking in ChatGPT :)").
            let _ancestor = node.parentElement;
            let _inOverlay = false;
            while (_ancestor && _ancestor !== root) {
              if (_ancestor.classList && _ancestor.classList.contains("bdbm-overlay-text")) {
                _inOverlay = true;
                break;
              }
              _ancestor = _ancestor.parentElement;
            }
            if (_inOverlay) continue;

            let value = node.nodeValue || "";
            let changed = false;

            for (const fragment of summaryFragments) {
              if (!fragment || fragment.length < 5) continue;
              const idx = value.indexOf(fragment);
              if (idx !== -1) {
                value = value.substring(0, idx) + value.substring(idx + fragment.length);
                changed = true;
                cleaned = true;
              }
            }

            if (changed) {
              if (!value.trim()) {
                removeTextNode(node);
              } else {
                node.nodeValue = value;
              }
            }
          }
        }
      }
    }

    if (cleaned) {
      debugIo("UI MASK (surgical result)", {
        element: elementBrief(el),
        afterText: ((el.textContent || "")).slice(0, 300),
        afterTextLen: (el.textContent || "").trim().length,
        hadParsedInfo: !!parsedInfo,
        conservative: !!conservative
      });
    }

    return cleaned;
  }

  function getUserMessageCount(adapter) {
    if (!adapter || !adapter.getUserMessageElements) return 0;
    const els = adapter.getUserMessageElements();
    return Array.isArray(els) ? els.length : 0;
  }

  function enqueuePendingUser(adapter, item) {
    const currentCount = getUserMessageCount(adapter);
    const lastExpected = STATE.pendingUserQueue.length
      ? (STATE.pendingUserQueue[STATE.pendingUserQueue.length - 1].expectedUserCount || 0)
      : 0;
    const expectedUserCount = Math.max(currentCount, lastExpected) + 1;
    const promptTimestamp = item.promptTimestamp || extractPromptTimestamp(item.enrichedText || "");
    const queued = {
      ...item,
      promptTimestamp,
      expectedUserCount,
      queuedAt: Date.now(),
      cancelled: false
    };
    STATE.pendingUserQueue.push(queued);
    debugIo("UI MASK (enqueuePendingUser)", {
      originalText: queued.originalText,
      originalTextLen: (queued.originalText || "").length,
      enrichedTextLen: (queued.enrichedText || "").length,
      promptTimestamp: queued.promptTimestamp,
      expectedUserCount: queued.expectedUserCount,
      hidden: !!queued.hidden
    });
    return queued;
  }

  function removePendingUserItem(item, cancelSweeps) {
    if (!item) return;
    if (cancelSweeps) item.cancelled = true;
    STATE.pendingUserQueue = STATE.pendingUserQueue.filter((candidate) => candidate !== item);
  }

  function isPendingUserSweepCurrent(item) {
    return !!item && !item.cancelled && STATE.pendingUserQueue.includes(item);
  }

  function needsUserMask(current, item) {
    if (!current) return false;
    const currentNorm = normalizeText(current);
    const originalNorm = normalizeText(item.originalText);
    const enrichedNorm = normalizeText(item.enrichedText);
    return containsControlArtifacts(current) ||
      (enrichedNorm && currentNorm === enrichedNorm) ||
      current.length > item.originalText.length + 20 ||
      currentNorm !== originalNorm;
  }

  function getPromptSnippet(text) {
    const norm = normalizeText(text || "");
    if (!norm) return "";
    return norm.slice(0, Math.min(80, norm.length));
  }

  function extractPromptTimestamp(text) {
    const match = (text || "").match(/(?:<System\s*-\s*Current Date and Time>|<current_time>)\s*([\d]{4}-[\d]{2}-[\d]{2}\s+[\d]{2}:[\d]{2})/i);
    return match ? match[1] : "";
  }

  // DIAGNOSTIC (temporary): when the mask target cannot be resolved or the
  // replacement is blocked, dump WHY — is the last user bubble found at all,
  // is it misclassified as a composer, does the React overlay target resolve?
  let lastMaskDiagAt = 0;
  function pendingMaskDiag(adapter, item, where, throttleMs) {
    if (!DEBUG_PROMPT_IO) return;
    const now = Date.now();
    if (throttleMs && now - lastMaskDiagAt < throttleMs) return;
    lastMaskDiagAt = now;
    try {
      const users = adapter.getUserMessageElements ? (adapter.getUserMessageElements() || []) : [];
      const last = users.length ? users[users.length - 1] : null;
      const lastText = last
        ? (adapter.extractMessageText ? adapter.extractMessageText(last) : last.innerText)
        : "";
      debugIo(`UI MASK DIAG (${where})`, {
        userElCount: users.length,
        expectedUserCount: item.expectedUserCount,
        lastUserEl: elementBrief(last),
        lastUserIsComposer: last ? isComposerElement(last) : "(no element)",
        lastUserReactMaskTarget: last ? elementBrief(resolveReactUserMaskTarget(last)) : "(no element)",
        lastUserHasArtifacts: containsControlArtifacts(lastText),
        likelyPendingLeak: last ? isLikelyPendingLeakText(lastText, item) : "(no element)",
        lastUserTextPreview: (lastText || "").slice(0, 160)
      });
    } catch (_) {
    }
  }

  function schedulePendingUserSweep(adapter, item) {
    if (!item || item.hidden) return;
    const delays = [150, 450, 900, 1800, 3200];
    delays.forEach((delayMs) => {
      setTimeout(() => {
        if (!isPendingUserSweepCurrent(item)) return;
        const target = resolvePendingUserTarget(adapter, item, []);
        const globalSanitized = sanitizeAllVisibleLeakNodes(adapter, item.originalText, item.promptTimestamp || "");
        if (!target) {
          pendingMaskDiag(adapter, item, `sweep no-target @${delayMs}ms`);
          const forced = forceMaskVisibleLeaks(adapter, item);
          if (forced > 0 || globalSanitized > 0) {
            debugIo("UI MASK (sweep-force)", {
              replacedCount: forced,
              globalSanitizedCount: globalSanitized,
              targetElement: elementBrief(target),
              promptTimestamp: item.promptTimestamp || "",
              expectedUserCount: item.expectedUserCount,
              sessionId: item.sessionId
            });
            return;
          }
          sanitizeLatestUserLeak(adapter);
          if (delayMs === delays[delays.length - 1]) {
            debugIo("UI MASK (sweep-miss)", {
              promptTimestamp: item.promptTimestamp || "",
              expectedUserCount: item.expectedUserCount,
              sessionId: item.sessionId,
              originalUserPrompt: item.originalText
            });
          }
          return;
        }
        const current = adapter.extractMessageText ? adapter.extractMessageText(target) : target.innerText;
        if (!needsUserMask(current, item)) return;
        const didReplace = replaceMessageText(adapter, target, item.originalText);
        const forced = forceMaskVisibleLeaks(adapter, item);
        if (!didReplace && forced === 0) {
          pendingMaskDiag(adapter, item, `sweep replace BLOCKED @${delayMs}ms (target=${elementBrief(target)})`);
          return;
        }
        debugIo("UI MASK (sweep)", {
          uiTextBefore: current,
          uiTextAfter: item.originalText,
          replacedCount: forced,
          globalSanitizedCount: globalSanitized,
          targetElement: elementBrief(target),
          promptTimestamp: item.promptTimestamp || "",
          sentEnrichedPrompt: item.enrichedText,
          expectedUserCount: item.expectedUserCount,
          sessionId: item.sessionId
        });
      }, delayMs);
    });
  }
  function isUnsafeMaskTarget(el) {
    if (!el || !el.isConnected) return true;

    // Ask adapter first
    if (STATE.adapter && STATE.adapter.isSafeToMask) {
      const safe = STATE.adapter.isSafeToMask(el);
      if (typeof safe === "boolean" && !safe) return true;
    }

    // Generic sidebar/nav exclusion
    let current = el;
    while (current && current !== document.body && current !== document.documentElement) {
      const tag = (current.tagName || "").toUpperCase();
      if (tag === "NAV" || tag === "ASIDE") return true;
      if (current.getAttribute) {
        const role = current.getAttribute("role");
        if (role === "navigation" || role === "complementary" || role === "banner") return true;
      }
      // Claude specific sidebar check as fallback
      const className = typeof current.className === "string" ? current.className : "";
      if (className && (className.includes("Sidebar") || className.includes("Navigation"))) return true;

      current = current.parentElement;
    }
    return false;
  }

  function findLeakCandidateInMutations(adapter, item, mutations) {
    if (!mutations || !mutations.length) return null;
    const maxLen = getMaskMaxLen(item);
    const checked = new Set();
    const queue = [];

    for (const m of mutations) {
      if (m.target && m.target instanceof HTMLElement) {
        queue.push(m.target);
      } else if (m.target && m.target.parentElement) {
        queue.push(m.target.parentElement);
      }
      if (m.addedNodes && m.addedNodes.length) {
        for (const node of m.addedNodes) {
          if (node instanceof HTMLElement) queue.push(node);
        }
      }
    }

    while (queue.length > 0) {
      const node = queue.shift();
      if (!node || checked.has(node)) continue;
      checked.add(node);
      if (isUnsafeMaskTarget(node)) continue;

      const direct = findLeakCandidateInNode(adapter, node, item, maxLen);
      if (direct) return direct;

      if (isElementVisible(node)) {
        const text = adapter.extractMessageText ? adapter.extractMessageText(node) : node.innerText;
        if (text && !isTooBroadCandidate(node, text, item) && isLikelyPendingLeakText(text, item)) {
          return node;
        }
      }

      if (node.querySelectorAll) {
        const children = node.querySelectorAll("*");
        for (const child of children) {
          if (!checked.has(child)) queue.push(child);
        }
      }
    }

    return null;
  }

  function findLeakCandidateInNode(adapter, root, item, maxLen) {
    if (!root || !root.ownerDocument || typeof root.ownerDocument.createTreeWalker !== "function") return null;
    const doc = root.ownerDocument;
    const markers = /<user_context>|<current_time>|<relevant_memories>|<response_format>|<System\s*-\s*Current Date and Time>|<System\s*-\s*associated memory context|Summary of (?:my query|the USER'S QUERY)|Format:\s*\|?STPAM\|?/i;
    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    let best = null;
    let textNode = walker.nextNode();
    while (textNode) {
      const value = textNode.nodeValue || "";
      if (markers.test(value)) {
        let el = textNode.parentElement;
        while (el && el !== doc.body) {
          if (!isElementVisible(el)) {
            el = el.parentElement;
            continue;
          }
          const text = adapter.extractMessageText ? adapter.extractMessageText(el) : el.innerText;
          if (!text || text.length < (item.originalText || "").length + 10) {
            el = el.parentElement;
            continue;
          }
          if (text.length > maxLen) break;
          if (isTooBroadCandidate(el, text, item)) break;
          if (isLikelyPendingLeakText(text, item)) {
            const depth = getElementDepth(el);
            if (!best || depth > best.depth || (depth === best.depth && text.length < best.len)) {
              best = { el, depth, len: text.length };
            }
            break;
          }
          el = el.parentElement;
        }
      }
      textNode = walker.nextNode();
    }
    return best ? best.el : null;
  }

  function resolvePendingUserTarget(adapter, item, mutations) {
    const mutationCandidate = findLeakCandidateInMutations(adapter, item, mutations);
    if (mutationCandidate && !isComposerElement(mutationCandidate)) {
      if (!adapter.isReactSite || resolveReactUserMaskTarget(mutationCandidate)) {
        return mutationCandidate;
      }
    }

    if (adapter.getUserMessageElements) {
      const users = adapter.getUserMessageElements() || [];
      if (users.length >= item.expectedUserCount) {
        const candidate = users[item.expectedUserCount - 1] || users[users.length - 1] || null;
        if (candidate && !isComposerElement(candidate)) {
          const text = adapter.extractMessageText ? adapter.extractMessageText(candidate) : candidate.innerText;
          if (isLikelyPendingLeakText(text, item)) {
            return candidate;
          }
        }
      }
      const enrichedNorm = normalizeText(item.enrichedText);
      const originalNorm = normalizeText(item.originalText);
      for (let i = users.length - 1; i >= 0; i -= 1) {
        const candidate = users[i];
        if (isComposerElement(candidate)) continue;
        const text = adapter.extractMessageText ? adapter.extractMessageText(candidate) : candidate.innerText;
        if (!text) continue;
        const normalized = normalizeText(text);
        const looksLikeTarget = isLikelyPendingLeakText(text, item) ||
          (enrichedNorm && normalized === enrichedNorm) ||
          (originalNorm && normalized === originalNorm);
        if (looksLikeTarget) {
          return candidate;
        }
      }
    }

    const fallback = findAddedTextNode(mutations);
    if (fallback && !isComposerElement(fallback)) {
      const text = adapter.extractMessageText ? adapter.extractMessageText(fallback) : fallback.innerText;
      if (text && isLikelyPendingLeakText(text, item) &&
        (!adapter.isReactSite || resolveReactUserMaskTarget(fallback))) {
        return fallback;
      }
    }

    const containerResult = findLeakCandidateInContainer(adapter, item);
    if (containerResult && isComposerElement(containerResult)) return null;
    if (containerResult && adapter.isReactSite && !resolveReactUserMaskTarget(containerResult)) return null;
    return containerResult;
  }

  function getElementDepth(el) {
    let depth = 0;
    let node = el;
    while (node && node !== document.body) {
      depth += 1;
      node = node.parentElement;
    }
    return depth;
  }

  function getMaskMaxLen(item) {
    const originalLen = (item && item.originalText ? item.originalText.length : 0);
    const enrichedLen = (item && item.enrichedText ? item.enrichedText.length : 0);
    const byOriginal = Math.max(3000, originalLen * 20);
    const byEnriched = Math.max(25000, Math.floor(enrichedLen * 1.4));
    return Math.max(byOriginal, byEnriched);
  }

  function elementBrief(el) {
    if (!el) return null;
    const tag = (el.tagName || "").toLowerCase();
    const id = el.id ? `#${el.id}` : "";
    const cls = el.className && typeof el.className === "string"
      ? "." + el.className.trim().split(/\s+/).slice(0, 3).join(".")
      : "";
    const role = el.getAttribute ? (el.getAttribute("role") || "") : "";
    const testid = el.getAttribute ? (el.getAttribute("data-testid") || "") : "";
    return `${tag}${id}${cls}${role ? `[role=${role}]` : ""}${testid ? `[data-testid=${testid}]` : ""}`;
  }

  /**
   * Returns true if the element is (or is an ancestor of) a chat input / composer.
   * Used to prevent the masking system from ever overwriting the input field.
   */
  function isComposerElement(el) {
    if (!el) return false;
    const tag = (el.tagName || "").toUpperCase();
    // Direct input types
    if (tag === "TEXTAREA" || tag === "INPUT" || tag === "FORM") return true;
    // ContentEditable
    if (el.isContentEditable || el.getAttribute("contenteditable") === "true") return true;
    // Role hints
    const role = (el.getAttribute && el.getAttribute("role")) || "";
    if (role === "textbox" || role === "combobox" || role === "searchbox") return true;
    // Common composer container patterns
    const testId = (el.getAttribute && el.getAttribute("data-testid")) || "";
    if (/composer|input|editor|prompt-textarea/i.test(testId)) return true;
    const cls = (typeof el.className === "string" ? el.className : "").toLowerCase();
    if (/composer|prompt-input|chat-input|message-input/.test(cls)) return true;
    // Check if a textarea/contenteditable is INSIDE this element
    if (el.querySelector) {
      try {
        if (el.querySelector("textarea, [contenteditable='true'], [role='textbox']")) return true;
      } catch (_) { /* skip */ }
    }
    // Check if we're INSIDE a composer (an ancestor is a form/composer)
    let parent = el.parentElement;
    let depth = 0;
    while (parent && depth < 6) {
      const ptag = (parent.tagName || "").toUpperCase();
      if (ptag === "FORM") return true;
      if (parent.isContentEditable) return true;
      const pRole = (parent.getAttribute && parent.getAttribute("role")) || "";
      if (pRole === "textbox" || pRole === "combobox") return true;
      const pTestId = (parent.getAttribute && parent.getAttribute("data-testid")) || "";
      if (/composer|prompt-textarea|chat-input/i.test(pTestId)) return true;
      parent = parent.parentElement;
      depth++;
    }
    return false;
  }

  /**
   * Returns true if the element is a structural / navigation / container
   * that the shadow mask system should NEVER touch. Modifying these elements
   * destroys the Gemini UI layout (sidebar) or overwrites model responses
   * (conversation containers that hold both user message AND model reply).
   */
  function isShadowMaskUnsafeTarget(el) {
    if (!el) return true;
    const tag = (el.tagName || "").toLowerCase();
    const cls = (typeof el.className === "string" ? el.className : "").toLowerCase();
    const id = (el.id || "").toLowerCase();

    // Top-level structural tags
    if (tag === "body" || tag === "html" || tag === "main" || tag === "nav" || tag === "header" || tag === "footer") return true;

    // Gemini sidebar / navigation elements
    if (/conversations-list|sidenav|side-nav|sidebar|navigation|nav-drawer/.test(tag)) return true;
    if (/conversations-list|sidenav|side-nav|sidebar|navigation|nav-drawer/.test(cls)) return true;
    if (/conversations-list|sidenav|side-nav|sidebar|navigation/.test(id)) return true;

    // Conversation containers that hold BOTH user query and model response
    // Replacing text on these destroys the model output
    if (/conversation-container|pending-request|chat-window|chat-scroll/.test(tag)) return true;
    if (/conversation-container|pending-request|chat-window|chat-scroll/.test(cls)) return true;

    // Model response elements — the AI response may legitimately reference
    // system tokens or PAM instructions when discussing the plugin itself.
    // These must never be masked.
    if (/message-content|model-response|response-container|markdown-main/.test(tag)) return true;
    if (/message-content|model-response|response-container/.test(cls)) return true;
    if (id && /^message-content/.test(id)) return true;

    // Elements with role="main", role="navigation", role="complementary"
    const role = (el.getAttribute && el.getAttribute("role")) || "";
    if (role === "main" || role === "navigation" || role === "complementary" || role === "banner") return true;

    // Elements with too many direct children are likely containers, not message bubbles
    if (el.childElementCount && el.childElementCount > 50) return true;

    // Quick heuristic: if the element's text contains BOTH the response
    // marker AND control artifacts, it's a conversation container, not a user bubble
    const text = el.innerText || el.textContent || "";
    if (text.length > 5000) return true; // Way too long for a single user message

    return false;
  }

  function isTooBroadCandidate(el, text, item) {
    if (!el) return true;
    const tag = (el.tagName || "").toUpperCase();
    if (tag === "BODY" || tag === "HTML" || tag === "MAIN") return true;
    const role = (el.getAttribute && el.getAttribute("role")) || "";
    if (role === "main") return true;
    // Never touch input fields, textareas, forms, or composer containers
    if (isComposerElement(el)) return true;
    if (el.childElementCount && el.childElementCount > 120) return true;
    const maxLen = getMaskMaxLen(item);
    if ((text || "").length > maxLen) return true;
    return false;
  }

  function isElementVisible(el) {
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

  function collectSearchRoots(base) {
    const roots = [];
    const visited = new Set();
    const start = base || document.body || document.documentElement;
    if (!start) return roots;

    function walk(root) {
      if (!root || visited.has(root)) return;
      visited.add(root);
      roots.push(root);
      if (!root.querySelectorAll) return;
      const elements = root.querySelectorAll("*");
      for (const el of elements) {
        if (el && el.shadowRoot) {
          walk(el.shadowRoot);
        }
      }
    }

    walk(start);
    return roots;
  }

  // Site classification (isReactSite, isShadowDom) is now declared per-adapter
  // in each site-*.js file. This eliminates all cross-site branching from this
  // file — a fix for one site cannot accidentally affect another.
  const USER_MESSAGE_SELECTORS = [
    "[data-message-author-role='user']",
    "[data-author='user']",
    "[data-testid*='user-message']",
    "[data-testid*='user-query']",
    "[data-testid*='user-turn']",
    "div.user-message",
    "[class*='user-message']"
  ];
  const ASSISTANT_MESSAGE_SELECTORS = [
    "[data-message-author-role='assistant']",
    "[data-author='assistant']",
    "[data-testid*='assistant']",
    "[data-testid*='model']",
    "div.assistant-message",
    "[class*='assistant-message']",
    "div.markdown"
  ];

  function getRoleSelectors(role) {
    return role === "assistant" ? ASSISTANT_MESSAGE_SELECTORS : USER_MESSAGE_SELECTORS;
  }

  function hasMessageRoleMarker(el, role) {
    if (!el || !el.getAttribute) return false;
    const attrRole = (el.getAttribute("data-message-author-role") || "").toLowerCase();
    const author = (el.getAttribute("data-author") || "").toLowerCase();
    const testId = (el.getAttribute("data-testid") || "").toLowerCase();
    const cls = typeof el.className === "string" ? el.className.toLowerCase() : "";
    if (role === "assistant") {
      return attrRole === "assistant" ||
        author === "assistant" ||
        /assistant|model/.test(testId) ||
        /assistant-message|model-response|model-turn/.test(cls);
    }
    return attrRole === "user" ||
      author === "user" ||
      /(^|[-_])user([-_]|$)/.test(testId) ||
      /user-message|user-turn|human-turn|query/.test(cls);
  }

  function findClosestRoleElement(el, role) {
    let node = el;
    while (node && node !== document.body) {
      if (hasMessageRoleMarker(node, role)) return node;
      node = node.parentElement;
    }
    return null;
  }

  function findDeepestRoleElement(root, role) {
    if (!root) return null;
    const selectors = getRoleSelectors(role);
    const candidates = [];
    const seen = new Set();

    if (hasMessageRoleMarker(root, role)) {
      seen.add(root);
      candidates.push(root);
    }

    if (root.querySelectorAll) {
      for (const selector of selectors) {
        let found = [];
        try {
          found = Array.from(root.querySelectorAll(selector));
        } catch (_) {
          found = [];
        }
        for (const el of found) {
          if (!seen.has(el)) {
            seen.add(el);
            candidates.push(el);
          }
        }
      }
    }

    if (!candidates.length) return null;
    candidates.sort((a, b) => {
      const depthDiff = getElementDepth(b) - getElementDepth(a);
      if (depthDiff !== 0) return depthDiff;
      return a.childElementCount - b.childElementCount;
    });
    return candidates[0];
  }

  function containsRoleElementExcludingSelf(root, role) {
    if (!root || !root.querySelector) return false;
    const selectors = getRoleSelectors(role);
    for (const selector of selectors) {
      try {
        const found = root.querySelector(selector);
        if (found && found !== root) return true;
      } catch (_) {
        // ignore invalid selector usage on dynamic DOMs
      }
    }
    return false;
  }

  function resolveReactUserMaskTarget(el) {
    if (!el || isComposerElement(el)) return null;

    const closestUser = findClosestRoleElement(el, "user");
    if (closestUser && !containsRoleElementExcludingSelf(closestUser, "assistant")) {
      return closestUser;
    }

    const nestedUser = findDeepestRoleElement(el, "user");
    if (!nestedUser) return null;
    if (hasMessageRoleMarker(nestedUser, "assistant")) return null;
    if (findClosestRoleElement(nestedUser.parentElement, "assistant")) return null;
    if (containsRoleElementExcludingSelf(nestedUser, "assistant")) return null;
    return nestedUser;
  }

  function queryMaskNodes(adapter, selector) {
    const container = adapter.getMessageContainer ? adapter.getMessageContainer() : null;
    const base = container || document.body || document.documentElement;
    if (!adapter.isShadowDom) {
      return base && base.querySelectorAll ? Array.from(base.querySelectorAll(selector)) : [];
    }

    const roots = collectSearchRoots(base);
    const out = [];
    const seen = new Set();
    for (const root of roots) {
      if (!root.querySelectorAll) continue;
      try {
        const found = root.querySelectorAll(selector);
        for (const el of found) {
          if (!seen.has(el)) {
            seen.add(el);
            out.push(el);
          }
        }
      } catch (_) { /* selector may be invalid in some shadow roots */ }
    }
    return out;
  }

  function isLikelyPendingLeakText(text, item) {
    if (!text) return false;
    const normalized = normalizeText(text);
    const originalNorm = normalizeText(item.originalText);
    const enrichedNorm = normalizeText(item.enrichedText);
    const promptTimestamp = item.promptTimestamp || "";
    const hasPromptTimestamp = promptTimestamp ? normalized.includes(promptTimestamp) : false;
    const promptSnippet = getPromptSnippet(item.originalText);
    const hasPromptSnippet = promptSnippet ? normalized.includes(promptSnippet) : true;
    if (containsControlArtifacts(text) || hasStrongSystemLeak(text)) {
      if (promptTimestamp) {
        return hasPromptTimestamp || hasPromptSnippet;
      }
      return hasPromptSnippet;
    }
    if (enrichedNorm && normalized === enrichedNorm) return true;
    if (enrichedNorm && enrichedNorm.length > 50 && normalized.includes(enrichedNorm.slice(0, 120))) return true;
    if (originalNorm && normalized.startsWith(originalNorm) && normalized.length > originalNorm.length + 20) return true;
    if (originalNorm && normalized === originalNorm) return true;
    return false;
  }

  function findLeakCandidateInContainer(adapter, item) {
    const targetLen = Math.max((item.originalText || "").length, (item.enrichedText || "").length, 1);
    const maxLen = getMaskMaxLen(item);
    const nodes = queryMaskNodes(
      adapter,
      "[data-message-author-role='user'], [data-author='user'], [data-testid*='user'], article, [role='listitem'], *"
    );

    let best = null;
    for (const el of nodes) {
      if (!isElementVisible(el)) continue;
      const text = adapter.extractMessageText ? adapter.extractMessageText(el) : el.innerText;
      if (!text || text.length < 20) continue;
      if (text.length > maxLen) continue;
      if (isTooBroadCandidate(el, text, item)) continue;
      if (!isLikelyPendingLeakText(text, item)) continue;

      const delta = Math.abs(text.length - targetLen);
      const depth = getElementDepth(el);
      if (!best || delta < best.delta || (delta === best.delta && depth > best.depth)) {
        best = { el, delta, depth };
      }
    }

    return best ? best.el : null;
  }

  function hasStrongSystemLeak(text) {
    if (!text) return false;
    return /<user_context>|<current_time>|<relevant_memories>|<response_format>|<System\s*-\s*(Current Date and Time|associated memory context|Additional instruction|Deep Recall|New Conversation thread)|Summary of (?:my query|the USER'S QUERY)|Summary of (?:your response|YOUR RESPONSE)|Format:\s*\|?STPAM\|?/i.test(text);
  }

  function shouldIgnoreAssistantCandidate(text, pendingStore = STATE.pendingStore) {
    if (!text) return true;

    const rawText = String(text);
    const norm = normalizeText(rawText);
    const userNorm = normalizeText(pendingStore
      ? (pendingStore.userText || "")
      : (STATE.lastTurn ? (STATE.lastTurn.userText || "") : ""));
    const sentPromptNorm = normalizeText(pendingStore ? (pendingStore.sentPromptText || "") : "");
    // Provenance beats token shape: a verbatim user echo remains a user echo
    // even when the user happened to type strings that look like PAM markers.
    if (userNorm && norm === userNorm) return true;
    if (sentPromptNorm && norm === sentPromptNorm) return true;
    if (/^(?:unknown|n\/?a|not sure|i (?:do not|don't) know|no (?:information|data)(?: available)?)[.!?]*$/i.test(rawText.trim())) {
      return true;
    }

    // If the text contains PAM tokens (|STPAM|...|ENDPAM|), this is a
    // legitimate model response with memory summaries — NEVER ignore it.
    // This is critical: hasStrongSystemLeak() matches "Summary of the
    // USER'S QUERY" which also appears in the system instruction echo AND
    // in the model's PAM output.  Without this check, all responses with
    // PAM tokens were being silently dropped, preventing memory storage.
    if (/\|STPAM\|[\s\S]*?\|ENDPAM\|/i.test(rawText)) return false;

    // Provider chrome, login walls, and transport failures are not assistant
    // turns. Match standalone control/page-shell shapes rather than keywords,
    // so explanatory prose containing the same words remains a valid answer.
    const lines = rawText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const compact = lines.join(" ");
    const diagnosticErrorCluster = lines.length > 0 && lines.length <= 5 &&
      lines.some((line) => /^Unable to connect$/i.test(line)) &&
      lines.every((line) => line.length <= 180 && (
        /^ChatGPT said:$/i.test(line) ||
        /^Unable to connect$/i.test(line) ||
        /^Retry$/i.test(line) ||
        /^Request ID:\s*[A-Za-z0-9._:-]{1,128}$/i.test(line) ||
        /^Diagnostic:\s*[A-Za-z0-9._:-]{1,128}$/i.test(line)
      ));
    if (diagnosticErrorCluster) return true;
    const hasExactLine = (pattern) => lines.some((line) => pattern.test(line));
    const loginControlCluster = lines.length >= 2 &&
      hasExactLine(/^Log in$/i) &&
      (hasExactLine(/^Sign up$/i) || hasExactLine(/^Continue with(?:\s+.+)?$/i));
    const securityPageShell = lines.length >= 2 &&
      hasExactLine(/^Performing security verification$/i) &&
      hasExactLine(/^(?:Checking your browser(?: before accessing ChatGPT)?|Verify you are human|Cloudflare)$/i);
    const captchaPageShell = lines.length >= 2 &&
      hasExactLine(/^Verify you are human$/i) &&
      (hasExactLine(/^Cloudflare$/i) || hasExactLine(/^Privacy\s*[•·-]\s*Help$/i));
    if (loginControlCluster || securityPageShell || captchaPageShell) return true;

    // Reject if the text looks like a raw system prompt echo (no actual
    // model content — just system instructions leaked into the DOM).
    if (hasStrongSystemLeak(text)) {
      // Additional guard: if the text is very short or is just the system
      // prompt without any model-generated content, treat it as a leak.
      // But if it's long enough to be a real response, let it through
      // to finalizeAssistant which will properly parse PAM tokens.
      const stripped = text.replace(/<System\s*-[\s\S]*?>/gi, "").replace(/<\/System\s*-[^>]*>/gi, "").replace(/<\/?(?:user_context|current_time|relevant_memories|response_format)[^>]*>/gi, "").trim();
      if (stripped.length < 50) return true;  // Too short — pure leak
      // For longer texts, only reject if it matches the enriched prompt pattern
      // (<System - Current Date and Time> ... Format: |STPAM|...) or (<user_context> ... |STPAM|...)
      if (/^<System\s*-\s*Memory Module/i.test(text.trim()) || /^<user_context>/i.test(text.trim())) return true;
      // Otherwise, let it through — it's likely a real response
    }

    return false;
  }

  function expirePendingStoreIfStale(reason) {
    if (!STATE.pendingStore || !STATE.pendingStore.createdAt) return;
    const age = Date.now() - STATE.pendingStore.createdAt;
    if (age < 45000) return;
    debugIo("INCOMING (pending-store-expired)", {
      reason,
      ageMs: age,
      sessionId: STATE.pendingStore.sessionId
    });
    STATE.pendingStore = null;
  }

  function scoreStrongLeakText(text) {
    if (!text) return 0;
    let score = 0;
    if (/<user_context>|<current_time>|<System\s*-\s*Current Date and Time>/i.test(text)) score += 4;
    if (/<relevant_memories>|<System\s*-\s*associated memory context/i.test(text)) score += 6;
    if (/<response_format>|<System\s*-\s*Additional instruction/i.test(text)) score += 6;
    if (/<System\s*-\s*New Conversation thread/i.test(text)) score += 4;
    if (/\|\s*Turn distance:/i.test(text)) score += 2;
    if (/\|\s*Confidence:/i.test(text)) score += 2;
    const lineCount = text.split(/\r?\n/).length;
    if (lineCount >= 6) score += 2;
    if (text.length >= 300) score += 2;
    return score;
  }

  function findArtifactLeakInContainer(adapter) {
    const nodes = queryMaskNodes(adapter, "article, [role='listitem'], [data-message-author-role='user'], [data-author='user'], *");
    let best = null;
    for (const el of nodes) {
      if (!isElementVisible(el)) continue;
      if (isUnsafeMaskTarget(el)) continue;
      if (adapter.isReactSite && !resolveReactUserMaskTarget(el)) continue;
      const text = adapter.extractMessageText ? adapter.extractMessageText(el) : el.innerText;
      if (!text || text.length < 20) continue;
      if (!hasStrongSystemLeak(text)) continue;

      const depth = getElementDepth(el);
      const len = text.length;
      const score = scoreStrongLeakText(text);
      if (!best ||
        score > best.score ||
        (score === best.score && depth > best.depth) ||
        (score === best.score && depth === best.depth && len < best.len)) {
        best = { el, len, depth, score };
      }
    }
    return best ? best.el : null;
  }

  function forceMaskVisibleLeaks(adapter, item) {
    if (!item || !item.originalText) return 0;
    const originalNorm = normalizeText(item.originalText);
    if (!originalNorm) return 0;

    const snippetLen = Math.min(Math.max(12, Math.floor(originalNorm.length * 0.4)), 80);
    const snippet = originalNorm.slice(0, snippetLen);
    const promptTimestamp = item.promptTimestamp || "";
    const maxLen = getMaskMaxLen(item);

    const reactSafe = !!adapter.isReactSite;
    const nodes = queryMaskNodes(adapter, "article, [role='listitem'], [data-message-author-role='user'], [data-author='user'], [data-testid*='user'], *");
    const candidates = [];
    for (const el of nodes) {
      if (!isElementVisible(el)) continue;
      if (reactSafe && !resolveReactUserMaskTarget(el)) continue;
      // Skip elements already masked with our overlay
      if (reactSafe && el.querySelector && el.querySelector(".bdbm-overlay-text")) continue;
      const text = adapter.extractMessageText ? adapter.extractMessageText(el) : el.innerText;
      if (!text || text.length < item.originalText.length + 10) continue;
      if (text.length > maxLen) continue;
      if (isTooBroadCandidate(el, text, item)) continue;
      const normalized = normalizeText(text);
      if (!normalized) continue;
      const snippetMatch = snippet && normalized.includes(snippet);
      const timestampMatch = promptTimestamp && normalized.includes(promptTimestamp);
      if (promptTimestamp) {
        if (!timestampMatch && !snippetMatch) continue;
      } else if (!snippetMatch) {
        continue;
      }
      const likelyLeak = hasStrongSystemLeak(text) || isLikelyPendingLeakText(text, item);
      if (!likelyLeak) continue;
      candidates.push({ el, len: text.length, depth: getElementDepth(el), text });
    }

    candidates.sort((a, b) => {
      if (a.depth !== b.depth) return b.depth - a.depth;
      return a.len - b.len;
    });

    const replaced = [];
    let count = 0;
    for (const candidate of candidates) {
      if (replaced.some((el) => el.contains(candidate.el))) continue;
      replaceMessageText(adapter, candidate.el, item.originalText);
      if (reactSafe) {
        // On React sites, overlay was applied — check for overlay element
        if (candidate.el.querySelector && candidate.el.querySelector(".bdbm-overlay-text")) {
          replaced.push(candidate.el);
          count += 1;
        }
      } else {
        const after = adapter.extractMessageText ? adapter.extractMessageText(candidate.el) : candidate.el.innerText;
        if (normalizeText(after) === originalNorm) {
          replaced.push(candidate.el);
          count += 1;
        }
      }
    }
    return count;
  }

  function sanitizeAllVisibleLeakNodes(adapter, preferredText = "", preferredTimestamp = "") {
    const nodes = queryMaskNodes(adapter, "article, [role='listitem'], [data-message-author-role='user'], [data-author='user'], [data-testid*='user'], *");
    const preferredNorm = normalizeText(preferredText || "");
    const preferredSnippet = getPromptSnippet(preferredText || "");
    const tsNorm = normalizeText(preferredTimestamp || "");
    const sizeHint = {
      originalText: preferredText || "",
      enrichedText: preferredText || ""
    };
    let count = 0;

    const reactSafe = !!adapter.isReactSite;
    for (const el of nodes) {
      if (!isElementVisible(el)) continue;
      if (isUnsafeMaskTarget(el)) continue;
      if (reactSafe && !resolveReactUserMaskTarget(el)) continue;
      // Skip elements already masked with our overlay
      if (reactSafe && el.querySelector && el.querySelector(".bdbm-overlay-text")) continue;
      const text = adapter.extractMessageText ? adapter.extractMessageText(el) : el.innerText;
      if (!text || text.length < 60) continue;
      if (isTooBroadCandidate(el, text, sizeHint)) continue;
      if (!hasStrongSystemLeak(text) && !containsControlArtifacts(text)) continue;
      // Gemini PAM guard: don't strip PAM tokens from assistant responses
      if (STATE.pendingStore && /\|STPAM\||\|MIDPAM\||\|ENDPAM\|/i.test(text) && !hasStrongSystemLeak(text)) continue;

      const norm = normalizeText(text);
      const timestampMatch = tsNorm && norm.includes(tsNorm);
      const shouldUsePreferred = preferredNorm &&
        (timestampMatch || norm.includes(preferredNorm) || (preferredSnippet && norm.includes(preferredSnippet)));
      const cleaned = shouldUsePreferred
        ? preferredText
        : extractUserPrompt(text);
      debugIo("UI MASK (sanitizeAllVisibleLeakNodes candidate)", {
        element: elementBrief(el),
        textBefore: (text || "").slice(0, 200),
        shouldUsePreferred,
        cleanedText: (cleaned || "").slice(0, 200),
        cleanedLen: (cleaned || "").length
      });
      if (!cleaned) continue;
      if (normalizeText(cleaned) === norm) continue;

      if (replaceMessageText(adapter, el, cleaned)) {
        count += 1;
      }
    }

    return count;
  }

  function sanitizeLatestUserLeak(adapter) {
    let el = null;
    if (adapter.getLastUserMessageElement) {
      el = adapter.getLastUserMessageElement();
    }
    if (!el) {
      el = findArtifactLeakInContainer(adapter);
    }
    if (!el) return;
    // Skip if already masked with overlay
    if (adapter.isReactSite && el.querySelector && el.querySelector(".bdbm-overlay-text")) return;
    const text = adapter.extractMessageText ? adapter.extractMessageText(el) : el.innerText;
    if (!text) return;
    const likelyLeak = hasStrongSystemLeak(text) ||
      (/\|STPAM\|/i.test(text) && /\|ENDPAM\|/i.test(text)) ||
      (/\|MEMQUERY\|/i.test(text) && /\|ENDQUERY\|/i.test(text));
    if (!likelyLeak) return;
    // Gemini PAM guard: don't strip PAM tokens while pendingStore is active
    if (STATE.pendingStore && /\|STPAM\||\|ENDPAM\|/i.test(text) && !hasStrongSystemLeak(text)) return;
    const cleaned = extractUserPrompt(text);
    if (!cleaned) return;
    if (normalizeText(cleaned) !== normalizeText(text)) {
      if (replaceMessageText(adapter, el, cleaned)) {
        debugIo("UI MASK (self-heal)", {
          uiTextBefore: text,
          uiTextAfter: cleaned
        });
      }
    }
  }

  function getDeepActiveElement() {
    let el = document.activeElement;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) {
      el = el.shadowRoot.activeElement;
    }
    return el;
  }

  function findEditableInEventPath(event) {
    if (!event || typeof event.composedPath !== "function") return null;
    const path = event.composedPath();
    for (const node of path) {
      if (!node || !(node instanceof HTMLElement)) continue;
      if (node.tagName === "TEXTAREA" || node.isContentEditable) {
        return node;
      }
    }
    return null;
  }

  function armBypass() {
    STATE.bypass = true;
    if (STATE.bypassTimer) clearTimeout(STATE.bypassTimer);
    STATE.bypassTimer = setTimeout(() => {
      STATE.bypass = false;
      STATE.bypassTimer = null;
    }, 0);
  }

  function getInputText(input) {
    return input && input.isContentEditable ? input.innerText : (input ? input.value : "");
  }

  function isLikelySendButton(el) {
    if (!el) return false;
    const btn = el.closest ? el.closest("button,[role='button'],input[type='submit']") : null;
    if (!btn) return false;
    const label = (
      btn.getAttribute("aria-label") ||
      btn.getAttribute("title") ||
      btn.textContent ||
      ""
    ).toLowerCase();
    const dataTest = (btn.getAttribute("data-testid") || "").toLowerCase();
    const typeAttr = (btn.getAttribute("type") || "").toLowerCase();
    const hasWord = ["send", "submit", "odeslat", "poslat", "send message", "send prompt"].some((w) => label.includes(w));
    const hasTest = dataTest.includes("send");
    const isSubmit = typeAttr === "submit";
    return hasWord || hasTest || isSubmit;
  }

  // Shadow DOM variant: e.target at window level is the shadow host (retargeted),
  // so closest() can't find the inner <button>. Walk composedPath() instead.
  function isSendButtonInEventPath(event) {
    if (!event || typeof event.composedPath !== "function") return false;
    const path = event.composedPath();
    const SEND_WORDS = ["send", "submit", "odeslat", "poslat", "send message", "send prompt"];
    for (const node of path) {
      if (!(node instanceof HTMLElement)) continue;
      const tag = node.tagName;
      const role = (node.getAttribute("role") || "").toLowerCase();
      const typeAttr = (node.getAttribute("type") || "").toLowerCase();
      const isBtn = tag === "BUTTON" || role === "button" || (tag === "INPUT" && typeAttr === "submit");
      if (!isBtn) continue;
      const label = (
        node.getAttribute("aria-label") ||
        node.getAttribute("title") ||
        node.textContent ||
        ""
      ).toLowerCase();
      const dataTest = (node.getAttribute("data-testid") || "").toLowerCase();
      if (SEND_WORDS.some((w) => label.includes(w)) || dataTest.includes("send") || typeAttr === "submit") {
        return true;
      }
    }
    return false;
  }

  function shouldGuardSend() {
    const now = Date.now();
    if (now < STATE.sendGuardUntil) {
      return true;
    }
    STATE.sendGuardUntil = now + 300;
    return false;
  }

  function schedulePrefetch(text) {
    if (STATE.bypass) return;
    if (!text || !text.trim()) return;
    // Only prefetch on a reasonably complete prompt (3+ words, 15+ chars).
    // Avoids sending single characters/words to the memory server prematurely.
    const words = text.trim().split(/\s+/);
    if (words.length < 3 || text.trim().length < 15) return;
    STATE.prefetchText = text;
    if (STATE.prefetchTimer) clearTimeout(STATE.prefetchTimer);
    STATE.prefetchTimer = setTimeout(() => {
      prefetchMemories(text);
    }, 500);
  }

  function getAuthoritativeAssistantSnapshot(adapter) {
    if (!adapter) return { assistant: null, assistantCount: null, assistantText: "" };
    if (typeof adapter.getAssistantMessageElements === "function") {
      const assistants = Array.from(adapter.getAssistantMessageElements() || []);
      const assistant = assistants.length > 0 ? assistants[assistants.length - 1] : null;
      const text = assistant
        ? (adapter.extractMessageText ? adapter.extractMessageText(assistant) : (assistant.innerText || assistant.textContent || ""))
        : "";
      return {
        assistant,
        assistantCount: assistants.length,
        assistantText: normalizeText(text)
      };
    }
    const assistant = typeof adapter.getLastAssistantMessageElement === "function"
      ? adapter.getLastAssistantMessageElement()
      : null;
    const text = assistant
      ? (adapter.extractMessageText ? adapter.extractMessageText(assistant) : (assistant.innerText || assistant.textContent || ""))
      : "";
    return { assistant, assistantCount: null, assistantText: normalizeText(text) };
  }

  function getLastAuthoritativeAssistant(adapter) {
    return getAuthoritativeAssistantSnapshot(adapter).assistant;
  }

  function isPendingBaselineAssistant(adapter, pendingStore, snapshot) {
    if (!pendingStore || adapter.requiresAuthoritativeAssistantProvenance !== true) return false;
    if (!!pendingStore.baselineAssistant && pendingStore.baselineAssistant === snapshot.assistant) return true;
    const sameText = !!pendingStore.baselineAssistantText &&
      pendingStore.baselineAssistantText === snapshot.assistantText;
    if (Number.isFinite(pendingStore.baselineAssistantCount) && Number.isFinite(snapshot.assistantCount)) {
      if (snapshot.assistantCount > pendingStore.baselineAssistantCount) return false;
      return sameText;
    }
    return sameText;
  }

  function createPendingStore(adapter, sessionId, userText, sentPromptText) {
    const baseline = getAuthoritativeAssistantSnapshot(adapter);
    return {
      sessionId,
      userText,
      sentPromptText,
      createdAt: Date.now(),
      baselineAssistant: baseline.assistant,
      baselineAssistantCount: baseline.assistantCount,
      baselineAssistantText: baseline.assistantText,
      lastAssistantText: "",
      assistantTimer: null
    };
  }

  function claimCompletedPendingBeforeReplacement(adapter) {
    const pendingStore = STATE.pendingStore;
    if (!pendingStore || pendingStore.inFlight) return false;
    if (adapter.requiresAuthoritativeAssistantProvenance !== true) return false;
    if (typeof adapter.isResponseStreaming !== "function" || adapter.isResponseStreaming() !== false) return false;

    const assistantSnapshot = getAuthoritativeAssistantSnapshot(adapter);
    const assistant = assistantSnapshot.assistant;
    if (!assistant || isPendingBaselineAssistant(adapter, pendingStore, assistantSnapshot)) return false;
    const text = adapter.extractMessageText
      ? adapter.extractMessageText(assistant)
      : (assistant.innerText || assistant.textContent || "");
    const normalizedText = normalizeText(text);
    if (!normalizedText || shouldIgnoreAssistantCandidate(text, pendingStore)) return false;
    if (!pendingStore.lastAssistantText || normalizedText !== normalizeText(pendingStore.lastAssistantText)) return false;

    const finalization = finalizeAssistant(adapter, assistant, pendingStore);
    if (finalization && typeof finalization.catch === "function") {
      finalization.catch((err) => log(`rapid-turn finalize failed: ${err.message}`));
    }
    if (!pendingStore.inFlight) return false;

    const timer = pendingStore.assistantTimer;
    if (timer) clearTimeout(timer);
    pendingStore.assistantTimer = null;
    if (STATE.assistantTimer === timer) STATE.assistantTimer = null;
    return true;
  }

  const RETRIEVAL_CANDIDATE_LIMIT = 20;
  const PROMPT_MEMORY_LIMIT = 5;

  function isExactNonInformativeText(value) {
    return /^(?:unknown|n\/?a|not sure|i (?:do not|don't) know|no (?:information|data)(?: available)?)[.!?]*$/i.test(String(value || "").trim());
  }

  function isExactNonInformativePayload(value) {
    const visible = String(value || "")
      .replace(/\|STPAM\|[\s\S]*?\|ENDPAM\|/gi, " ")
      .replace(/\|TITLE\|[^\n\r]*/gi, " ")
      .trim();
    return isExactNonInformativeText(visible);
  }

  function isNonInformativeMemoryValue(user, value) {
    const key = String(user || "").trim();
    const raw = String(value || "").trim();
    if (!raw) return true;
    if (isExactNonInformativePayload(raw)) return true;
    const combined = `${key}\n${raw}`;
    const lines = combined.split(/\r?\n|\s*[|•·]\s*/).map((line) => line.trim()).filter(Boolean);
    const saidIndex = lines.findIndex((line) => /^ChatGPT said:$/i.test(line));
    const diagnosticLines = saidIndex >= 0 ? lines.slice(saidIndex) : lines;
    const diagnosticCluster = diagnosticLines.length > 0 && diagnosticLines.length <= 6 &&
      diagnosticLines.some((line) => /^Unable to connect$/i.test(line)) &&
      diagnosticLines.some((line) => /^Retry$/i.test(line)) &&
      diagnosticLines.every((line) => line.length <= 180 && (
        /^ChatGPT said:$/i.test(line) || /^Unable to connect$/i.test(line) || /^Retry$/i.test(line) ||
        /^[•·-]$/i.test(line) || /^Request ID:\s*[A-Za-z0-9._:-]{1,128}$/i.test(line) ||
        /^Diagnostic:\s*[A-Za-z0-9._:-]{1,128}$/i.test(line)
      ));
    const diagnosticPair = (/^Unable to connect$/i.test(key) && /^Retry$/i.test(raw)) ||
      (/^Request ID:\s*[A-Za-z0-9._:-]{1,128}$/i.test(key) && /^[A-Za-z0-9._:-]{1,128}$/i.test(raw)) ||
      (/^(?:Performing )?Security verification$/i.test(key) && /^Verify you are human$/i.test(raw));
    return diagnosticPair || diagnosticCluster || shouldIgnoreAssistantCandidate(raw, { userText: "", sentPromptText: "" });
  }

  function opaqueIdentifiers(text) {
    const matches = String(text || "").match(/\b[A-Za-z0-9]+(?:(?:_|-)[A-Za-z0-9]+)+\b/g) || [];
    return new Set(matches.filter((token) => token.includes("_") || /\d/.test(token)).map((token) => token.toUpperCase()));
  }

  function lexicalTokens(text) {
    return new Set((String(text || "").toLowerCase().match(/[\p{L}\p{N}]{3,}/gu) || []));
  }

  function selectPromptMemories(memories, query, limit = PROMPT_MEMORY_LIMIT) {
    const queryIds = opaqueIdentifiers(query);
    const queryTokens = lexicalTokens(query);
    return (Array.isArray(memories) ? memories : []).map((memory, index) => {
      const user = String(memory && (memory.user || memory.key) || "");
      const model = String(memory && (memory.model || memory.value) || "");
      if (isNonInformativeMemoryValue(user, model)) return null;
      const memoryIds = opaqueIdentifiers(`${user}\n${model}`);
      const memoryTokens = lexicalTokens(`${user}\n${model}`);
      let identifierOverlap = 0;
      let lexicalOverlap = 0;
      queryIds.forEach((identifier) => { if (memoryIds.has(identifier)) identifierOverlap += 1; });
      queryTokens.forEach((token) => { if (memoryTokens.has(token)) lexicalOverlap += 1; });
      const confidence = Number(memory && memory.confidence);
      return { memory, index, identifierOverlap, lexicalOverlap, confidence: Number.isFinite(confidence) ? confidence : 0 };
    }).filter(Boolean).sort((a, b) =>
      b.identifierOverlap - a.identifierOverlap || b.lexicalOverlap - a.lexicalOverlap ||
      b.confidence - a.confidence || a.index - b.index
    ).slice(0, limit).map((entry) => entry.memory);
  }

  async function prefetchMemories(text) {
    if (!STATE.connected || !STATE.memoryEnabled) return;
    if (STATE.prefetchInFlight) return;
    const normalized = normalizeText(text);
    if (!normalized) return;
    STATE.prefetchInFlight = true;
    const sessionId = `pref_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    // Store the promise so an in-flight prefetch can be awaited at send time
    STATE.prefetchPromise = (async () => {
      try {
        const res = await STATE.client.retrieve(text, sessionId, RETRIEVAL_CANDIDATE_LIMIT);
        STATE.prefetchMemories = selectPromptMemories(res.memories, text);
        STATE.prefetchSessionId = sessionId;
        STATE.prefetchAt = Date.now();
        STATE.prefetchText = text;
        STATE.lastMemoryCount = STATE.prefetchMemories.length;
        updatePanel(true);
      } catch (err) {
        log(`prefetch retrieve failed: ${err.message}`);
      } finally {
        STATE.prefetchInFlight = false;
        STATE.prefetchPromise = null;
      }
    })();
    return STATE.prefetchPromise;
  }

  function getCachedMemories(text) {
    const normalized = normalizeText(text);
    const cachedText = normalizeText(STATE.prefetchText);
    if (!normalized || normalized !== cachedText) return null;
    if (!STATE.prefetchAt) return null;
    if (Date.now() - STATE.prefetchAt > 30000) return null;
    return {
      memories: STATE.prefetchMemories || [],
      sessionId: STATE.prefetchSessionId
    };
  }

  /**
   * If a prefetch is currently in-flight for text matching the given query,
   * returns the promise for it. Awaiting this avoids starting a duplicate
   * retrieve call and gives us the result as soon as it arrives.
   */
  function getInFlightPrefetch(text) {
    if (!STATE.prefetchInFlight || !STATE.prefetchPromise) return null;
    const normalized = normalizeText(text);
    const prefetchNorm = normalizeText(STATE.prefetchText);
    // Accept if text matches or the prefetch text starts with our text
    // (covers the case where user stopped typing slightly before the full prompt)
    if (!normalized || !prefetchNorm) return null;
    if (normalized === prefetchNorm || prefetchNorm.startsWith(normalized) || normalized.startsWith(prefetchNorm)) {
      return STATE.prefetchPromise;
    }
    return null;
  }

  async function prepareNativeSend(adapter, input, text, event) {
    if (!STATE.memoryEnabled || !STATE.connected) return false;

    let memories = [];
    let sessionId = `ext_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    // React's controlled composer does not commit a rewritten value until a
    // later render. Suppress every enriched send, including cache hits, then
    // let the adapter re-resolve the live controls and submit after that render.
    if (event) {
      try { event.preventDefault(); event.stopImmediatePropagation(); } catch (_) { }
    }
    const needsRefire = true;

    // ── Path 1: Cache hit (synchronous) ─────────────────────────────
    // DON'T suppress the event. Set enriched input value and let the
    // original event propagate naturally — ChatGPT/Claude handle it once.
    const cached = getCachedMemories(text);
    if (cached) {
      memories = cached.memories || [];
      sessionId = cached.sessionId;
      debugIo("OUTGOING (send path: cache hit)", { text: text.slice(0, 80) });
      // Fall through to enrichment below.
    } else {
      // ── Path 2: Prefetch in-flight — await the running retrieve ─────
      const inFlight = getInFlightPrefetch(text);
      if (inFlight) {
        debugIo("OUTGOING (send path: awaiting in-flight prefetch)", { text: text.slice(0, 80) });
        try { await inFlight; } catch (_) { /* errors handled inside prefetch */ }
        const freshCache = getCachedMemories(text);
        if (freshCache) {
          memories = freshCache.memories || [];
          sessionId = freshCache.sessionId;
        } else {
          // Prefetch completed but text didn't match — live retrieve
          debugIo("OUTGOING (send path: in-flight miss → live retrieve)", {});
          try {
            const res = await STATE.client.retrieve(text, sessionId, RETRIEVAL_CANDIDATE_LIMIT);
            memories = selectPromptMemories(res.memories, text);
          } catch (err) { log(`retrieve failed: ${err.message}`); }
        }
      } else {
        // ── Path 3: No cache, no in-flight — live retrieve at send time ─
        debugIo("OUTGOING (send path: live retrieve)", { text: text.slice(0, 80) });
        try {
          const res = await STATE.client.retrieve(text, sessionId, RETRIEVAL_CANDIDATE_LIMIT);
          memories = selectPromptMemories(res.memories, text);
          STATE.lastMemoryCount = memories.length;
          updatePanel(true);
        } catch (err) {
          log(`retrieve failed: ${err.message}`);
        }
      }
    }

    // Build and inject the enriched prompt into the input field
    armBypass();
    const prompt = window.BdbmPromptBuilder.buildEnrichedPrompt({
      userText: text,
      memories,
      isFirstTurn: adapter.isFirstTurn ? adapter.isFirstTurn() : false
    });
    const promptTimestamp = extractPromptTimestamp(prompt.combinedPrompt);

    setInputValue(input, prompt.combinedPrompt);

    const queuedUser = enqueuePendingUser(adapter, {
      originalText: text,
      enrichedText: prompt.combinedPrompt,
      sessionId,
      promptTimestamp
    });
    schedulePendingUserSweep(adapter, queuedUser);

    claimCompletedPendingBeforeReplacement(adapter);
    const pendingStore = createPendingStore(adapter, sessionId, text, prompt.combinedPrompt);
    STATE.pendingStore = pendingStore;
    STATE.lastTurn = {
      userText: text,
      memories,
      sessionId,
      isFirstTurn: adapter.isFirstTurn ? adapter.isFirstTurn() : false
    };

    debugIo("OUTGOING (native send enriched)", {
      originalUserPrompt: text,
      enrichedPrompt: prompt.combinedPrompt,
      systemPrompt: prompt.systemPrompt,
      memoriesUsed: memories.length,
      needsRefire,
      sessionId
    });

    // Re-fire after the controlled input value has reached the site framework.
    // The adapter's refireAfterSend() handles all site-specific timing and mechanism.
    // armBypass is passed as a callback so the adapter can re-arm it after any
    // async waits (the original bypass timeout expires during async retrieve).
    let refireSucceeded = true;
    if (needsRefire) {
      const liveBtn = adapter.findSendButton ? adapter.findSendButton() : null;
      if (adapter.refireAfterSend) {
        refireSucceeded = (await adapter.refireAfterSend(input, liveBtn, armBypass, prompt.combinedPrompt)) !== false;
      } else {
        armBypass();
        if (liveBtn) {
          liveBtn.click();
        } else {
          const enterEvent = new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true });
          input.dispatchEvent(enterEvent);
        }
      }
    }

    if (!refireSucceeded) {
      removePendingUserItem(queuedUser, true);
      if (STATE.pendingStore === pendingStore) {
        STATE.pendingStore = null;
        const liveInputForRestore = adapter.findInput ? adapter.findInput() : input;
        setInputValue(liveInputForRestore, text);
      }
      return false;
    }

    // Post-send input clearing: adapters that need to forcibly clear the
    // composer (Gemini, Perplexity, ChatGPT) implement clearInputAfterSend().
    // ChatGPT needs it since its composer draft-persistence re-populates the
    // input with the sent enriched prompt. Claude clears itself natively,
    // so its adapter omits this.
    // Must run for BOTH sync (cache-hit) and async send paths.
    if (adapter.clearInputAfterSend) {
      const liveInputForClear = adapter.findInput ? adapter.findInput() : input;
      adapter.clearInputAfterSend(liveInputForClear);
    }

    return true;
  }

  function queueStoreForNativeSend(adapter, text) {
    if (!STATE.memoryEnabled || !STATE.connected) return;
    const sessionId = `ext_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    claimCompletedPendingBeforeReplacement(adapter);
    STATE.pendingStore = createPendingStore(adapter, sessionId, text, text);
    STATE.lastTurn = {
      userText: text,
      memories: [],
      sessionId,
      isFirstTurn: STATE.adapter && STATE.adapter.isFirstTurn ? STATE.adapter.isFirstTurn() : false
    };
    const queuedUser = enqueuePendingUser(adapter, {
      originalText: text,
      enrichedText: text,
      sessionId
    });
    schedulePendingUserSweep(adapter, queuedUser);

    debugIo("OUTGOING (native send without enrichment)", {
      originalUserPrompt: text,
      enrichedPrompt: text,
      sessionId
    });

    STATE.client.retrieve(text, sessionId, 5)
      .then((res) => {
        STATE.lastMemoryCount = res.memories ? res.memories.length : 0;
        updatePanel(true);
      })
      .catch((err) => {
        log(`retrieve (post-send) failed: ${err.message}`);
      });
  }

  function setInputValue(input, value) {
    if (!input) return "";

    // Delegate to the adapter's site-specific input writer if available.
    // Shadow DOM sites (Gemini) override writeInputValue() with an
    // execCommand-based approach that fires a TRUSTED InputEvent the
    // Lit/Angular framework observes correctly. Non-shadow DOM sites
    // (ChatGPT, Claude, Perplexity) fall through to the standard path below.
    if (STATE.adapter && STATE.adapter.writeInputValue && input.isContentEditable) {
      return STATE.adapter.writeInputValue(input, value);
    }

    // ── Non-Shadow DOM sites (ChatGPT, Claude, Perplexity): existing approach ──
    let lastValue = input.value;
    if (input && input.isContentEditable) {
      lastValue = input.textContent || "";
      if (normalizeText(lastValue) === normalizeText(value)) return lastValue;
      try {
        input.focus();
      } catch (_) {
        // ignore focus errors
      }

      try {
        const selection = window.getSelection();
        if (selection) {
          selection.removeAllRanges();
          const range = document.createRange();
          range.selectNodeContents(input);
          selection.addRange(range);
        }
      } catch (_) {
        // ignore selection errors
      }

      // Focus guard: execCommand("insertText") writes into the DOCUMENT's
      // focused element, not into `input`. If something stole focus between
      // the send attempt and this write (e.g. ChatGPT's login modal), the
      // enriched prompt would land in an unrelated field. In that case skip
      // the selection/execCommand path entirely and write directly.
      const activeNow = getDeepActiveElement();
      const focusOnInput = activeNow === input ||
        (input.contains && activeNow && input.contains(activeNow));
      if (!focusOnInput) {
        debugIo("setInputValue: focus NOT on target input — using direct write", {
          target: elementBrief(input),
          activeElement: elementBrief(activeNow)
        });
        input.textContent = value;
        try {
          input.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, data: value, inputType: "insertText" }));
        } catch (_) {
          input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        }
        input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
        return lastValue;
      }

      let handled = false;
      let needsSyntheticInput = false;
      try {
        const dt = new DataTransfer();
        dt.setData("text/plain", value);
        const pasteEvent = new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: dt });
        input.dispatchEvent(pasteEvent);
        handled = pasteEvent.defaultPrevented;
      } catch (_) {
        // ClipboardEvent/DataTransfer may be blocked
      }

      if (!handled && document.queryCommandSupported && document.queryCommandSupported("insertText")) {
        try {
          document.execCommand("insertText", false, value);
          handled = true;
        } catch (_) {
          // ignore
        }
      }

      if (!handled) {
        input.textContent = value;
        needsSyntheticInput = true;
      }

      // Some rich editors append instead of replacing. Force exact value if mismatch remains.
      const resulting = (input.innerText || input.textContent || "").trim();
      if (normalizeText(resulting) !== normalizeText(value)) {
        input.textContent = value;
        needsSyntheticInput = true;
      }

      if (needsSyntheticInput) {
        try {
          input.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, data: value, inputType: "insertText" }));
        } catch (_) {
          input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        }
        input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      }
      return lastValue;
    }
    lastValue = input.value;
    const proto = input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) {
      setter.call(input, value);
    } else {
      input.value = value;
    }
    input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    return lastValue;
  }

  function triggerSend(adapter, input, sendBtn, text) {
    setInputValue(input, text);
    if (sendBtn) {
      sendBtn.click();
      return;
    }
    // fallback: press Enter
    const enterEvent = new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true });
    input.dispatchEvent(enterEvent);
  }

  async function handleSend(originalText, adapter, input, sendBtn) {
    if (STATE.isSending) return;
    if (!originalText || !originalText.trim()) return;

    STATE.isSending = true;
    try {
      if (!STATE.memoryEnabled || !STATE.connected) {
        armBypass();
        triggerSend(adapter, input, sendBtn, originalText);
        return;
      }

      const sessionId = `ext_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
      let memories = [];
      try {
        // If a prefetch is in-flight for this text, await it instead of
        // starting a duplicate retrieve — eliminates redundant server calls
        const inFlight = getInFlightPrefetch(originalText);
        if (inFlight) {
          await inFlight;
          const cached = getCachedMemories(originalText);
          if (cached) {
            memories = cached.memories || [];
          } else {
            // Prefetch completed but text didn't match — do immediate retrieve
            const result = await STATE.client.retrieve(originalText, sessionId, RETRIEVAL_CANDIDATE_LIMIT);
            memories = selectPromptMemories(result.memories, originalText);
          }
        } else {
          // No in-flight prefetch — do immediate retrieve
          const result = await STATE.client.retrieve(originalText, sessionId, RETRIEVAL_CANDIDATE_LIMIT);
          memories = selectPromptMemories(result.memories, originalText);
        }
        STATE.lastMemoryCount = memories.length;
        updatePanel(true);
      } catch (err) {
        log(`retrieve failed: ${err.message}`);
      }

      const prompt = window.BdbmPromptBuilder.buildEnrichedPrompt({
        userText: originalText,
        memories,
        isFirstTurn: adapter.isFirstTurn ? adapter.isFirstTurn() : false
      });
      const promptTimestamp = extractPromptTimestamp(prompt.combinedPrompt);

      debugIo("OUTGOING (intercepted send)", {
        originalUserPrompt: originalText,
        enrichedPrompt: prompt.combinedPrompt,
        systemPrompt: prompt.systemPrompt,
        sessionId
      });

      const queuedUser = enqueuePendingUser(adapter, {
        originalText,
        enrichedText: prompt.combinedPrompt,
        sessionId,
        promptTimestamp
      });
      schedulePendingUserSweep(adapter, queuedUser);

      claimCompletedPendingBeforeReplacement(adapter);
      STATE.pendingStore = createPendingStore(adapter, sessionId, originalText, prompt.combinedPrompt);
      STATE.lastTurn = {
        userText: originalText,
        memories,
        sessionId,
        isFirstTurn: adapter.isFirstTurn ? adapter.isFirstTurn() : false
      };

      armBypass();
      triggerSend(adapter, input, sendBtn, prompt.combinedPrompt);
    } finally {
      STATE.isSending = false;
    }
  }

  function attachSendHooks(adapter, input, sendBtn) {
    const onSendAttempt = async (e) => {
      if (STATE.bypass) return;
      if (STATE.isSending) {
        try { e.preventDefault(); e.stopImmediatePropagation(); } catch (_) { }
        return;
      }
      if (shouldGuardSend()) return;
      const liveInput = findEditableInEventPath(e) || getDeepActiveElement() || (adapter.findInput ? adapter.findInput() : findHeuristicInput());
      if (!liveInput) return;

      // Composer-only guard: generic <input> fields (e.g. the email field in
      // ChatGPT's login modal — its "Continue" button is type=submit, which
      // matches isLikelySendButton) must never be treated as the prompt
      // source. Only the site's chat composer (textarea / contenteditable,
      // or whatever adapter.findInput() resolves to) may trigger enrichment.
      const composerEl = adapter.findInput ? adapter.findInput() : null;
      const isChatComposer =
        liveInput.tagName === "TEXTAREA" ||
        liveInput.isContentEditable ||
        (composerEl && (composerEl === liveInput ||
          (composerEl.contains && composerEl.contains(liveInput)) ||
          (liveInput.contains && liveInput.contains(composerEl))));
      if (!isChatComposer) {
        debugIo("OUTGOING (blocked: non-composer input, native event passes through)", {
          element: elementBrief(liveInput)
        });
        return;
      }

      const text = getInputText(liveInput);
      if (!text || !text.trim()) return;

      // Cancel any pending prefetch timer — too late to be useful,
      // prepareNativeSend handles retrieve correctly for all cases.
      if (STATE.prefetchTimer) { clearTimeout(STATE.prefetchTimer); STATE.prefetchTimer = null; }

      if (!STATE.memoryEnabled || !STATE.connected) {
        // Memory disabled or disconnected: let the native event pass through.
        // The queue helper is a no-op until the local daemon is connected.
        queueStoreForNativeSend(adapter, text);
        return;
      }

      // prepareNativeSend always: suppresses event → retrieves → enriches → re-fires.
      // Three internal paths: (1) cache hit, (2) await in-flight prefetch, (3) live retrieve.
      STATE.isSending = true;
      try {
        await prepareNativeSend(adapter, liveInput, text, e);
      } finally {
        STATE.isSending = false;
      }
    };

    if (input) {
      input.addEventListener("input", (e) => {
        const liveInput = findEditableInEventPath(e) || input;
        const text = getInputText(liveInput);
        schedulePrefetch(text);
      }, true);
    }

    window.addEventListener("input", (e) => {
      const target = findEditableInEventPath(e) || e.target;
      if (!target) return;
      if (target.closest && target.closest(".bdbm-injector-panel")) return;
      if (!(target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      schedulePrefetch(getInputText(target));
    }, true);

    window.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.shiftKey) return;
      onSendAttempt(e);
    }, true);

    window.addEventListener("pointerdown", (e) => {
      const target = e.target;
      if (!target) return;
      if (target.closest && target.closest(".bdbm-injector-panel")) return;
      if (!isLikelySendButton(target) && !isSendButtonInEventPath(e)) return;
      onSendAttempt(e);
    }, true);

    window.addEventListener("click", (e) => {
      const target = e.target;
      if (!target) return;
      if (target.closest && target.closest(".bdbm-injector-panel")) return;
      if (!isLikelySendButton(target) && !isSendButtonInEventPath(e)) return;
      onSendAttempt(e);
    }, true);

    document.addEventListener("submit", (e) => {
      onSendAttempt(e);
    }, true);
  }

  function startUserMessageObserver(adapter) {
    const root = document.body || (adapter.getMessageContainer ? adapter.getMessageContainer() : null);
    if (!root) return;

    const observer = new MutationObserver((mutations) => {
      // Invalidate adapter caches on DOM mutations (shadow DOM sites)
      if (adapter.invalidateCache) adapter.invalidateCache();

      if (STATE.pendingUserQueue.length === 0) {
        sanitizeLatestUserLeak(adapter);
        sanitizeAllVisibleLeakNodes(adapter);
        return;
      }
      const item = STATE.pendingUserQueue[0];
      const lastUser = resolvePendingUserTarget(adapter, item, mutations);
      if (!lastUser) {
        pendingMaskDiag(adapter, item, "observer no-target", 1000);
        sanitizeLatestUserLeak(adapter);
        sanitizeAllVisibleLeakNodes(adapter, item.originalText, item.promptTimestamp || "");
        // Nuclear fallback: use adapter's deep leak scanner if available
        if (adapter.findLeakedElements) {
          const leaked = adapter.findLeakedElements();
          for (const leakEl of leaked) {
            const leakText = adapter.extractMessageText ? adapter.extractMessageText(leakEl) : leakEl.innerText;
            if (leakText && containsControlArtifacts(leakText)) {
              const cleaned = extractUserPrompt(leakText);
              if (cleaned && normalizeText(cleaned) !== normalizeText(leakText)) {
                replaceMessageText(adapter, leakEl, item.originalText || cleaned);
              }
            }
          }
        }
        if (Date.now() - (item.queuedAt || 0) > 20000) {
          removePendingUserItem(item, true);
        }
        return;
      }
      const current = adapter.extractMessageText ? adapter.extractMessageText(lastUser) : lastUser.innerText;

      debugIo("UI MASK (observer target resolved)", {
        element: elementBrief(lastUser),
        currentText: (current || "").slice(0, 200),
        originalText: item.originalText,
        originalTextLen: (item.originalText || "").length,
        hidden: !!item.hidden,
        needsMask: !item.hidden && !!current ? needsUserMask(current, item) : "(n/a)"
      });

      if (item.hidden) {
        if (adapter.isReactSite) {
          lastUser.setAttribute("data-bdbm-react-hidden", "true");
          ensureBdbmHideStyle();
        } else {
          lastUser.style.display = "none";
        }
      } else {
        if (!current) return;
        if (needsUserMask(current, item)) {
          const didReplace = replaceMessageText(adapter, lastUser, item.originalText);
          const forced = forceMaskVisibleLeaks(adapter, item);
          if (!didReplace && forced === 0) {
            pendingMaskDiag(adapter, item, `observer replace BLOCKED (target=${elementBrief(lastUser)})`, 1000);
            return;
          }
          debugIo("UI MASK (user bubble)", {
            uiTextBefore: current,
            uiTextAfter: item.originalText,
            replacedCount: forced,
            targetElement: elementBrief(lastUser),
            promptTimestamp: item.promptTimestamp || "",
            sentEnrichedPrompt: item.enrichedText,
            expectedUserCount: item.expectedUserCount,
            sessionId: item.sessionId
          });
        }
      }
      removePendingUserItem(item, false);
    });

    observer.observe(root, { childList: true, subtree: true, characterData: true });
  }

  function startAssistantObserver(adapter) {
    const root = document.body || (adapter.getMessageContainer ? adapter.getMessageContainer() : null);
    if (!root) return;

    let rescanTimer = null;
    let rescanPendingStore = null;
    const requiresStrictProvenance = adapter.requiresAuthoritativeAssistantProvenance === true;

    const scheduleFinalize = (pendingStore, assistantSnapshot, text) => {
      const assistant = assistantSnapshot.assistant;
      if (STATE.pendingStore !== pendingStore || pendingStore.inFlight) return false;
      if (!assistant || !text || shouldIgnoreAssistantCandidate(text)) return false;
      if (isPendingBaselineAssistant(adapter, pendingStore, assistantSnapshot)) return false;
      if (text !== pendingStore.lastAssistantText) {
        pendingStore.lastAssistantText = text;
        if (pendingStore.assistantTimer) clearTimeout(pendingStore.assistantTimer);
        const timer = setTimeout(() => {
          if (STATE.pendingStore !== pendingStore || pendingStore.inFlight) return;
          if (pendingStore.assistantTimer === timer) pendingStore.assistantTimer = null;
          if (STATE.assistantTimer === timer) STATE.assistantTimer = null;
          return finalizeAssistant(adapter, assistant, pendingStore);
        }, 1500);
        pendingStore.assistantTimer = timer;
        STATE.assistantTimer = timer;
      }
      return true;
    };

    const scheduleRescan = (pendingStore = STATE.pendingStore) => {
      if (!pendingStore || pendingStore.inFlight) return;
      if (STATE.pendingStore !== pendingStore) return;
      if (rescanTimer && rescanPendingStore === pendingStore) return;
      if (rescanTimer) clearTimeout(rescanTimer);
      rescanPendingStore = pendingStore;
      rescanTimer = setTimeout(() => {
        rescanTimer = null;
        if (STATE.pendingStore !== pendingStore || pendingStore.inFlight) return;
        expirePendingStoreIfStale("assistant_rescan");
        if (STATE.pendingStore !== pendingStore) return;

        const assistantSnapshot = getAuthoritativeAssistantSnapshot(adapter);
        const assistant = assistantSnapshot.assistant;
        const text = assistant
          ? (adapter.extractMessageText ? adapter.extractMessageText(assistant) : assistant.innerText)
          : "";
        const streaming = !!(adapter.isResponseStreaming && adapter.isResponseStreaming());
        if (!assistant || isPendingBaselineAssistant(adapter, pendingStore, assistantSnapshot) || !text ||
          shouldIgnoreAssistantCandidate(text) || streaming) {
          scheduleRescan(pendingStore);
          return;
        }
        scheduleFinalize(pendingStore, assistantSnapshot, text);
      }, 500);
    };

    const observer = new MutationObserver((mutations) => {
      const pendingStore = STATE.pendingStore;
      if (!pendingStore || pendingStore.inFlight) return;
      let assistantSnapshot = getAuthoritativeAssistantSnapshot(adapter);
      let lastAssistant = assistantSnapshot.assistant;
      if (!lastAssistant && !requiresStrictProvenance) {
        lastAssistant = findAddedTextNode(mutations);
        assistantSnapshot = { assistant: lastAssistant, assistantCount: null };
      }
      if (!lastAssistant || isPendingBaselineAssistant(adapter, pendingStore, assistantSnapshot)) {
        scheduleRescan(pendingStore);
        return;
      }

      const text = adapter.extractMessageText ? adapter.extractMessageText(lastAssistant) : lastAssistant.innerText;
      if (!text) {
        scheduleRescan(pendingStore);
        return;
      }
      if (shouldIgnoreAssistantCandidate(text)) {
        expirePendingStoreIfStale("ignored_candidate_in_observer");
        scheduleRescan(pendingStore);
        return;
      }

      scheduleFinalize(pendingStore, assistantSnapshot, text);
      if (adapter.isResponseStreaming && adapter.isResponseStreaming()) scheduleRescan(pendingStore);
    });

    observer.observe(root, { childList: true, subtree: true, characterData: true });
  }

  /**
   * Freeze individual text nodes inside a Shadow DOM element so that
   * Lit/Angular re-renders can no longer overwrite them.
   *
   * Lit's ChildPart updates a managed text node via `node.data = value`.
   * By defining a per-instance `data` (and `nodeValue`) property with a
   * no-op setter, we write `originalText` into the underlying C++ DOM
   * buffer once, then silently drop any subsequent writes from the framework.
   * The browser's rendering engine reads the buffer directly, so it keeps
   * showing `originalText` even after Lit re-renders.
   *
   * Returns the number of text nodes successfully frozen.
   */
  function freezeLeakedTextNodes(el, originalText) {
    if (!el || !originalText) return 0;
    let frozen = 0;
    const seen = new Set();

    function processRoot(root) {
      if (!root || seen.has(root)) return;
      seen.add(root);
      const doc = root.ownerDocument || (root === document ? document : null);
      if (!doc || typeof doc.createTreeWalker !== "function") return;
      let walker;
      try {
        walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
      } catch (_) { return; }

      const toFreeze = [];
      let tn = walker.nextNode();
      while (tn) {
        const val = tn.nodeValue || "";
        if (containsControlArtifacts(val) || hasStrongSystemLeak(val)) toFreeze.push(tn);
        tn = walker.nextNode();
      }

      for (const node of toFreeze) {
        // Write originalText into the underlying DOM buffer first
        try { node.nodeValue = originalText; } catch (_) { }
        // Then freeze: intercept future `.data` / `.nodeValue` writes via per-instance
        // property descriptors that shadow the prototype. Lit's ChildPart writes via
        // `node.data = value` — our no-op setter silently drops those updates,
        // leaving the underlying buffer at originalText indefinitely.
        try {
          Object.defineProperty(node, "nodeValue", { get: () => originalText, set: () => { }, configurable: true });
          Object.defineProperty(node, "data", { get: () => originalText, set: () => { }, configurable: true });
        } catch (_) { /* browser disallowed override — regular nodeValue write above still applies */ }
        frozen++;
      }

      // Recurse into nested shadow roots (Gemini has 2-3 levels of Lit components)
      try {
        const nested = root.querySelectorAll ? root.querySelectorAll("*") : [];
        for (const child of nested) {
          if (child.shadowRoot && !seen.has(child.shadowRoot)) processRoot(child.shadowRoot);
        }
      } catch (_) { }
    }

    // Start from el's shadow root (if it's a shadow host) AND from el itself
    if (el.shadowRoot) processRoot(el.shadowRoot);
    processRoot(el);
    return frozen;
  }

  function startDeepLeakSweep(adapter) {
    if (!adapter.isShadowDom) return;
    const siteId = adapter && adapter.siteId ? adapter.siteId : ""; // kept for debug logging
    if (STATE.leakSweepTimer) clearInterval(STATE.leakSweepTimer);
    STATE.leakSweepTimer = setInterval(() => {
      // Invalidate caches before each sweep
      if (adapter.invalidateCache) adapter.invalidateCache();

      sanitizeLatestUserLeak(adapter);
      sanitizeAllVisibleLeakNodes(adapter);

      // Nuclear fallback: direct text-node scan across all shadow roots
      if (adapter.findLeakedElements) {
        const leaked = adapter.findLeakedElements();
        for (const el of leaked) {
          const text = adapter.extractMessageText ? adapter.extractMessageText(el) : el.innerText;
          if (!text || text.length < 20) continue;
          if (!containsControlArtifacts(text) && !hasStrongSystemLeak(text)) continue;

          // ── Gemini PAM-token guard (same as shadow-observer) ────────
          // Don't strip PAM tokens from assistant responses during streaming.
          // finalizeAssistant needs them for memory storage.
          if (STATE.pendingStore && /\|STPAM\||\|MIDPAM\||\|ENDPAM\|/i.test(text)) {
            if (!hasStrongSystemLeak(text)) {
              continue; // Skip — let finalizeAssistant handle this
            }
          }

          const isComposer = isComposerElement(el);
          const hasShadow = !!el.shadowRoot;
          const cleaned = extractUserPrompt(text);
          const cleanedMatchesRaw = !cleaned || normalizeText(cleaned) === normalizeText(text);

          // Skip input/composer elements — we must not overwrite what the user is typing
          if (isComposer) {
            debugIo("UI MASK (deep-sweep)", {
              verdict: "SKIP_COMPOSER",
              element: elementBrief(el),
              textPreview: text.slice(0, 120),
              siteId
            });
            continue;
          }

          // Skip overly broad elements (sidebar, conversation containers,
          // navigation, etc.)  These contain both user AND model text, so
          // replacing their content destroys the model response and/or breaks
          // the sidebar layout.
          if (isShadowMaskUnsafeTarget(el)) {
            debugIo("UI MASK (deep-sweep)", {
              verdict: "SKIP_BROAD",
              element: elementBrief(el),
              textPreview: text.slice(0, 120),
              siteId
            });
            continue;
          }

          // First try surgical removal (preserves HTML structure like citations)
          const didSurgical = surgicalRemovePamTokens(el);
          let frozenCount = 0;
          let didReplace = false;

          if (!didSurgical) {
            if (!cleanedMatchesRaw) {
              // Prefer replaceMessageText over freezeLeakedTextNodes.
              // freezeLeakedTextNodes overrides nodeValue/data setters which can
              // cause V8 deoptimisation and Angular zone instability. Direct
              // text replacement + the shadow-aware MutationObservers provide
              // sufficient coverage for re-render overwrites.
              // Use the original user text from the pending queue when available,
              // rather than the extractUserPrompt() result, for more accurate display.
              const qItem = STATE.pendingUserQueue.length > 0 ? STATE.pendingUserQueue[0] : null;
              const displayText = (qItem && qItem.originalText) ? qItem.originalText : cleaned;
              didReplace = replaceMessageText(adapter, el, displayText);
            } else if (!cleaned) {
              // Pure system text fragment (no user content extractable) — hide it entirely
              debugIo("UI MASK (deep-sweep) !!! HIDING ENTIRE ELEMENT (extractUserPrompt returned empty)", {
                element: elementBrief(el),
                textPreview: (text || "").slice(0, 300),
                pendingQueueLen: STATE.pendingUserQueue.length
              });
              el.style.setProperty("display", "none", "important");
              frozenCount = 1; // mark as handled so verdict shows FROZEN
            }
          }

          debugIo("UI MASK (deep-sweep)", {
            verdict: didSurgical ? "SURGICAL" : (frozenCount > 0 ? "FROZEN" : (didReplace ? "REPLACED" : "NO_ACTION")),
            element: elementBrief(el),
            hasShadow,
            frozenCount,
            cleanedMatchesRaw,
            textPreview: text.slice(0, 120),
            cleanedPreview: (cleaned || "").slice(0, 80),
            siteId
          });
        }
      }
    }, 400);
  }

  function getCurrentAssistantResponseElements(adapter, fallbackAssistant) {
    const assistants = adapter.getAssistantMessageElements
      ? Array.from(adapter.getAssistantMessageElements() || [])
      : [];
    const users = adapter.getUserMessageElements
      ? Array.from(adapter.getUserMessageElements() || [])
      : [];
    const lastUser = users.length > 0 ? users[users.length - 1] : null;

    const isInCurrentTurn = (element) => {
      if (!element || element.isConnected === false || isComposerElement(element)) return false;
      if (!lastUser || typeof lastUser.compareDocumentPosition !== "function") return true;
      return (lastUser.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
    };

    const current = assistants.filter(isInCurrentTurn);
    if (current.length === 0 && isInCurrentTurn(fallbackAssistant)) current.push(fallbackAssistant);
    return Array.from(new Set(current));
  }

  async function finalizeAssistant(adapter, lastAssistant, expectedPendingStore) {
    const pendingStore = expectedPendingStore || STATE.pendingStore;
    if (!pendingStore || STATE.pendingStore !== pendingStore || pendingStore.inFlight) return;

    // Re-query the assistant element at finalize time — the reference captured
    // 1500ms ago by the observer may be stale (React re-renders replace DOM
    // nodes) or may actually be a user message element (observer fallback
    // findAddedTextNode can return the wrong element during early mutations).
    const requiresStrictProvenance = adapter.requiresAuthoritativeAssistantProvenance === true;
    const freshSnapshot = requiresStrictProvenance
      ? getAuthoritativeAssistantSnapshot(adapter)
      : { assistant: lastAssistant, assistantCount: null };
    let freshAssistant = freshSnapshot.assistant;
    if (!freshAssistant && !requiresStrictProvenance) {
      freshAssistant = getLastAuthoritativeAssistant(adapter);
    }
    if (!freshAssistant || isPendingBaselineAssistant(adapter, pendingStore, freshSnapshot)) return;

    let rawText = adapter.extractMessageText ? adapter.extractMessageText(freshAssistant) : (freshAssistant ? freshAssistant.innerText : "");

    // ── Fallback: If single element text lacks PAM tokens, scan wider ──
    // Native LLM sites stream token-by-token and often split the response
    // across multiple DOM elements (paragraphs, divs).  The PAM tokens
    // arrive at the very END of the stream, so we need to read the FULL
    // response text, not just one fragment.
    if (!rawText || !/\|STPAM\|[\s\S]*?\|ENDPAM\|/i.test(rawText)) {
      // Strategy 1: Concatenate text from ALL recent assistant elements
      if (adapter.getAssistantMessageElements) {
        const allAssistant = adapter.getAssistantMessageElements();
        if (allAssistant.length > 0) {
          // Find which elements belong to the LAST response
          // (after the last user message in DOM order)
          const allUser = adapter.getUserMessageElements ? adapter.getUserMessageElements() : [];
          const lastUserEl = allUser.length > 0 ? allUser[allUser.length - 1] : null;

          let combined = "";
          for (const el of allAssistant) {
            // Only include elements that come AFTER the last user message
            if (lastUserEl && (lastUserEl.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) === 0) continue;
            const t = adapter.extractMessageText ? adapter.extractMessageText(el) : el.innerText;
            if (t) combined += "\n" + t;
          }
          combined = combined.trim();
          if (combined && /\|STPAM\|[\s\S]*?\|ENDPAM\|/i.test(combined)) {
            rawText = combined;
          }
        }
      }

    }

    if (!rawText) return;

    // Guard: if the text is actually the user's own message (observer captured
    // wrong element), don't finalize — but don't clear pendingStore either,
    // so the observer can try again when the real response appears.
    if (STATE.lastTurn && normalizeText(rawText) === normalizeText(STATE.lastTurn.userText || "")) {
      return;
    }

    if (shouldIgnoreAssistantCandidate(rawText)) {
      expirePendingStoreIfStale("ignored_candidate_in_finalize");
      return;
    }

    const parsed = window.BdbmPromptBuilder.parsePamTokens(rawText);

    if (!parsed.hasTokens && adapter.isResponseStreaming && adapter.isResponseStreaming()) {
      pendingStore.lastAssistantText = "";
      return;
    }

    debugIo("INCOMING (assistant final)", {
      rawAssistantText: rawText,
      cleanedAssistantText: parsed.displayText || "",
      parsedUserSummary: parsed.userSummary,
      parsedModelSummary: parsed.modelSummary,
      parsedThreadTitle: parsed.threadTitle,
      hadPamTokens: parsed.hasTokens,
      hadArtifacts: parsed.hadArtifacts,
      sessionId: pendingStore.sessionId
    });

    const storeUserText = parsed.hasTokens
      ? (parsed.userSummary || "")
      : (pendingStore.userText || ((STATE.lastTurn && STATE.lastTurn.userText) || ""));
    const storeModelText = parsed.hasTokens
      ? (parsed.modelSummary || "")
      : (parsed.displayText || rawText || "");
    const exactResponseText = parsed.displayText || rawText || "";
    if (isExactNonInformativePayload(parsed.displayText) || isExactNonInformativePayload(storeModelText) ||
      isExactNonInformativePayload(exactResponseText)) {
      expirePendingStoreIfStale("noninformative_parsed_candidate");
      return;
    }
    // Freeze the verified old-turn nodes before store() yields. Cleanup passes
    // must never re-query "latest" after a newer pending turn can exist.
    const cleanupTargets = (parsed.hasTokens || parsed.hadArtifacts)
      ? Array.from(new Set(getCurrentAssistantResponseElements(adapter, freshAssistant)))
      : [];

    if (storeModelText) {
      // Claim this exact turn synchronously before the first await. A second
      // settle timer can then observe the claim and cannot duplicate the store.
      pendingStore.inFlight = true;
      try {
        const storeResult = await STATE.client.store(
          storeUserText,
          storeModelText,
          pendingStore.sessionId,
          exactResponseText
        );
      } catch (err) {
        log(`store failed: ${err.message}`);
      }
    } else {
      pendingStore.lastAssistantText = ""; // reset so this turn re-triggers on new text
      return;
    }

    // ── Clean PAM/TITLE tokens from the visible response ──────────────
    // Streaming sites (Claude, ChatGPT, Gemini) re-render DOM nodes
    // asynchronously after the response stream completes.  We schedule
    // multiple cleanup passes at staggered delays to catch re-renders.
    //
    // IMPORTANT: Scope every pass to assistant nodes in the current turn.
    // Mutating the conversation container can damage prior messages or its
    // framework-owned response wrapper. Use non-conservative mode (direct
    // text node surgery) for those response nodes — NOT CSS hiding. The CSS-hiding approach
    // (hideReactPamBlocks) doesn't reliably work on response elements:
    // it targets specific element structures that don't match response
    // paragraphs.  Direct text node surgery is safe here because React
    // has finished reconciling after the stream completes.
    if (parsed.hasTokens || parsed.hadArtifacts) {
      const cleanupDelays = [200, 800, 2000, 4000, 8000];

      function doPamCleanup(passLabel) {
        try {
          let cleaned = false;
          for (const responseElement of cleanupTargets) {
            if (!responseElement || responseElement.isConnected === false) continue;
            const responseText = responseElement.innerText || responseElement.textContent || "";
            if (/\|STPAM\||\|MIDPAM\||\|ENDPAM\||\|TITLE\|/i.test(responseText)) {
              surgicalRemovePamTokens(responseElement, parsed, false);
            }
          }
        } catch (_) {
          // non-fatal
        }
      }

      // Immediate pass
      doPamCleanup("immediate");
      // Staggered passes to handle framework re-renders
      cleanupDelays.forEach((delay) => {
        setTimeout(() => doPamCleanup("delay-" + delay + "ms"), delay);
      });
    }

    if (STATE.pendingStore === pendingStore) STATE.pendingStore = null;
  }

  // ── Shadow DOM-aware MutationObservers ──────────────────────────────
  // The standard MutationObserver on document.body cannot see mutations
  // inside shadow roots.  This function attaches observers to every
  // open shadow root so that leaked enriched-prompt text is detected
  // and masked as soon as it appears in a user message bubble.
  // Uses text-based heuristics (pendingUserQueue content, control
  // artifact regex) instead of fragile CSS selectors that Google
  // rotates frequently.
  function startShadowMutationObservers(adapter) {
    if (!adapter.isShadowDom) return;
    const siteId = adapter && adapter.siteId ? adapter.siteId : ""; // kept for debug logging

    const observedRoots = new WeakSet();

    function checkLeakedNode(node) {
      if (!node || !node.isConnected) return;
      if (isComposerElement(node)) return;
      if (isShadowMaskUnsafeTarget(node)) return;

      const text = node.innerText || node.textContent || "";
      if (!text || text.length < 30) return;
      if (!containsControlArtifacts(text) && !hasStrongSystemLeak(text)) return;

      // ── Gemini PAM-token guard ──────────────────────────────────────
      // If this element contains PAM tokens (|STPAM|, |MIDPAM|, |ENDPAM|)
      // and we have an active pendingStore (waiting to store memory),
      // this is an ASSISTANT response being streamed — do NOT mask the
      // PAM tokens yet.  finalizeAssistant needs to read them first.
      // System prompt leaks (<System - ...) are still masked immediately.
      // This guard only fires on Shadow DOM sites (Gemini, NotebookLM).
      if (STATE.pendingStore && /\|STPAM\||\|MIDPAM\||\|ENDPAM\|/i.test(text)) {
        // Check if this looks like an assistant response (not a user bubble)
        // User bubbles have the enriched prompt with <System - ...> tags.
        // Assistant responses have PAM tokens but usually no <System - tags.
        if (!hasStrongSystemLeak(text)) {
          return; // Skip — let finalizeAssistant handle this
        }
        // If it has BOTH system leaks AND PAM tokens, it's the user bubble
        // echoing the enriched prompt — continue with masking.
      }

      // Determine what the user actually typed from the pending queue
      const qItem = STATE.pendingUserQueue.length > 0 ? STATE.pendingUserQueue[0] : null;
      const displayText = (qItem && qItem.originalText) ? qItem.originalText : extractUserPrompt(text);
      if (!displayText) return;
      if (normalizeText(displayText) === normalizeText(text)) return;

      // Use the adapter's replaceMessageText (handles shadow DOM traversal)
      const didReplace = replaceMessageText(adapter, node, displayText);

      if (didReplace) {
        debugIo("UI MASK (shadow-observer)", {
          element: elementBrief(node),
          textPreview: text.slice(0, 120),
          replacedWith: displayText.slice(0, 80),
          siteId
        });
        // If this matches the first pending queue item, remove it
        if (qItem && qItem.originalText && normalizeText(displayText) === normalizeText(qItem.originalText)) {
          removePendingUserItem(qItem, false);
        }
      }
    }

    function observeRoot(root) {
      if (!root || observedRoots.has(root)) return;
      observedRoots.add(root);

      const obs = new MutationObserver((mutations) => {
        if (adapter.invalidateCache) adapter.invalidateCache();

        for (const m of mutations) {
          // Check added element nodes
          if (m.addedNodes && m.addedNodes.length) {
            for (const node of m.addedNodes) {
              if (!(node instanceof HTMLElement)) continue;
              checkLeakedNode(node);
              // Also check children (Lit may add a subtree at once)
              if (node.querySelectorAll) {
                try {
                  const children = node.querySelectorAll("*");
                  for (const child of children) {
                    checkLeakedNode(child);
                    // Observe any new shadow roots that appeared
                    if (child.shadowRoot) observeRoot(child.shadowRoot);
                  }
                } catch (_) { }
              }
              // Observe shadow root of the newly added element itself
              if (node.shadowRoot) observeRoot(node.shadowRoot);
            }
          }
          // Check characterData changes (text node content updates by Lit)
          if (m.type === "characterData" && m.target && m.target.parentElement) {
            checkLeakedNode(m.target.parentElement);
          }
        }
      });

      obs.observe(root, { childList: true, subtree: true, characterData: true });

      // Recurse into existing nested shadow roots
      if (root.querySelectorAll) {
        try {
          for (const el of root.querySelectorAll("*")) {
            if (el.shadowRoot) observeRoot(el.shadowRoot);
          }
        } catch (_) { }
      }
    }

    // Initial scan: observe all currently existing shadow roots
    const initialRoots = collectSearchRoots(document);
    for (const root of initialRoots) {
      observeRoot(root);
    }

    // Re-scan periodically for lazily created shadow roots
    // (Angular/Lit create new components as the user navigates conversations)
    setInterval(() => {
      const freshRoots = collectSearchRoots(document);
      for (const root of freshRoots) {
        observeRoot(root);
      }
    }, 3000);
  }

  /**
   * Mask all user-message elements that contain leaked enriched prompts.
   * Uses the site adapter's targeted selectors (NOT a broad "*"), so it can
   * only touch elements the adapter explicitly identifies as user messages.
   * This is the F5 / hard-reload safety net: the MutationObserver in
   * startUserMessageObserver fires only on mutations AFTER attach, so
   * server-side-hydrated history would otherwise stay unmasked.
   * Idempotent — applyReactSafeOverlay early-returns when overlay exists.
   */
  function maskAllUserMessages(adapter) {
    if (!adapter || !adapter.getUserMessageElements) return;
    const userMsgs = adapter.getUserMessageElements() || [];

    const detectLeak = (t) => !!t && (
      hasStrongSystemLeak(t)
      || containsControlArtifacts(t)
      || (/\bSTPAM\b/.test(t) && /\bMIDPAM\b/.test(t) && /\bENDPAM\b/.test(t))
    );

    // F5/hard-reload extraction guard: on first paint after refresh there is
    // no pendingStore (live-send path is bypassed), so maskAllUserMessages
    // extracts the source text directly from the bubble. Adapters can override
    // extractSourceText() to apply site-specific extraction logic.
    // Source text extraction: adapters can override extractSourceText() for
    // site-specific behaviour (e.g. ChatGPT excludes sibling \"Show more\" labels
    // that appear after the enriched text in the wrapper's innerText).
    const extractSource = (elNode) => {
      if (adapter.extractSourceText) return adapter.extractSourceText(elNode);
      return adapter.extractMessageText ? adapter.extractMessageText(elNode) : (elNode && elNode.innerText) || "";
    };

    for (const el of userMsgs) {
      if (!el) continue;
      const visibleText = extractSource(el);

      let sourceText = visibleText || "";
      let isLeak = detectLeak(sourceText);

      // Self-heal via hidden source-of-truth: React-managed children we
      // previously marked with data-bdbm-react-hidden retain the verbatim
      // enriched prompt in their textContent. Always inspect them when the
      // visible text shows no leak markers — the visible side could be:
      //   (a) whitespace/empty (transitory partial render before user text
      //       was appended; previous mask ran with empty source)
      //   (b) stale text from a previous conversation (Claude reuses the
      //       same bubble element across sidebar navigation)
      //   (c) already-correctly-masked clean user text (we skip via the
      //       normalized comparison below)
      // We MUST NOT gate this on overlay-is-whitespace — that gate misses
      // case (b) and any case where the overlay ended up with non-stripped
      // residue. Comparing extracted-vs-visible at the end is the only
      // correct skip condition.
      if (!isLeak) {
        const hiddenChildren = el.querySelectorAll("[data-bdbm-react-hidden]");
        for (const h of hiddenChildren) {
          const t = h.textContent || "";
          if (detectLeak(t)) {
            sourceText = t;
            isLeak = true;
            break;
          }
        }
      }

      if (!isLeak) continue;

      const cleaned = extractUserPrompt(sourceText);
      // Never mask with empty/whitespace — that would freeze the bubble blank
      // (subsequent sweeps see "clean" innerText and never re-process).
      if (!cleaned || !cleaned.trim()) continue;

      // Choose the comparison reference carefully. innerText of the bubble
      // wrapper can include SIBLING UI labels that sit alongside our overlay
      // (notably ChatGPT's "Show more" / "Show less" toggle text). Comparing
      // against that yields a false mismatch and triggers a redundant re-mask
      // every 2s sweep, which perturbs the bubble layout. Instead, prefer the
      // existing overlay's textContent as the authoritative "what we masked
      // it to" reference. Fall back to visibleText only when:
      //   - no overlay exists yet (first mask), OR
      //   - the visible bubble itself shows a leak (CSS-hide failed, or this
      //     is the pre-mask render with raw enriched prompt visible).
      const visibleIsLeaked = detectLeak(visibleText || "");
      // Overlay lookup: direct-child first (Claude / older code path),
      // descendant fallback for ChatGPT where the overlay now sits inside
      // the bubble wrapper (one level deeper than the role div). The
      // fallback is safe — user-message divs do not nest other user
      // messages, so the only .bdbm-overlay-text inside is our own.
      const overlayEl = el.querySelector(":scope > .bdbm-overlay-text")
        || el.querySelector(".bdbm-overlay-text");
      const refText = (!visibleIsLeaked && overlayEl)
        ? (overlayEl.textContent || "")
        : (visibleText || "");

      if (normalizeText(cleaned) !== normalizeText(refText)) {
        replaceMessageText(adapter, el, cleaned);
      }
    }
  }

  function startHistoryPamSweep(adapter) {
    // Periodically sweep the DOM for PAM tokens in history conversations.
    // Shadow DOM sites (Gemini): use conservative (CSS-hiding) mode so that
    // Lit re-hydration after page refresh cannot restore text nodes that were
    // mutated by direct surgery, which would make PAM token text reappear.
    const useConservative = !!(adapter.isReactSite || adapter.isShadowDom);

    // One-time scan shortly after init catches user messages that were
    // server-side-hydrated before our observers attached (F5 / hard reload).
    setTimeout(() => {
      if (!STATE.pendingStore && adapter.isReactSite) maskAllUserMessages(adapter);
    }, 1500);

    setInterval(() => {
      // Don't interfere while a live stream is happening
      if (STATE.pendingStore) return;

      const container = adapter.getMessageContainer ? adapter.getMessageContainer() : document.body;
      if (!container) return;

      const containerText = container.innerText || "";
      if (/\|STPAM\||\|MIDPAM\||\|ENDPAM\||\|TITLE\|/i.test(containerText)) {
        surgicalRemovePamTokens(container, null, useConservative);
      }

      // F5 safety net for React sites: re-process any user messages that
      // still carry a leak. Targeted via adapter selectors only, so the
      // broad "*" fallback used by sanitizeAllVisibleLeakNodes is avoided.
      if (adapter.isReactSite) maskAllUserMessages(adapter);
    }, 2000);
  }

  async function init(adapter) {
    STATE.adapter = adapter;
    STATE.config = await loadConfig();
    if (!STATE.config) return;

    const enabled = STATE.config.sites && STATE.config.sites[adapter.siteId];
    if (!enabled) return;

    STATE.memoryEnabled = !!STATE.config.memoryEnabled;

    await createPanel();

    STATE.learnedSelectors = await loadLearnedSelectors();
    const wrappedAdapter = wrapAdapter(adapter);
    STATE.adapter = wrappedAdapter;

    STATE.client = new window.biomemClient({
      wsUrl: STATE.config.bdbmWsUrl,
      httpUrl: STATE.config.bdbmHttpUrl
    });

    STATE.client.onDisconnect = (code, reason) => {
      if (STATE.connected) {
        STATE.connected = false;
        updatePanel(false);
        log(`biomem disconnected (${code}: ${reason}), will auto-reconnect or fast wake-up.`);
        startReconnectLoop(wrappedAdapter);
      }
    };

    STATE.client.onConnect = () => {
      if (!STATE.connected) {
        STATE.connected = true;
        updatePanel(true);
        log("biomem reconnected via event");
      }
    };

    window.addEventListener("focus", async () => {
      if (!STATE.connected && STATE.client) {
        try {
          await STATE.client.connect();
          STATE.connected = true;
          log("biomem fast wake-up reconnected (focus)");
          updatePanel(true);
        } catch (_) { }
      }
    });

    document.addEventListener("visibilitychange", async () => {
      if (document.visibilityState === "visible" && !STATE.connected && STATE.client) {
        try {
          await STATE.client.connect();
          STATE.connected = true;
          log("biomem fast wake-up reconnected (visibility)");
          updatePanel(true);
        } catch (_) { }
      }
    });

    try {
      await STATE.client.connect();
      STATE.connected = true;
    } catch (err) {
      STATE.connected = false;
      log(`connect failed: ${err.message}`);
    }
    updatePanel(STATE.connected);

    // Attach all DOM hooks immediately — even if not yet connected.
    // Every code path that makes a network call checks STATE.connected,
    // so it is safe to attach observers
    // and send hooks while offline. They simply stay dormant until the
    // connection comes back.
    const input = wrappedAdapter.findInput ? wrappedAdapter.findInput() : null;
    const sendBtn = wrappedAdapter.findSendButton ? wrappedAdapter.findSendButton() : null;
    attachSendHooks(wrappedAdapter, input, sendBtn);
    startUserMessageObserver(wrappedAdapter);
    startAssistantObserver(wrappedAdapter);
    startDeepLeakSweep(wrappedAdapter);
    startShadowMutationObservers(wrappedAdapter);
    startHistoryPamSweep(wrappedAdapter);
    STATE.hooksSetUp = true;

    // Auto-reconnect: if SW was not running at page load, retry every 5 s.
    if (!STATE.connected) {
      startReconnectLoop(wrappedAdapter);
    }
  }

  // Patch adapter lookups for learned selectors
  function wrapAdapter(adapter) {
    const wrapped = { ...adapter };
    wrapped.findInput = () => {
      const learned = getLearnedForHost();
      if (learned && learned.inputSelector) {
        const el = document.querySelector(learned.inputSelector);
        if (el) return el;
      }
      const fromAdapter = adapter.findInput ? adapter.findInput() : null;
      if (fromAdapter) return fromAdapter;
      return findHeuristicInput();
    };
    wrapped.getMessageContainer = () => {
      const learned = getLearnedForHost();
      if (learned && learned.containerSelector) {
        const el = document.querySelector(learned.containerSelector);
        if (el) return el;
      }
      return adapter.getMessageContainer ? adapter.getMessageContainer() : findHeuristicContainer();
    };
    const learned = getLearnedForHost();
    if (learned && learned.assistantSelector) {
      wrapped.getAssistantMessageElements = () => Array.from(document.querySelectorAll(learned.assistantSelector));
      wrapped.getLastAssistantMessageElement = () => {
        const els = wrapped.getAssistantMessageElements();
        return els.length ? els[els.length - 1] : null;
      };
    }
    return wrapped;
  }

  /**
   * Auto-reconnect loop: when the SW was not running at page load (or the
   * WebSocket dropped), retry every 5 seconds until connection is restored.
   * On first successful reconnect also re-verifies the API key and — if hooks
   * were not yet set up — attaches them.
   */
  function startReconnectLoop(wrappedAdapter) {
    if (STATE._reconnectTimer) return;
    const INTERVAL_MS = 5000;
    STATE._reconnectTimer = setInterval(async () => {
      if (STATE.connected) {
        clearInterval(STATE._reconnectTimer);
        STATE._reconnectTimer = null;
        return;
      }
      try {
        await STATE.client.connect();
        STATE.connected = true;
        log("biomem reconnected");
        updatePanel(true);
        clearInterval(STATE._reconnectTimer);
        STATE._reconnectTimer = null;

        if (!STATE.hooksSetUp) {
          const input = wrappedAdapter.findInput ? wrappedAdapter.findInput() : null;
          const sendBtn = wrappedAdapter.findSendButton ? wrappedAdapter.findSendButton() : null;
          attachSendHooks(wrappedAdapter, input, sendBtn);
          startUserMessageObserver(wrappedAdapter);
          startAssistantObserver(wrappedAdapter);
          startDeepLeakSweep(wrappedAdapter);
          startShadowMutationObservers(wrappedAdapter);
          startHistoryPamSweep(wrappedAdapter);
          STATE.hooksSetUp = true;
        }
      } catch (_) {
        updatePanel(false);
      }
    }, INTERVAL_MS);
  }

  window.biomemInjector = {
    init(adapter) {
      init(adapter);
    }
  };

})();
