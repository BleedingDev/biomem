"""
Memory Module - Standalone Text In/Out Memory System.

Complete cognitive memory with:
- Text in / Text out API
- Two-layer architecture (LTM + STM)
- 3D terrain visualization
- Automatic consolidation
- Persistence

Quick start:
    from memory_module import TextMemory

    memory = TextMemory()
    memory.store("What is the capital of France?", "Paris")
    result = memory.recall("Capital of France?")
    print(result.text)  # "Paris"

Installation:
    pip install -e ./MemoryModule

CLI:
    biomem store "key" "value"
    biomem recall "query"
    biomem interactive
"""
__version__ = '0.0.2'
__author__ = 'biomem contributors'

import os

if 'OMP_NUM_THREADS' not in os.environ:
    os.environ['OMP_NUM_THREADS'] = '1'
if 'TOKENIZERS_PARALLELISM' not in os.environ:
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'

from .text_memory import TextMemory, RecallResult
from .config import MemoryConfig, DEFAULT_CONFIG
from .embedder import TextEmbedder, EmotionExtractor
from .memory_centers import MemoryCenters
from .terrain_3d import Terrain3D
from .projections import ProjectionBundle
from .consolidation import SleepConsolidator, AutomaticConsolidator
from .session_cache import SessionCache
from .security import SecurityManager
from .protocol import CommandHandler
from .http_fallback import HTTPFallbackServer
from .autostart import register_autostart, is_autostart_enabled
from .settings_manager import SettingsManager
from .bdbm_container import BDBMContainer, save_bdbm, load_bdbm
from .utils.hw_fingerprint import get_hw_fingerprint, get_hw_fingerprint_hex

__all__ = [
    'TextMemory',
    'RecallResult',
    'MemoryConfig',
    'DEFAULT_CONFIG',
    'TextEmbedder',
    'EmotionExtractor',
    'MemoryCenters',
    'Terrain3D',
    'ProjectionBundle',
    'SleepConsolidator',
    'AutomaticConsolidator',
    'SessionCache',
    'SecurityManager',
    'CommandHandler',
    'HTTPFallbackServer',
    'SettingsManager',
    'BDBMContainer',
    'save_bdbm',
    'load_bdbm',
    'get_hw_fingerprint',
    'get_hw_fingerprint_hex',
    'register_autostart',
    'is_autostart_enabled',
]
