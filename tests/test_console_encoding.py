import io
import sys
import types
import unittest
from unittest.mock import patch

from memory_module.embedder import TextEmbedder
from memory_module.localization import Localization


class _FakeSentenceTransformer:
    def eval(self):
        return self


class ConsoleEncodingTests(unittest.TestCase):
    def test_english_cli_messages_are_cp1252_safe(self):
        for key, message in Localization._strings['en'].items():
            if key.startswith('cli.'):
                with self.subTest(key=key):
                    message.encode('cp1252', errors='strict')

    def test_embedder_status_messages_are_cp1252_safe(self):
        module = types.SimpleNamespace(
            SentenceTransformer=lambda *_args, **_kwargs: _FakeSentenceTransformer()
        )
        raw_output = io.BytesIO()
        output = io.TextIOWrapper(raw_output, encoding='cp1252', errors='strict')
        embedder = TextEmbedder(device='cpu')

        with (
            patch.object(embedder, '_is_model_cached', return_value=False),
            patch.dict(sys.modules, {'sentence_transformers': module}),
            patch('sys.stdout', output),
        ):
            self.assertIsInstance(embedder.model, _FakeSentenceTransformer)
            output.flush()

        self.assertIn('Model loaded on cpu', raw_output.getvalue().decode('cp1252'))


if __name__ == '__main__':
    unittest.main()
