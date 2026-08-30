"""
Conversation Handler — local 8-step conversation flow.

Ported from conversation.js to Python for full integration with the PyQt6 GUI.

Flow:
  Input → biomem Retrieve → Build Prompt → LLM → Parse PAM → Display → biomem Store → Unlock

Supports:
  - Associative Recall (default)
  - Deep Recall (optional 2-round cycle with biomem MEMQUERY)
  - Web Search toggle
  - Thread management (ThreadStore → SQLite + AES-256-GCM)
  - First-Run Wizard trigger
"""
import asyncio
import base64
import logging
import random
import re
import string
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger('bdbm.conv_handler')

# ---------------------------------------------------------------------------
# PAM (Personalised Adaptive Memory) protocol tokens
# ---------------------------------------------------------------------------
_TOKEN_STPAM = '|STPAM|'
_TOKEN_MIDPAM = '|MIDPAM|'
_TOKEN_ENDPAM = '|ENDPAM|'
_TOKEN_TITLE = '|TITLE|'


class ConversationHandler(QObject):
    """
    Orchestrates the complete conversation cycle.

    Signals are emitted from the asyncio thread and Qt delivers them
    to the main thread automatically (queued connection).
    """

    #: PyQt signals (Qt delivers asynchronously)
    first_run = pyqtSignal()
    user_message = pyqtSignal(str, list)
    model_message = pyqtSignal(str)
    system_message = pyqtSignal(str, str)
    thinking_bubble = pyqtSignal(str)
    thinking_update = pyqtSignal(str)
    thinking_hide = pyqtSignal()
    busy_changed = pyqtSignal(bool)
    history_updated = pyqtSignal(list)
    pam_token_warning = pyqtSignal()

    _IMAGE_EXTS = {'.bmp', '.png', '.webp', '.jpeg', '.jpg', '.gif'}
    _IMAGE_MIME = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp',
    }
    _TEXT_EXTS = {
        '.ts', '.js', '.json', '.ini', '.md', '.xml', '.htm',
        '.toml', '.py', '.csv', '.txt', '.yaml', '.html',
    }

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __init__(self, command_handler, llm_client, settings_manager, thread_store, parent=None):
        super().__init__(parent)
        self._handler = command_handler
        self._llm = llm_client
        self._settings = settings_manager
        self._settings_manager = settings_manager
        self._threads = thread_store
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.is_busy: bool = False
        self.current_thread_id: str = self._generate_session_id()
        self.session_history: List[Dict] = []
        self._current_attachments: List[str] = []
        self._init_active_thread()

    def set_command_handler(self, handler):
        """Sets the CommandHandler (server protocol) — called after startup."""
        self._handler = handler

    def set_async_loop(self, loop: asyncio.AbstractEventLoop):
        """Sets the asyncio loop the server background runs on (change is thread-safe)."""
        self._loop = loop

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _get_timestamp() -> str:
        return datetime.now().strftime('%Y-%m-%d %H:%M')

    @staticmethod
    def _generate_session_id() -> str:
        """Random short session identifier (9 chars from [a-z0-9])."""
        chars = string.ascii_lowercase + string.digits
        return ''.join(random.choice(chars) for _ in range(9))

    @staticmethod
    def _format_memory(mem: Dict) -> str:
        """Formats one recalled memory for the prompt."""
        user = mem.get('user', '[no user content]')
        model = mem.get('model', '[no model content]')
        return ''.join((
            'User: ', str(user),
            ' | Model: ', str(model),
            ' | Turn distance: ', str(mem.get('turn_distance', 0)),
            ' | Confidence: ', str(mem.get('confidence', 0)),
        ))

    @staticmethod
    def _combine_prompts(prompts: Dict) -> str:
        """Combines systemPrompt + userPrompt into one string (corporate LLM)."""
        sys_prompt = prompts.get('systemPrompt', '')
        user_prompt = prompts.get('userPrompt', '')
        return (sys_prompt + '\n' + user_prompt).strip()

    @staticmethod
    def _split_prompts(prompts: Dict, model: str):
        """
        Return (user_prompt, system_prompt).
        For Ollama: keep system and user separate so LLMClient can send a proper system role.
        For corporate LLMs: combine into one string (system role not used
        matches JS behaviour).
        """
        sys_prompt = prompts.get('systemPrompt', '')
        user_prompt = prompts.get('userPrompt', '')
        if model == 'ollama':
            return (user_prompt, sys_prompt)
        return (ConversationHandler._combine_prompts(prompts), '')

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------
    def _get_max_associations(self) -> int:
        """Maximum number of memory associations (min 3, max 10, default 5)."""
        try:
            max_assoc = self._settings_manager.get_max_associations()
        except Exception:
            max_assoc = 5
        if not isinstance(max_assoc, int):
            max_assoc = 5
        return max(3, min(10, max_assoc))

    def _build_enriched_prompt(self, user_text: str, memories: List[Dict], model: str,
                               deep_recall: bool = False,
                               deep_recall_round2: Optional[Dict] = None,
                               use_web_search: bool = False) -> Dict[str, str]:
        context_limit = 250
        personalisation = ''
        try:
            context_limit = self._settings.get_context_limit(model)
        except Exception:
            pass
        try:
            personalisation = self._settings.get_personalisation(model)
        except Exception:
            pass
        if model == 'ollama':
            return self._build_ollama_prompt(
                user_text, memories, context_limit, personalisation,
                deep_recall, deep_recall_round2,
            )
        return self._build_corporate_prompt(
            user_text, memories, context_limit, personalisation, model,
            deep_recall, deep_recall_round2, use_web_search,
        )

    def _build_corporate_prompt(self, user_text, memories, context_limit,
                                personalisation, model, deep_recall,
                                deep_recall_round2, use_web_search) -> Dict[str, str]:
        """Builds the prompt for a corporate LLM (ChatGPT/Gemini/Claude)."""
        sys_prompt = ''

        # --- User personalisation -------------------------------------
        if personalisation:
            sys_prompt += 'You have access to biomem (BDBM memory).\n'
            sys_prompt += '</System - User Personalisation>\n\n'
            sys_prompt += '<System - User Personalisation>\n'
            sys_prompt += personalisation
            sys_prompt += '\n</System - User Personalisation>\n\n'
        else:
            sys_prompt += 'You have access to biomem (BDBM memory).\n'
            sys_prompt += '</System - User Personalisation>\n\n'
            sys_prompt += '<System - User Personalisation>\n'
            sys_prompt += '{} is not configured. Go to LLM Settings.'.format(model)
            sys_prompt += '\n</System - User Personalisation>\n\n'

        # --- Current date ----------------------------------------------
        sys_prompt += '## CURRENT DATE AND TIME\n'
        sys_prompt += '<System - Current Date and Time>\n'
        sys_prompt += self._get_timestamp()
        sys_prompt += '\n</System - Current Date and Time>\n\n'

        # --- Web search notice -----------------------------------------
        if use_web_search:
            sys_prompt += '<System - Web Search Enabled>\n'
            sys_prompt += (
                'You have access to a web search tool. If the user asks for up-to-date '
                'information, news, current events, or facts you are not completely '
                'certain about, you MUST use the web search tool to find the most '
                'accurate and current answer. Always try to ground your response in '
                'recent search results when applicable.\n'
            )
            sys_prompt += '</System - Web Search Enabled>\n\n'

        # --- Recent conversation history -------------------------------
        history = self._build_history_block(context_limit)
        if history:
            sys_prompt += '<System - Recent Conversation History (last ~'
            sys_prompt += str(context_limit)
            sys_prompt += ' words max)>\n'
            sys_prompt += history
            sys_prompt += '\n</System - Recent Conversation History>\n\n'

        # --- Associated memory context ---------------------------------
        mem_text = self._build_memory_block(memories, deep_recall_round2)
        if mem_text:
            sys_prompt += '<System - associated memory context (The assessment of which '
            sys_prompt += 'memories are useful depends on the situation and overall '
            sys_prompt += 'context, therefore you have the freedom to decide which '
            sys_prompt += 'memories to use and which to discard. Actively seek '
            sys_prompt += 'connections between different memories — combining partial '
            sys_prompt += 'pieces of information can create a complete picture.):\n'
            sys_prompt += mem_text
            sys_prompt += '\n</System - associated memory context>'
            sys_prompt += '\n\n'

        # --- Deep recall blocks -----------------------------------------
        if deep_recall and deep_recall_round2 is None:
            sys_prompt += '<System - Deep Recall Mode Active>\n'
            sys_prompt += (
                'You may perform ONE targeted memory query if the associated '
                'memories above are insufficient.\n\n'
                'HOW THE MEMORY SYSTEM WORKS:\n'
                'The memory system indexes the USER\'S past statements — not your '
                'responses, not the timestamps, not the turn distance.\n'
                'It retrieves memories by semantic similarity to the query text.\n'
                'Therefore: write the query AS IF YOU WERE THE USER speaking about '
                'themselves,\n'
                'using the same language and vocabulary the user uses when talking '
                'to you.\n\n'
                'HOW TO FORMULATE THE QUERY:\n'
                '- Use keyword probes in first person — short, open-ended fragments '
                'ending with \'?\'\n'
                '- Combine MULTIPLE probes in one query to maximize recall coverage.\n'
                '  More keyword probes = richer semantic vector = better memory '
                'retrieval.\n'
                '- Use the same language the user uses (if they write in Czech, query '
                'in Czech, etc.)\n\n'
                'GOOD queries (combined probes, user\'s voice):\n'
                '  "My name is? I work as? I study? My background is?"\n'
                '  "I\'m working on? My project is? My role is?"\n'
                '  "I live in? I like? I\'m interested in?"\n\n'
                'BAD queries (model\'s perspective, generic phrases — will NOT match '
                'user\'s past statements):\n'
                '  "What do I know about the user?"\n'
                '  "User\'s personal information"\n'
                '  "Tell me everything about this person"\n\n'
                'HOW TO USE:\n'
                '1. Write a brief acknowledgment (max 1 sentence, e.g. "Let me think '
                'about that...").\n'
                '2. Immediately follow with: |MEMQUERY| [your query using the rules '
                'above] |ENDQUERY|\n'
                '3. Stop. The system retrieves memories and prompts you again for the '
                'full response.\n\n'
                'If associated memories are already sufficient — skip the query and '
                'respond directly.\n'
                'STRICT RULES:\n'
                '1. Maximum ONE |MEMQUERY| token. Never use it more than once.\n'
                '2. If you USE |MEMQUERY|: do NOT include |STPAM|, |MIDPAM|, or '
                '|ENDPAM| tokens.\n'
                '   Those are reserved for your FINAL response (which comes in the '
                'next round).\n'
                '3. If you SKIP the query and answer directly: include '
                '|STPAM|...|ENDPAM| as normal\n'
                '   (because your direct answer IS the final response).\n'
            )
            sys_prompt += '</System - Deep Recall Mode Active>\n'

        if deep_recall and deep_recall_round2 is not None:
            results = deep_recall_round2.get('results', []) or []
            mem_text_2 = self._build_memory_block(results, None)
            sys_prompt += '<System - Deep Recall Results>\n'
            sys_prompt += 'Your targeted memory query returned the following results:\n'
            sys_prompt += mem_text_2 or 'No additional memories found for this query.'
            sys_prompt += '\n</System - Deep Recall Results>\n'
            sys_prompt += 'IMPORTANT: Do NOT include another |MEMQUERY| token. Provide '
            sys_prompt += 'your complete final response now.\n'
            sys_prompt += 'Include |STPAM|...|ENDPAM| summary tokens at the end as '
            sys_prompt += 'instructed.'
            sys_prompt += '\n'

        # --- Title reminder (thread) -------------------------------------
        if not self.session_history:
            sys_prompt += '<System - New Conversation thread> Suggest a brief 2-5 word '
            sys_prompt += 'title based on the user\'s first query. Place it at the very '
            sys_prompt += 'end after |ENDPAM|, format: |TITLE| Your suggested title. '
            sys_prompt += 'Do not wrap it in any brackets.'

        # --- Additional PAM instruction -------------------------------
        sys_prompt += '<System - Additional instruction: Mandatory - include TWO '
        sys_prompt += 'concise summaries STRICTLY at the very end of your response:\n'
        sys_prompt += '1. Summary of the USER\'S QUERY - semantic keywords, main '
        sys_prompt += 'intent, key concepts.\n'
        sys_prompt += '2. Summary of YOUR RESPONSE - key points, suggestions, '
        sys_prompt += 'actions.\n'
        sys_prompt += '3. The summaries you create are your own future memories. '
        sys_prompt += 'Phrase them with future usefulness and practical '
        sys_prompt += 'applicability in mind.\n'
        sys_prompt += 'IMPORTANT: Write these summaries in the same language the user '
        sys_prompt += 'is using.\n'
        sys_prompt += 'STRICT RULE: The summaries must be the LAST thing in your '
        sys_prompt += 'output.\n'
        sys_prompt += 'Format: |STPAM| [summary of user query] |MIDPAM| [summary of '
        sys_prompt += 'response] |ENDPAM|>'
        sys_prompt += '\n'

        user_prompt = 'User: ' + user_text
        if deep_recall_round2 is not None:
            user_prompt += '\n'
            user_prompt += 'You already started your response to the user with: "'
            user_prompt += deep_recall_round2.get('thinkingText', '')
            user_prompt += '"\nNow seamlessly continue and complete your response. '
            user_prompt += 'Do NOT repeat the opening phrase.'

        return {'systemPrompt': sys_prompt, 'userPrompt': user_prompt}

    def _build_ollama_prompt(self, user_text, memories, context_limit,
                             personalisation, deep_recall,
                             deep_recall_round2) -> Dict[str, str]:
        """Builds the enriched prompt for the Ollama model (system/user kept separate)."""
        sys_prompt = '## OUTPUT FORMAT (MANDATORY)\n'
        sys_prompt += (
            'Every response MUST end with this exact line (no exceptions, no markdown '
            'around it):\n'
            "|STPAM| <one-sentence summary of the user's question> |MIDPAM| "
            "<one-sentence summary of your answer> |ENDPAM|\n"
            "Use the user's language. Place it as the LAST line of your output.\n"
            'Do NOT skip these tokens. Do NOT translate them. Write them literally '
            'as shown.\n\n'
            "EXAMPLE — if the user asks 'What's the capital of France?', a correct "
            'response ends with:\n'
            'Paris is the capital of France.\n'
            "|STPAM| User asked about France's capital. |MIDPAM| Answered: Paris. "
            '|ENDPAM|\n'
        )
        sys_prompt += 'You have access to biomem (BDBM memory).\n'

        if personalisation:
            sys_prompt += '<System - User Personalisation>\n'
            sys_prompt += personalisation
            sys_prompt += '\n</System - User Personalisation>\n\n'
        else:
            sys_prompt += '<System - User Personalisation>\n'
            sys_prompt += 'Ollama is not configured. Go to LLM Settings.'
            sys_prompt += '\n</System - User Personalisation>\n\n'

        sys_prompt += '## CURRENT DATE AND TIME\n'
        sys_prompt += '<System - Current Date and Time>\n'
        sys_prompt += self._get_timestamp()
        sys_prompt += '\n</System - Current Date and Time>\n\n'

        history = self._build_history_block(context_limit)
        if history:
            sys_prompt += '<System - Recent Conversation History (last ~'
            sys_prompt += str(context_limit)
            sys_prompt += ' words max)>\n'
            sys_prompt += history
            sys_prompt += '\n</System - Recent Conversation History>\n\n'

        user_prompt = 'User: ' + user_text
        mem_text = self._build_memory_block(memories, deep_recall_round2)
        if mem_text:
            user_prompt += '\n\n'
            user_prompt += '<System - associated memory context (The assessment of '
            user_prompt += 'which memories are useful depends on the situation and '
            user_prompt += 'overall context, therefore you have the freedom to decide '
            user_prompt += 'which memories to use and which to discard. Actively seek '
            user_prompt += 'connections between different memories — combining '
            user_prompt += 'partial pieces of information can create a complete '
            user_prompt += 'picture.):\n'
            user_prompt += mem_text
            user_prompt += '\n</System - associated memory context>\n'

        if deep_recall and deep_recall_round2 is None:
            user_prompt += '\n\n'
            user_prompt += '<System - Deep Recall Mode Active>\n'
            user_prompt += (
                'You may perform ONE targeted memory query if the associated '
                'memories above are insufficient.\n\n'
                'HOW THE MEMORY SYSTEM WORKS:\n'
                'The memory system indexes the USER\'S past statements — not your '
                'responses, not the timestamps, not the turn distance.\n'
                'It retrieves memories by semantic similarity to the query text.\n'
                'Therefore: write the query AS IF YOU WERE THE USER speaking about '
                'themselves,\n'
                'using the same language and vocabulary the user uses when talking '
                'to you.\n\n'
                'HOW TO FORMULATE THE QUERY:\n'
                '- Use keyword probes in first person — short, open-ended fragments '
                'ending with \'?\'\n'
                '- Combine MULTIPLE probes in one query to maximize recall coverage.\n'
                '  More keyword probes = richer semantic vector = better memory '
                'retrieval.\n'
                '- Use the same language the user uses (if they write in Czech, '
                'query in Czech, etc.)\n\n'
                'GOOD queries (combined probes, user\'s voice):\n'
                '  "My name is? I work as? I study? My background is?"\n'
                '  "I\'m working on? My project is? My role is?"\n'
                '  "I live in? I like? I\'m interested in?"\n\n'
                'BAD queries (model\'s perspective, generic phrases — will NOT match '
                'user\'s past statements):\n'
                '  "What do I know about the user?"\n'
                '  "User\'s personal information"\n'
                '  "Tell me everything about this person"\n\n'
                'HOW TO USE:\n'
                '1. Write a brief acknowledgment (max 1 sentence, e.g. "Let me think '
                'about that...").\n'
                '2. Immediately follow with: |MEMQUERY| [your query using the rules '
                'above] |ENDQUERY|\n'
                '3. Stop. The system retrieves memories and prompts you again for the '
                'full response.\n\n'
                'If associated memories are already sufficient — skip the query and '
                'respond directly.\n'
                'STRICT RULES:\n'
                '1. Maximum ONE |MEMQUERY| token. Never use it more than once.\n'
                '2. If you USE |MEMQUERY|: do NOT include |STPAM|, |MIDPAM|, or '
                '|ENDPAM| tokens.\n'
                '   Those are reserved for your FINAL response (which comes in the '
                'next round).\n'
                '3. If you SKIP the query and answer directly: include '
                '|STPAM|...|ENDPAM| as normal\n'
                '   (because your direct answer IS the final response).\n'
            )
            user_prompt += '</System - Deep Recall Mode Active>\n'

        if deep_recall and deep_recall_round2 is not None:
            results = deep_recall_round2.get('results', []) or []
            mem_text_2 = self._build_memory_block(results, None)
            user_prompt += '\n\n'
            user_prompt += '<System - Deep Recall Results>\n'
            user_prompt += 'Your targeted memory query returned the following results:\n'
            user_prompt += mem_text_2 or 'No additional memories found for this query.'
            user_prompt += '\n</System - Deep Recall Results>\n'
            user_prompt += 'IMPORTANT: Provide your FINAL response now. Do NOT use '
            user_prompt += '|MEMQUERY| again.'
            user_prompt += '\n'

        if not self.session_history:
            sys_prompt += '<System - New Conversation thread> Suggest a brief 2-5 word '
            sys_prompt += 'title based on the user\'s first query. Place it at the '
            sys_prompt += 'very end after |ENDPAM|, format: |TITLE| Your suggested '
            sys_prompt += 'title. Do not wrap it in any brackets.'

        return {'systemPrompt': sys_prompt, 'userPrompt': user_prompt}

    def _build_history_block(self, context_limit: int) -> str:
        """Last conversation records trimmed to context_limit words."""
        words = 0
        lines = []
        for msg in reversed(self.session_history):
            role = msg.get('role', '')
            text = msg.get('text', '')
            if not text:
                continue
            # Label formatting (User: / Model: prefixes).
            if role == 'user':
                lines.append('User: ' + text)
            else:
                lines.append('Model: ' + text)
            words += len(text.split())
            if words >= context_limit:
                break
        lines.reverse()
        return '\n'.join(lines)

    def _build_memory_block(self, memories: List[Dict],
                            deep_recall_round2: Optional[Dict]) -> str:
        if not memories:
            return ''
        return '\n'.join(self._format_memory(m) for m in memories)

    # ------------------------------------------------------------------
    # LLM call with timeout
    # ------------------------------------------------------------------
    async def _call_llm_with_timeout(self, prompt: str, model: str,
                                     use_web_search: bool,
                                     attachments: List[Dict] = None,
                                     system_prompt: str = '') -> str:
        """Calls the LLM and waits at most 7 minutes with progress notifications (2/5 min)."""
        start = time.monotonic()
        from .llm_client import LLMError

        llm_task = asyncio.ensure_future(
            self._llm.send_prompt(prompt, model, use_web_search,
                                  attachments or [], system_prompt)
        )
        warn1_fired = False
        warn2_fired = False
        while True:
            if llm_task.done():
                return llm_task.result()
            elapsed = time.monotonic() - start
            if elapsed > 120 and not warn1_fired:
                warn1_fired = True
                self.thinking_update.emit('⏳ Still waiting for response... (2 min)')
            if elapsed > 300 and not warn2_fired:
                warn2_fired = True
                self.thinking_update.emit('⚠️ Response is taking very long... (5 min)')
            if elapsed > 420:
                llm_task.cancel()
                raise LLMError('TIMEOUT', '⏰ LLM response timed out after 7 minutes.')
            await asyncio.sleep(10)

    # ------------------------------------------------------------------
    # Deep recall (2-round cycle with MEMQUERY)
    # ------------------------------------------------------------------
    async def _process_deep_recall(self, user_text: str, initial_memories: List,
                                   model: str, use_web_search: bool,
                                   attachments: List[Dict] = None) -> Dict:
        result: Dict = {'response': '', 'usedDeepRecall': False}

        # Round 1 — the model gets permission to run one MEMQUERY
        round1_prompts = self._build_enriched_prompt(
            user_text, initial_memories, model,
            deep_recall=True, deep_recall_round2=None,
            use_web_search=use_web_search,
        )
        user_prompt, system_prompt = self._split_prompts(round1_prompts, model)
        round1_response = await self._call_llm_with_timeout(
            user_prompt, model, use_web_search, attachments, system_prompt
        )

        match = re.search(r'\|MEMQUERY\|([\s\S]*?)\|ENDQUERY\|', round1_response)
        if not match:
            result['response'] = round1_response
            result['usedDeepRecall'] = False
            return result

        mem_query = match.group(1).strip()
        if not mem_query:
            logger.warning(
                'Deep Recall: model emitted empty MEMQUERY — skipping biomem lookup'
            )
            result['response'] = round1_response
            result['usedDeepRecall'] = False
            return result

        # Round 2 — targeted memory retrieval + final response
        self.thinking_bubble.emit(mem_query)
        dr_sid = 'dr_' + self._generate_session_id()
        try:
            handler = self._handler
            if handler is not None:
                top_k = self._get_max_associations()
                handler.cache.store(dr_sid, mem_query, {'top_k': top_k})
                additional_memories = await asyncio.get_running_loop().run_in_executor(
                    None, handler._retrieve_memories, mem_query, top_k,
                )
            else:
                additional_memories = []
        except Exception:
            logger.error('Deep Recall biomem query failed')
            result['response'] = round1_response
            result['usedDeepRecall'] = False
            return result

        if not additional_memories:
            logger.warning('No additional memories found for this query.')
            additional_memories = []

        round2_prompts = self._build_enriched_prompt(
            user_text, initial_memories, model,
            deep_recall=True,
            deep_recall_round2={'mem_text': mem_query,
                                'results': additional_memories,
                                'thinkingText': mem_query},
            use_web_search=use_web_search,
        )
        r2_user, r2_sys = self._split_prompts(round2_prompts, model)
        round2_response = await self._call_llm_with_timeout(
            r2_user, model, use_web_search, attachments, r2_sys
        )
        result['response'] = round2_response
        result['usedDeepRecall'] = True
        return result

    # ------------------------------------------------------------------
    # PAM token parsing
    # ------------------------------------------------------------------
    def _parse_llm_response(self, response_text: str) -> Dict:
        """
        Parses |STPAM| ... |MIDPAM| ... |ENDPAM| and the optional |TITLE|.

        Returns a dict with keys: displayText, userSummary, modelSummary,
        hasTokens, threadTitle, pamMisplaced.
        """
        text = response_text
        user_summary = None
        model_summary = None
        thread_title = None
        has_tokens = False
        pam_misplaced = False

        empty = {
            'displayText': text.strip(),
            'userSummary': user_summary,
            'modelSummary': model_summary,
            'hasTokens': has_tokens,
            'threadTitle': thread_title,
            'pamMisplaced': pam_misplaced,
        }

        stpam_idx = text.find(_TOKEN_STPAM)
        endpam_idx = text.find(_TOKEN_ENDPAM)
        if stpam_idx == -1 or endpam_idx == -1 or endpam_idx < stpam_idx:
            return empty

        pam_content = text[stpam_idx:endpam_idx + len(_TOKEN_ENDPAM)]
        midpam_idx = pam_content.find(_TOKEN_MIDPAM)
        if midpam_idx == -1:
            # malformed — tokeny bez model summary
            logger.warning('PAM tokens malformed (missing |MIDPAM|).')
            return empty

        user_summary = pam_content[len(_TOKEN_STPAM):midpam_idx].strip()
        model_summary = (
            pam_content[midpam_idx + len(_TOKEN_MIDPAM):].replace(
                _TOKEN_ENDPAM, '').strip()
        )
        has_tokens = True

        before = text[:stpam_idx]
        after = text[endpam_idx + len(_TOKEN_ENDPAM):]

        if not before.strip():
            # Tokens are at the start of the response (misplaced) — show only the text after |ENDPAM|
            pam_misplaced = True
            display_text = after.strip()
        else:
            after_no_title = re.sub(r'\|TITLE\|[^\n]*', '', after)
            display_text = (before + after_no_title).strip()

        title_idx = after.find(_TOKEN_TITLE)
        if title_idx != -1:
            raw_title = after[title_idx + len(_TOKEN_TITLE):].strip()
            if '\n' in raw_title:
                raw_title = raw_title.split('\n', 1)[0]
            thread_title = re.sub(r'["\'\u0027\[\]{}()*]+', '', raw_title).strip()
            if not thread_title:
                thread_title = None

        return {
            'displayText': display_text,
            'userSummary': user_summary,
            'modelSummary': model_summary,
            'hasTokens': has_tokens,
            'threadTitle': thread_title,
            'pamMisplaced': pam_misplaced,
        }

    # ------------------------------------------------------------------
    # LLM error formatting
    # ------------------------------------------------------------------
    def _format_llm_error(self, err: Exception) -> str:
        code = getattr(err, 'code', None)
        if code == 'NO_API_KEY':
            return '🔑 The API key is not configured for this provider.'
        if code == 'TIMEOUT':
            return '⏰ LLM response timed out after 7 minutes.'
        if code == 'NETWORK_ERROR':
            return '🌐 Could not connect to the selected provider.'
        if code == 'API_ERROR':
            return '❌ The selected provider rejected the request.'
        return '🔧 The conversation request failed unexpectedly.'

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------
    def _extract_pdf_text(self, path) -> str:
        """Extracts text from a PDF (pypdf) — called in an executor."""
        try:
            from pypdf import PdfReader
        except ImportError:
            logger.error('PDF extraction unavailable: pypdf is not installed')
            return ''
        reader = PdfReader(str(path))
        return '\n\n'.join(page.extract_text() for page in reader.pages)

    async def _prepare_attachments(self, paths: List[str]) -> List[Dict]:
        """Converts file paths into attachment dicts for the LLM call."""
        loop = asyncio.get_running_loop()
        prepared: List[Dict] = []
        for p in paths or []:
            path = Path(p)
            if not path.exists():
                logger.error('Attachment preparation failed: file not found')
                self.system_message.emit('Attachment could not be prepared.', 'warning')
                continue
            ext = path.suffix.lower()
            name = path.name
            try:
                if ext == '.pdf':
                    text = await loop.run_in_executor(
                        None, self._extract_pdf_text, str(path))
                    prepared.append({
                        'type': 'text', 'filename': name, 'data': text,
                        'mime_type': 'text/plain',
                    })
                elif ext in self._IMAGE_EXTS:
                    raw = await loop.run_in_executor(None, path.read_bytes)
                    prepared.append({
                        'type': 'image', 'filename': name,
                        'data': base64.b64encode(raw).decode('utf-8'),
                        'mime_type': self._IMAGE_MIME.get(ext, 'image/png'),
                    })
                elif ext in self._TEXT_EXTS:
                    text = await loop.run_in_executor(None, path.read_text, 'utf-8')
                    prepared.append({
                        'type': 'text', 'filename': name, 'data': text,
                        'mime_type': 'text/plain',
                    })
                else:
                    logger.error('Attachment preparation failed: unsupported type')
                    self.system_message.emit('Unsupported attachment type.', 'warning')
            except Exception:
                logger.error('Attachment preparation failed: read or decode error')
                self.system_message.emit('Attachment could not be prepared.', 'warning')
        return prepared

    # ------------------------------------------------------------------
    # Main entry point (non-blocking)
    # ------------------------------------------------------------------
    def process_message(self, user_text: str, model: str, mode: str = 'associative',
                        use_web_search: bool = False,
                        attachments: Optional[List[str]] = None):
        """Schedule the async 8-step flow. Non-blocking.

        attachments: list of local file paths (images, PDFs, text files).
        """
        if self.is_busy:
            logger.warning('Conversation flow error: handler is busy')
            return
        if self._loop is None:
            logger.error('ConversationHandler: async loop not set')
            self.system_message.emit(
                '⛔ biomem module not ready yet. Please wait a moment.', 'error')
            return

        self._current_attachments = list(attachments or [])
        self.is_busy = True
        self.busy_changed.emit(True)

        def _run():
            try:
                asyncio.ensure_future(
                    self._process_message_async(user_text, model, mode,
                                                use_web_search)
                )
            except Exception:
                logger.error('Conversation scheduling failed')
                self.thinking_hide.emit()
                self.system_message.emit(
                    '❌ The conversation request could not be started.', 'error')
                self._unlock()

        self._loop.call_soon_threadsafe(_run)

    # ------------------------------------------------------------------
    # Asynchronous 8-step flow
    # ------------------------------------------------------------------
    async def _process_message_async(self, user_text: str, model: str, mode: str,
                                     use_web_search: bool):
        try:
            ### 1. Input — prepare attachments + show the user message in the UI
            prepared_attachments = await self._prepare_attachments(
                self._current_attachments)
            attach_names = [a.get('name', '') for a in prepared_attachments]
            self.user_message.emit(user_text, attach_names)
            self.thinking_update.emit('')

            ### 2. Retrieve — session pairing + biomem associative recall
            session_id = self._generate_session_id()
            memories = []
            if self._handler is not None:
                max_assoc = self._get_max_associations()
                self._handler.cache.store(session_id, user_text, {'top_k': max_assoc})
                try:
                    memories = await asyncio.get_running_loop().run_in_executor(
                        None, self._handler._retrieve_memories, user_text, max_assoc)
                except Exception:
                    logger.warning('biomem retrieve failed')
                    memories = []
                try:
                    self._handler.memory.step()
                except Exception:
                    pass

            ### API key check
            display_name = {'chatgpt': 'ChatGPT', 'gemini': 'Gemini',
                            'claude': 'Claude', 'ollama': 'Ollama'}.get(
                                model, 'selected provider')
            try:
                has_key = self._llm.has_api_key(model)
            except Exception:
                has_key = False
            if not has_key:
                self.system_message.emit(
                    '⚠️ The API key for ' + display_name +
                    ' is not configured. Go to LLM Settings.', 'error')
                self._unlock()
                return

            self.session_history.append({'role': 'user', 'text': user_text})

            ### 3. Build Prompt + 4. LLM
            if mode == 'deep':
                result = await self._process_deep_recall(
                    user_text, memories, model, use_web_search, prepared_attachments)
                response_text = result['response']
            else:
                prompts = self._build_enriched_prompt(
                    user_text, memories, model,
                    deep_recall=False, deep_recall_round2=None,
                    use_web_search=use_web_search)
                user_prompt, system_prompt = self._split_prompts(prompts, model)
                response_text = await self._call_llm_with_timeout(
                    user_prompt, model, use_web_search, prepared_attachments,
                    system_prompt)

            ### 5. Parse PAM
            parsed = self._parse_llm_response(response_text)
            text = parsed.get('displayText', response_text.strip())

            ### 6. Display
            self.thinking_hide.emit()
            if parsed.get('pamMisplaced'):
                logger.warning('PAM tokens found at start of response (misplaced); '
                               'showing text after |ENDPAM|.')
            self.model_message.emit(text)
            if not text.strip():
                self.pam_token_warning.emit()
                self._unlock()
                return

            self.session_history.append({'role': 'model', 'text': text})

            ### 7. biomem Store — pairing via the session cache
            user_summary = parsed.get('userSummary') or ''
            model_summary = parsed.get('modelSummary') or ''
            try:
                if self._handler is not None:
                    original_query = self._handler.cache.consume(session_id)
                    if original_query is None:
                        self.system_message.emit(
                            '⚠️ Memory was not saved (session expired).', 'warning')
                    elif not (user_summary or model_summary):
                        self.system_message.emit(
                            '⚠️ Memory was not saved (model did not return summary '
                            'tokens).', 'warning')
                    else:
                        if len(original_query.split()) <= 20:
                            key = original_query
                        else:
                            key = user_summary or original_query
                        self._handler.memory.store(
                            key=key, value=model_summary,
                            intensity=1.0, surprise=1.0)
                        self._handler.memory.save()
                self._save_thread(parsed.get('threadTitle'))
            except Exception:
                logger.error('biomem store failed')
                self.system_message.emit('⚠️ Memory was not saved (biomem error).',
                                         'warning')
                self._save_thread(parsed.get('threadTitle'))

            ### 8. Unlock
            self._unlock()
        except Exception as e:
            logger.error('Conversation processing failed')
            self.thinking_hide.emit()
            self.system_message.emit(self._format_llm_error(e), 'error')
            self._unlock()

    # ------------------------------------------------------------------
    # Thread management
    # ------------------------------------------------------------------
    def _init_active_thread(self):
        """Loads the newest thread or creates a new empty thread."""
        threads = self._threads.get_thread_list()
        if threads:
            self.current_thread_id = threads[0]['id']
            self.session_history = self._threads.load_thread(self.current_thread_id)
        else:
            self.current_thread_id = self._generate_session_id()
            self.session_history = []
            self._threads.save_thread(self.current_thread_id, 'New chat',
                                      int(time.time() * 1000), [])

    def _save_thread(self, new_title: Optional[str] = None):
        """Saves the current thread and notifies the UI about the history change."""
        ts = int(time.time() * 1000)
        title = new_title or 'New chat'
        self._threads.save_thread(self.current_thread_id, title, ts,
                                  self.session_history)
        self.history_updated.emit(self._threads.get_thread_list())

    def _unlock(self):
        """Returns the handler to the non-busy state (step 8 of the flow)."""
        self.is_busy = False
        self.busy_changed.emit(False)

    def create_new_thread(self):
        """Creates a new empty thread and switches to it."""
        self.current_thread_id = self._generate_session_id()
        self.session_history = []
        self._threads.save_thread(self.current_thread_id, 'New chat',
                                  int(time.time() * 1000), [])
        self.history_updated.emit(self.get_thread_list())

    def switch_thread(self, thread_id: str):
        """Switches to an existing thread by ID."""
        self.current_thread_id = thread_id
        self.session_history = self._threads.load_thread(thread_id)

    def delete_thread(self, thread_id: str):
        """Deletes a thread; if it was active, switches to a new one."""
        self._threads.delete_thread(thread_id)
        if self.current_thread_id == thread_id:
            self._init_active_thread()
        self.history_updated.emit(self.get_thread_list())

    def rename_thread(self, thread_id: str, new_title: str):
        """Renames a thread."""
        self._threads.rename_thread(thread_id, new_title)
        self.history_updated.emit(self.get_thread_list())

    def get_thread_list(self) -> List[Dict]:
        """List of threads [{id, title, timestamp}] newest first."""
        return self._threads.get_thread_list()

    def get_current_history(self) -> List[Dict]:
        """History of the current thread."""
        return list(self.session_history)

    def check_first_run(self):
        """Emit first_run signal if the database is empty (no threads, no writes)."""
        threads = self._threads.get_thread_list()
        if not threads:
            self.first_run.emit()
