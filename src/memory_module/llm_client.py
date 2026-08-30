"""
LLM Client — async multi-provider HTTP client.
Supports: OpenAI (ChatGPT), Anthropic (Claude), Google (Gemini), Ollama (local).
"""
import logging
from typing import Optional, Callable
from urllib.parse import urlsplit

logger = logging.getLogger('bdbm.llm_client')

DEFAULT_MODELS = {
    'chatgpt': 'gpt-4o-mini',
    'gemini': 'gemini-2.5-flash',
    'claude': 'claude-sonnet-4-20250514',
    'ollama': 'llama3',
}

DEFAULT_OLLAMA_URL = 'http://127.0.0.1:11434'
_ALLOWED_OLLAMA_HOSTS = frozenset({'127.0.0.1', '::1', 'localhost'})
_ALLOWED_OLLAMA_BASE_PATHS = frozenset({'', '/'})


def normalize_ollama_base_url(value: str) -> str:
    """Validate and canonicalize a loopback-only Ollama base URL.

    The caller appends the fixed ``/api/chat`` endpoint, so configured URLs
    may only identify an HTTP loopback origin. Keeping this check independent
    of the HTTP client lets every Ollama path reject unsafe targets before a
    prompt, attachment, or recalled memory is placed in a request body.
    """
    if not isinstance(value, str) or not value:
        raise ValueError('invalid Ollama URL')
    if value != value.strip() or any(ord(char) < 0x20 for char in value):
        raise ValueError('invalid Ollama URL')

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError('invalid Ollama URL') from None

    hostname = (parsed.hostname or '').lower()
    if (
        parsed.scheme.lower() != 'http'
        or hostname not in _ALLOWED_OLLAMA_HOSTS
        or port is None
        or port < 1
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or '?' in value
        or '#' in value
        or parsed.path not in _ALLOWED_OLLAMA_BASE_PATHS
    ):
        raise ValueError('invalid Ollama URL')

    # Avoid consulting name service configuration for the accepted localhost
    # alias. Users who need IPv6 can select the explicit [::1] form.
    if hostname == 'localhost':
        hostname = '127.0.0.1'
    authority = f'[{hostname}]:{port}' if hostname == '::1' else f'{hostname}:{port}'
    return f'http://{authority}'


class LLMError(Exception):
    def __init__(self, code: str, message: str, http_status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class LLMClient:
    """Async multi-provider LLM client. Uses httpx for HTTP calls."""

    def __init__(self, get_key_fn: Callable[[str], str], get_model_fn: Optional[Callable[[str], str]] = None, get_ollama_timeout_min_fn: Optional[Callable[[], int]] = None):
        """
        Args:
            get_key_fn:   callable(model) -> str  — returns API key (or Ollama URL)
            get_model_fn: callable(model) -> str  — returns custom model name
            get_ollama_timeout_min_fn: callable() -> int — Ollama timeout in minutes (7–60)
        """
        self._get_key = get_key_fn
        self._get_model_name = get_model_fn or (lambda m: '')
        self._get_ollama_timeout_min = get_ollama_timeout_min_fn or (lambda: 7)

    def _model_name(self, model: str) -> str:
        """Return model name, falling back to DEFAULT_MODELS if user hasn't set one."""
        return self._get_model_name(model) or DEFAULT_MODELS.get(model, model)

    def has_api_key(self, model: str) -> bool:
        key = self._get_key(model)
        if model == 'ollama':
            return True
        return bool(key) and len(key) > 10

    async def send_prompt(self, prompt: str, model: str, use_web_search: bool = False, attachments=None, system_prompt: str = '') -> str:
        """Send enriched prompt to chosen LLM, return raw response text.

        attachments: list of dicts produced by ConversationHandler._prepare_attachments:
            {"type": "image"|"text", "filename": str, "data": str, "mime_type": str}
        system_prompt: used by Ollama as a dedicated system role message (separate from user prompt).
        """
        try:
            import httpx
        except ImportError:
            raise LLMError('MISSING_DEP', 'httpx not installed. Run: pip install httpx')

        try:
            api_key = self._get_key(model)
        except Exception:
            if model == 'ollama':
                raise LLMError(
                    'INVALID_CONFIG', 'Could not read the local Ollama configuration.'
                ) from None
            raise
        if not api_key and model not in ('ollama',):
            raise LLMError('NO_API_KEY', f'API key for {model} is not set.')

        if model == 'ollama':
            try:
                api_key = normalize_ollama_base_url(api_key or DEFAULT_OLLAMA_URL)
            except ValueError:
                raise LLMError(
                    'INVALID_LOCAL_URL',
                    'Ollama must use an HTTP loopback URL with an explicit port.',
                ) from None
            try:
                timeout_s = float(self._get_ollama_timeout_min()) * 60
            except Exception:
                raise LLMError(
                    'INVALID_CONFIG', 'The local Ollama timeout setting is invalid.'
                ) from None
        else:
            timeout_s = 420

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            trust_env=model != 'ollama',
            follow_redirects=False,
        ) as client:
            if model == 'chatgpt':
                return await self._send_to_openai(client, prompt, api_key, use_web_search, attachments)
            elif model == 'gemini':
                return await self._send_to_gemini(client, prompt, api_key, use_web_search, attachments)
            elif model == 'claude':
                return await self._send_to_claude(client, prompt, api_key, use_web_search, attachments)
            elif model == 'ollama':
                return await self._send_to_ollama(client, prompt, api_key, attachments, system_prompt)

            raise LLMError('UNKNOWN_MODEL', f'Unknown model: {model}')

    async def _send_to_openai(self, client, prompt: str, api_key: str, use_web_search: bool, attachments=None) -> str:
        import httpx

        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

        if use_web_search:
            # Responses API with the web search tool
            body = {
                'model': self._model_name('chatgpt'),
                'tools': [{'type': 'web_search_preview'}],
                'input': prompt,
            }
            try:
                resp = await client.post('https://api.openai.com/v1/responses', headers=headers, json=body)
            except httpx.TimeoutException:
                raise LLMError('TIMEOUT', 'Request timed out.')
            except httpx.RequestError as e:
                raise LLMError('NETWORK_ERROR', f'Network error: {e}')

            if resp.status_code != 200:
                err = self._extract_error_msg(resp)
                raise LLMError('API_ERROR', self._format_http_error('ChatGPT', resp.status_code, err), resp.status_code)

            data = resp.json()
            for item in data.get('output', []):
                if item.get('type') == 'message':
                    return ''.join(p.get('text', '') for p in item.get('content', []) if p.get('type') == 'output_text')
            return ''

        else:
            if attachments:
                content = [{'type': 'text', 'text': prompt}]
                for att in attachments:
                    if att['type'] == 'image':
                        content.append({
                            'type': 'image_url',
                            'image_url': {'url': f"data:{att['mime_type']};base64,{att['data']}"}
                        })
                    else:
                        content.append({
                            'type': 'text',
                            'text': f'\n[Attached: {att["filename"]}]\n{att["data"]}'
                        })
                msg = {'role': 'user', 'content': content}
            else:
                msg = {'role': 'user', 'content': prompt}

            body = {
                'model': self._model_name('chatgpt'),
                'messages': [msg],
            }
            try:
                resp = await client.post('https://api.openai.com/v1/chat/completions', headers=headers, json=body)
            except httpx.TimeoutException:
                raise LLMError('TIMEOUT', 'Request timed out.')
            except httpx.RequestError as e:
                raise LLMError('NETWORK_ERROR', f'Network error: {e}')

            if resp.status_code != 200:
                err = self._extract_error_msg(resp)
                raise LLMError('API_ERROR', self._format_http_error('ChatGPT', resp.status_code, err), resp.status_code)

            data = resp.json()
            text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            if isinstance(text, list):
                return ''.join(p.get('text', '') for p in text if p.get('type') == 'text')
            return text or ''

    async def _send_to_gemini(self, client, prompt: str, api_key: str, use_web_search: bool, attachments=None) -> str:
        import httpx

        model_name = self._model_name('gemini')
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}'

        parts = []
        if attachments:
            for att in attachments:
                if att['type'] == 'image':
                    parts.append({'inlineData': {'mimeType': att['mime_type'], 'data': att['data']}})
                else:
                    parts.append({'text': f'[Attached: {att["filename"]}]\n{att["data"]}\n'})

        parts.append({'text': prompt})

        body = {
            'contents': [{'parts': parts}]
        }
        if use_web_search:
            body['tools'] = [{'google_search': {}}]

        try:
            resp = await client.post(url, headers={'Content-Type': 'application/json'}, json=body)
        except httpx.TimeoutException:
            raise LLMError('TIMEOUT', 'Request timed out.')
        except httpx.RequestError as e:
            raise LLMError('NETWORK_ERROR', f'Network error: {e}')

        if resp.status_code != 200:
            msg = self._extract_error_msg(resp)
            raise LLMError('API_ERROR', self._format_http_error('Gemini', resp.status_code, msg), resp.status_code)

        data = resp.json()
        candidates = data.get('candidates', [])
        if not candidates:
            logger.warning('Gemini: empty candidates in response')
            return ''

        parts = candidates[0].get('content', {}).get('parts', [])
        return ''.join(p.get('text', '') for p in parts)

    async def _send_to_claude(self, client, prompt: str, api_key: str, use_web_search: bool, attachments=None) -> str:
        import httpx

        if attachments:
            content = []
            for att in attachments:
                if att['type'] == 'image':
                    content.append({
                        'type': 'image',
                        'source': {
                            'type': 'base64',
                            'media_type': att['mime_type'],
                            'data': att['data']
                        }
                    })
                else:
                    content.append({
                        'type': 'text',
                        'text': f'[Attached: {att["filename"]}]\n{att["data"]}'
                    })
            content.append({'type': 'text', 'text': prompt})
            msg = {'role': 'user', 'content': content}
        else:
            msg = {'role': 'user', 'content': prompt}

        body = {
            'model': self._model_name('claude'),
            'max_tokens': 8192,
            'messages': [msg],
        }
        if use_web_search:
            body['tools'] = [{'type': 'web_search_20250305', 'max_uses': 5}]

        try:
            resp = await client.post(
                'https://api.anthropic.com/v1/messages',
                headers={
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01',
                    'Content-Type': 'application/json'
                },
                json=body,
            )
        except httpx.TimeoutException:
            raise LLMError('TIMEOUT', 'Request timed out.')
        except httpx.RequestError as e:
            raise LLMError('NETWORK_ERROR', f'Network error: {e}')

        if resp.status_code != 200:
            msg = self._extract_error_msg(resp)
            raise LLMError('API_ERROR', self._format_http_error('Claude', resp.status_code, msg), resp.status_code)

        data = resp.json()
        text_parts = [p.get('text', '') for p in data.get('content', []) if p.get('type') == 'text']

        if not text_parts:
            logger.warning('Claude: no text blocks in response (tool-use only or empty content)')

        return ''.join(text_parts)

    async def _send_to_ollama(self, client, prompt: str, ollama_url: str, attachments=None, system_prompt: str = '') -> str:
        import httpx

        try:
            base_url = normalize_ollama_base_url(ollama_url or DEFAULT_OLLAMA_URL)
        except ValueError:
            raise LLMError(
                'INVALID_LOCAL_URL',
                'Ollama must use an HTTP loopback URL with an explicit port.',
            ) from None
        try:
            model_name = self._model_name('ollama')
        except Exception:
            raise LLMError(
                'INVALID_CONFIG', 'Could not read the local Ollama model setting.'
            ) from None

        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})

        msg = {'role': 'user', 'content': prompt}
        if attachments:
            images = [att['data'] for att in attachments if att['type'] == 'image']
            texts = [f'[Attached: {att["filename"]}]\n{att["data"]}' for att in attachments if att['type'] == 'text']

            if texts:
                msg['content'] = '\n\n'.join(texts) + '\n\n' + prompt
            if images:
                msg['images'] = images

        messages.append(msg)

        body = {
            'model': model_name,
            'messages': messages,
            'stream': False,
        }

        try:
            resp = await client.post(f'{base_url}/api/chat', headers={'Content-Type': 'application/json'}, json=body)
        except httpx.ConnectError:
            raise LLMError('NETWORK_ERROR', 'Cannot connect to local Ollama. Is it running?')
        except httpx.TimeoutException:
            raise LLMError('TIMEOUT', 'Ollama did not respond in time.')
        except httpx.RequestError:
            raise LLMError('NETWORK_ERROR', 'Local Ollama request failed.')

        if resp.status_code != 200:
            raise LLMError(
                'API_ERROR',
                f'Ollama returned HTTP {resp.status_code}.',
                resp.status_code,
            )

        try:
            result = resp.json().get('message', {}).get('content', '')
        except Exception:
            raise LLMError(
                'API_ERROR', 'Ollama returned an invalid response.', resp.status_code
            ) from None
        if not result:
            logger.warning('Ollama: empty content in response')

        return result

    @staticmethod
    def _extract_error_msg(resp) -> str:
        try:
            ct = resp.headers.get('content-type', '')
            if 'json' in ct:
                j = resp.json()
                return j.get('error', {}).get('message', '') or j.get('error', '')
        except Exception:
            pass
        return ''

    @staticmethod
    def _format_http_error(provider: str, status: int, server_msg: Optional[str] = None) -> str:
        if status in (401, 403):
            return f'Invalid API key for {provider}. Please check your key in Settings.'
        if status == 429:
            return f'Rate limit exceeded for {provider}.'
        if status == 400:
            return f'Bad request to {provider} API: {server_msg or "Invalid request format."}'
        if status in (500, 502, 503):
            return f'{provider} server error ({status}). Temporarily unavailable.'
        return f'{provider} API error {status}: {server_msg or "Unknown error."}'
