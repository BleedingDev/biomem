# -*- coding: utf-8 -*-
'''Embedder module for converting text to embeddings.

Uses SentenceTransformers with a multilingual model.
'''
import os

import torch
import torch.nn.functional as F
from pathlib import Path
from typing import List, Optional, Union

# Set OMP_NUM_THREADS early, together with the other module-level defaults.
os.environ.setdefault('OMP_NUM_THREADS', '1')

# Docstrings are assigned explicitly here (__doc__ = ...) so the module
# API stays stable regardless of how the compiler treats whitespace-only
# string literals.


class TextEmbedder(object):
    __doc__ = (
        '\n'
        '    Wrapper for SentenceTransformers with lazy loading.\n'
        '    \n'
        '    Supports:\n'
        '    - Lazy model loading (loaded only on first use)\n'
        '    - Automatic CPU/CUDA detection\n'
        '    - Batch encoding\n'
        '    - Embedding normalization\n'
        '    '
    )

    def __init__(self, model_name: str = 'paraphrase-multilingual-MiniLM-L12-v2',
                 device: Optional[str] = None, normalize: bool = True):
        """
        Args:
            model_name: Model name from SentenceTransformers
            device: Device ('cpu', 'cuda', or None for auto-detect)
            normalize: Whether to normalize embeddings to unit norm
        """
        self.model_name = model_name
        self.device = device
        self.normalize = normalize
        self._model = None

    __init__.__doc__ = (
        '\n'
        '        Args:\n'
        '            model_name: Model name from SentenceTransformers\n'
        '            device: Device (\'cpu\', \'cuda\', or None for auto-detect)\n'
        '            normalize: Whether to normalize embeddings to unit norm\n'
        '        '
    )

    @staticmethod
    def _is_model_cached(model_name: str) -> bool:
        """
        Checks whether the SentenceTransformer model is downloaded in the local HF cache.

        Checks the standard HF hub cache directory:
        ~/.cache/huggingface/hub/models--sentence-transformers--{model_name}
        """
        full_name = 'sentence-transformers/' + model_name
        cache_dir_name = 'models--' + full_name.replace('/', '--')
        hf_cache = os.environ.get('HUGGINGFACE_HUB_CACHE')
        if not hf_cache:
            hf_home = os.environ.get('HF_HOME') or str(Path.home() / '.cache' / 'huggingface')
            hf_cache = os.path.join(hf_home, 'hub')
        cache_path = os.path.join(hf_cache, cache_dir_name)
        if not os.path.isdir(cache_path):
            return False
        snapshots_dir = os.path.join(cache_path, 'snapshots')
        if not os.path.isdir(snapshots_dir):
            return False
        for snapshot_path in os.listdir(snapshots_dir):
            if os.path.isdir(os.path.join(snapshots_dir, snapshot_path)):
                return True
        return False

    _is_model_cached.__func__.__doc__ = (
        '\n'
        '        Checks whether the SentenceTransformer model is downloaded in the local HF cache.\n'
        '        \n'
        '        Checks the standard HF hub cache directory:\n'
        '        ~/.cache/huggingface/hub/models--sentence-transformers--{model_name}\n'
        '        '
    )

    @property
    def model(self):
        """Lazy model loading — offline-first (from local cache)."""
        if self._model is None:
            print("Loading model '{}'...".format(self.model_name))
            torch.set_num_threads(1)
            if self.device is None:
                self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise ImportError(
                    'SentenceTransformers is not installed. Install it with: '
                    'pip install sentence-transformers'
                )
            offline_ok = self._is_model_cached(self.model_name)
            if offline_ok:
                # Offline mode via HF_HUB_OFFLINE (defaults set here).
                os.environ.setdefault('HF_HUB_OFFLINE', '1')
                try:
                    self._model = SentenceTransformer(self.model_name, device=self.device)
                    self._model.eval()
                    print("Model loaded on {} (offline cache)".format(self.device))
                    return self._model
                except Exception:
                    print('Offline load failed, trying online...')
                    self._model = None
            self._model = SentenceTransformer(self.model_name, device=self.device)
            self._model.eval()
            if offline_ok:
                print("Model loaded on {} (online fallback)".format(self.device))
            else:
                print("Model loaded on {} (online)".format(self.device))
        return self._model

    @property
    def embedding_dim(self):
        """Returns the embedding dimension."""
        return self.model.get_sentence_embedding_dimension()

    def encode(self, text: str) -> torch.Tensor:
        """
        Converts text to an embedding.

        Args:
            text: Input text

        Returns:
            embedding: [embedding_dim] tensor
        """
        with torch.no_grad():
            embeddings = self.model.encode(
                [text], convert_to_tensor=True, show_progress_bar=False
            )
            embeddings = embeddings.squeeze(0)
            if self.normalize:
                embeddings = F.normalize(embeddings, dim=-1)
        return embeddings

    encode.__doc__ = (
        '\n'
        '        Converts text to an embedding.\n'
        '        \n'
        '        Args:\n'
        '            text: Input text\n'
        '            \n'
        '        Returns:\n'
        '            embedding: [embedding_dim] tensor\n'
        '        '
    )

    def encode_batch(self, texts: List[str]) -> torch.Tensor:
        """
        Converts a batch of texts to embeddings.

        Args:
            texts: List of texts

        Returns:
            embeddings: [N, embedding_dim] tensor
        """
        with torch.no_grad():
            embeddings = self.model.encode(
                texts, convert_to_tensor=True, show_progress_bar=False
            )
            if self.normalize:
                embeddings = F.normalize(embeddings, dim=-1)
        return embeddings

    encode_batch.__doc__ = (
        '\n'
        '        Converts a batch of texts to embeddings.\n'
        '        \n'
        '        Args:\n'
        '            texts: List of texts\n'
        '            \n'
        '        Returns:\n'
        '            embeddings: [N, embedding_dim] tensor\n'
        '        '
    )

    def similarity(self, text1: str, text2: str) -> float:
        """
        Computes cosine similarity between two texts.

        Args:
            text1: First text
            text2: Second text

        Returns:
            similarity: Cosine similarity in the range [-1, 1]
        """
        emb1 = self.encode(text1)
        emb2 = self.encode(text2)
        return F.cosine_similarity(emb1.unsqueeze(0), emb2.unsqueeze(0), dim=-1).item()

    similarity.__doc__ = (
        '\n'
        '        Computes cosine similarity between two texts.\n'
        '        \n'
        '        Args:\n'
        '            text1: First text\n'
        '            text2: Second text\n'
        '            \n'
        '        Returns:\n'
        '            similarity: Cosine similarity in the range [-1, 1]\n'
        '        '
    )

    def to(self, device: str) -> 'TextEmbedder':
        """Moves the model to another device."""
        if self._model is not None:
            self._model.to(device)
        self.device = device
        return self


class EmotionExtractor(object):
    __doc__ = (
        '\n'
        '    Extractor of emotion vectors from text.\n'
        '    \n'
        '    Maps text to a 4D vector: [dopamine, serotonin, cortisol, oxytocin]\n'
        '    Neutral value is 1.0.\n'
        '    '
    )

    EMOTION_KEYWORDS = {
        'dopamin': {
            'positive': [
                'radost', 'úspěch', 'skvělé', 'výborně', 'super', 'hurá',
                'joy', 'success', 'great', 'excellent', 'amazing', 'win',
            ],
            'boost': 0.3,
        },
        'serotonin': {
            'positive': [
                'klid', 'pohoda', 'spokojenost', 'mír', 'harmonie',
                'calm', 'peace', 'satisfied', 'content', 'balanced',
            ],
            'negative': [
                'strach', 'úzkost', 'panika', 'fear', 'anxiety', 'panic',
            ],
            'boost': 0.2,
            'penalty': -0.15,
        },
        'kortizol': {
            'positive': [
                'chyba', 'problém', 'strach', 'nemoc', 'špatně', 'nebezpečí',
                'error', 'problem', 'fear', 'illness', 'bad', 'danger',
            ],
            'boost': 0.3,
        },
        'oxytocin': {
            'positive': [
                'vztah', 'člověk', 'pomoc', 'děkuji', 'přátelství', 'láska',
                'relation', 'help', 'thank', 'friendship', 'love', 'together',
            ],
            'boost': 0.25,
        },
    }

    @classmethod
    def extract(cls, text: str) -> torch.Tensor:
        """
        Extracts an emotion vector from text.

        Args:
            text: Input text

        Returns:
            emotion: [4] tensor with values around 1.0 (neutral)
        """
        text_lower = text.lower()
        emotion_dict = {}
        for channel, keywords in cls.EMOTION_KEYWORDS.items():
            value = 1.0
            for word in keywords.get('positive', ()):
                if word in text_lower:
                    value += keywords['boost']
            for word in keywords.get('negative', ()):
                if word in text_lower:
                    value += keywords['penalty']
            # Clamp range [0.5, 2.0] via an if-ladder.
            if value < 0.5:
                value = 0.5
            elif value > 2.0:
                value = 2.0
            emotion_dict[channel] = value
        return torch.tensor([
            emotion_dict['dopamin'],
            emotion_dict['serotonin'],
            emotion_dict['kortizol'],
            emotion_dict['oxytocin'],
        ])

    extract.__func__.__doc__ = (
        '\n'
        '        Extracts an emotion vector from text.\n'
        '        \n'
        '        Args:\n'
        '            text: Input text\n'
        '            \n'
        '        Returns:\n'
        '            emotion: [4] tensor with values around 1.0 (neutral)\n'
        '        '
    )

    @classmethod
    def from_dict(cls, emotion_dict: dict) -> torch.Tensor:
        """
        Creates an emotion vector from a dictionary.

        Args:
            emotion_dict: Dict with 'dopamine', 'serotonin', 'cortisol', 'oxytocin' keys

        Returns:
            emotion: [4] tensor
        """
        return torch.tensor([
            emotion_dict.get('dopamin', 1.0),
            emotion_dict.get('serotonin', 1.0),
            emotion_dict.get('kortizol', 1.0),
            emotion_dict.get('oxytocin', 1.0),
        ])

    from_dict.__func__.__doc__ = (
        '\n'
        '        Creates an emotion vector from a dictionary.\n'
        '        \n'
        '        Args:\n'
        "            emotion_dict: Dict with 'dopamine', 'serotonin', 'cortisol', 'oxytocin' keys\n"
        '            \n'
        '        Returns:\n'
        '            emotion: [4] tensor\n'
        '        '
    )

    @classmethod
    def from_name(cls, name: str) -> torch.Tensor:
        """
        Creates an emotion vector from an emotion name.

        Args:
            name: 'neutral', 'positive', 'negative', 'curious', 'social'

        Returns:
            emotion: [4] tensor
        """
        # NOTE: exact preset table (the module's internal table also
        # contains 'stressed'); the boost is taken from EMOTION_KEYWORDS[channel]['boost'].
        presets = {
            'neutral': None,
            'positive': 'dopamin',
            'negative': 'kortizol',
            'curious': 'serotonin',
            'social': 'oxytocin',
            'stressed': 'kortizol',
        }
        channel = presets.pop(name, name)
        emotion_dict = {k: 1.0 for k in cls.EMOTION_KEYWORDS}
        if channel in cls.EMOTION_KEYWORDS:
            emotion_dict[channel] = 1.0 + cls.EMOTION_KEYWORDS[channel]['boost']
        return torch.tensor([
            emotion_dict['dopamin'],
            emotion_dict['serotonin'],
            emotion_dict['kortizol'],
            emotion_dict['oxytocin'],
        ])

    from_name.__func__.__doc__ = (
        '\n'
        '        Creates an emotion vector from an emotion name.\n'
        '        \n'
        '        Args:\n'
        "            name: 'neutral', 'positive', 'negative', 'curious', 'social'\n"
        '            \n'
        '        Returns:\n'
        '            emotion: [4] tensor\n'
        '        '
    )
