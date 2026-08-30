function formatMemoryLine(m) {
  const userContent = m.user || "[no user]";
  const modelContent = m.model || "[no model]";
  const td = typeof m.turn_distance === "number" ? m.turn_distance : "?";
  const conf = typeof m.confidence === "number" ? m.confidence : "?";
  return `User: ${userContent} | Model: ${modelContent} | Turn distance: ${td} | Confidence: ${conf}`;
}

function getTimestamp() {
  const now = new Date();
  const yyyy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  const hh = String(now.getHours()).padStart(2, "0");
  const min = String(now.getMinutes()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd} ${hh}:${min}`;
}

function buildEnrichedPrompt(options) {
  const userText = options.userText || "";
  const memories = options.memories || [];
  const isFirstTurn = !!options.isFirstTurn;

  let systemPrompt = "<user_context>\n";
  systemPrompt += `<current_time>${getTimestamp()}</current_time>\n\n`;

  if (memories.length > 0) {
    const memoryContext = memories.map(formatMemoryLine).join("\n");
    systemPrompt += `<relevant_memories>\n${memoryContext}\n</relevant_memories>\n\n`;
  }

  systemPrompt += `<response_format>\n` +
    `For my personal memory and note-taking system, please include TWO concise summaries at the very end of your response:\n` +
    `1. Summary of my query (semantic keywords, main intent, key concepts).\n` +
    `2. Summary of your response (key points, suggestions, actions, important details).\n` +
    `Note: Write these summaries in the same language as our conversation.\n` +
    `Format (strictly at the very end on a new line):\n` +
    `|STPAM| [summary of user query] |MIDPAM| [summary of response] |ENDPAM|\n` +
    `</response_format>\n` +
    `</user_context>\n\n`;

  const userPrompt = userText;
  const combinedPrompt = `${systemPrompt}${userPrompt}`;

  return { systemPrompt, userPrompt, combinedPrompt };
}

function containsControlArtifacts(text) {
  if (!text) return false;
  return /<user_context|<\/user_context>|<System\s*-|<\/System\s*-|\|STPAM\||\|MIDPAM\||\|ENDPAM\||\|MEMQUERY\||\|ENDQUERY\||\|TITLE\|/i.test(text);
}

function normalizeDisplayWhitespace(text) {
  return (text || "")
    .replace(/\r\n/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function stripSystemArtifacts(text) {
  let cleaned = text || "";
  cleaned = cleaned.replace(/<user_context>[\s\S]*?<\/user_context>\s*/gi, "\n");
  cleaned = cleaned.replace(/<\/?(?:user_context|current_time|relevant_memories|response_format)[^>]*>/gi, "\n");
  cleaned = cleaned.replace(/<System\s*-\s*[^>\n]*>[\s\S]*?<\/System\s*-\s*[^>]*>\s*/gi, "\n");
  cleaned = cleaned.replace(/<System\s*-\s*[\s\S]*?>\s*(?:\r?\n){1,2}/gi, "\n");
  cleaned = cleaned.replace(/<\/System\s*-\s*[^>]*>/gi, "\n");
  cleaned = cleaned.replace(/\|MEMQUERY\|[\s\S]*?\|ENDQUERY\|/gi, " ");
  cleaned = cleaned.replace(/\|(?:STPAM|MIDPAM|ENDPAM|MEMQUERY|ENDQUERY)\|/gi, " ");
  cleaned = cleaned.replace(/\|TITLE\|[^\n\r]*/gi, " ");

  const lines = cleaned.split(/\r?\n/).filter((line) => {
    const t = line.trim();
    if (!t) return true;
    if (/^<\/?(?:user_context|current_time|relevant_memories|response_format)/i.test(t)) return false;
    if (/^<?\/?System\s*-/i.test(t)) return false;
    if (/^For my personal memory and note-taking/i.test(t)) return false;
    if (/^Note:\s*Write these summaries in the same language/i.test(t)) return false;
    if (/^\d+\.\s*Summary of (?:my query|the USER'S QUERY)/i.test(t)) return false;
    if (/^\d+\.\s*Summary of (?:your response|YOUR RESPONSE)/i.test(t)) return false;
    if (/^IMPORTANT:\s*Write these summaries in the same language the user is using\./i.test(t)) return false;
    if (/^Format(?:\s*\(strictly at the very end on a new line\))?:\s*\|STPAM\|/i.test(t)) return false;
    if (/^Format:\s*/i.test(t) && /summary of user query|summary of response/i.test(t)) return false;
    if (/^STRICT RULE:\s*The summaries must be the LAST thing in your output\./i.test(t)) return false;
    return true;
  });

  return normalizeDisplayWhitespace(lines.join("\n"));
}

function extractUserPrompt(text) {
  let cleaned = text || "";

  // Fast path for <user_context>: user text is everything after closing </user_context>
  const userCtxCloseIdx = cleaned.lastIndexOf("</user_context>");
  if (userCtxCloseIdx !== -1) {
    const afterCtx = cleaned.slice(userCtxCloseIdx + 15).trim(); // 15 = "</user_context>".length
    if (afterCtx) return afterCtx;
  }

  // Fast path: the legacy enriched format ends with "Format: |STPAM|...|ENDPAM|>USER_TEXT"
  // (user text is appended directly after the closing ">" with no newline separator).
  // Find the last |ENDPAM|> and return everything after it as the user's original message.
  const endpamIdx = cleaned.lastIndexOf("|ENDPAM|>");
  if (endpamIdx !== -1) {
    const afterEndpam = cleaned.slice(endpamIdx + 9).trim(); // 9 = "|ENDPAM|>".length
    if (afterEndpam) return afterEndpam;
  }

  // Fallback for Claude: when Claude renders the prompt from history, it parses the PAM
  // format into a Markdown table, which consumes the | characters and replaces <System> tags with ※.
  const tableMatch = cleaned.match(/ENDPAM(?:\||>|\s)*([\s\S]*)$/i);
  if (tableMatch) {
    const afterEndpam = tableMatch[1].trim();
    if (afterEndpam) return afterEndpam;
  }

  for (let i = 0; i < 8; i += 1) {
    const withClosedBlocks = cleaned.replace(/^\s*<user_context>[\s\S]*?<\/user_context>\s*/i, "")
      .replace(/^\s*<System\s*-\s*[^>\n]*>[\s\S]*?<\/System\s*-\s*[^>]*>\s*/i, "");
    const withPseudoBlocks = withClosedBlocks.replace(/^\s*<System\s*-\s*[\s\S]*?>\s*(?:\r?\n){1,2}\s*/i, "");
    const next = withPseudoBlocks.replace(/^\s*<System\s*-[^\n]*(?:\r?\n)?\s*/i, "");
    if (next === cleaned) break;
    cleaned = next;
  }
  cleaned = cleaned.replace(/^\s*<\/System\s*-\s*[^>]*>\s*/i, "");
  cleaned = cleaned.replace(/^\s*Format:\s*\|STPAM\|[^\n]*(?:\r?\n)?/i, "");
  cleaned = stripSystemArtifacts(cleaned);
  // Reject whitespace-only result. When invoked during a transitory partial
  // render (e.g., the user-message div has the first <System> tag but no
  // user text appended yet), stripping the System block leaves only newlines.
  // Returning that whitespace would cause callers to mask the bubble with an
  // empty overlay — and the bubble stays empty forever because the next
  // sweep sees no leak markers anymore (the overlay hides them).
  if (cleaned && cleaned.trim()) return cleaned;
  const finalFallback = stripSystemArtifacts(text || "");
  if (finalFallback && finalFallback.trim()) return finalFallback;
  return "";
}

function parsePamTokens(responseText) {
  const originalText = responseText || "";
  let text = originalText;
  let userSummary = null;
  let modelSummary = null;
  let threadTitle = null;
  let hasTokens = false;

  const pamMatches = Array.from(text.matchAll(/\|STPAM\|([\s\S]*?)\|ENDPAM\|/gi));
  if (pamMatches.length > 0) {
    hasTokens = true;
    const pamBlock = pamMatches[pamMatches.length - 1][1].trim();
    const midpam = pamBlock.indexOf("|MIDPAM|");
    if (midpam !== -1) {
      userSummary = pamBlock.substring(0, midpam).trim();
      modelSummary = pamBlock.substring(midpam + 8).trim();
    } else {
      modelSummary = pamBlock.trim();
    }
    text = text.replace(/\|STPAM\|[\s\S]*?\|ENDPAM\|/gi, " ");
  }

  const titleMatch = text.match(/\|TITLE\|\s*([^\n\r]+)/i);
  if (titleMatch) {
    threadTitle = titleMatch[1].trim();
    threadTitle = threadTitle.replace(/^["'\\[\\]{}()*]+|["'\\[\\]{}()*]+$/g, "");
    if (threadTitle.length > 50) threadTitle = threadTitle.substring(0, 50) + "...";
    text = text.replace(/\|TITLE\|[^\n\r]*/gi, " ");
  }

  text = stripSystemArtifacts(text);
  const hadArtifacts = hasTokens || containsControlArtifacts(originalText) || normalizeDisplayWhitespace(originalText) !== text;
  return { displayText: text, userSummary, modelSummary, hasTokens, threadTitle, hadArtifacts };
}

window.BdbmPromptBuilder = {
  buildEnrichedPrompt,
  parsePamTokens,
  containsControlArtifacts,
  stripSystemArtifacts,
  extractUserPrompt
};
