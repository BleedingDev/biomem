'''
biomem container — .bdbm format for storing and exporting memory.

Structure:
  A .bdbm file = portable ZIP archive containing:
    - vectors.pt    — PyTorch tensor state (vectors, projections, terrains, ...)
    - metadata.json — text metadata (memory texts, statistics, version)

New containers are portable between machines. Loading also supports the older
machine-bound encrypted representation so existing user data remains readable.
'''
import io
import json
import zipfile
import logging
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger('bdbm.container')

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

from .utils.hw_fingerprint import get_hw_fingerprint

_BDBM_EXTENSION = '.bdbm'
_VECTORS_FILENAME = 'vectors.pt'
_METADATA_FILENAME = 'metadata.json'
_HKDF_SALT = b'bdbm-container-encryption-v1'
_HKDF_INFO = b'bdbm-aes256gcm-key'
_NONCE_SIZE = 12
_MAGIC_ENCRYPTED = b'BDBMENC01'
_MAGIC_PLAIN = b'BDBMZIP01'


def _derive_encryption_key(hw_fingerprint: bytes) -> bytes:
    '''Derives the AES-256 key from the HW Fingerprint.'''
    if HAS_CRYPTO:
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_HKDF_SALT,
            info=_HKDF_INFO,
        )
        return hkdf.derive(hw_fingerprint)
    import hashlib
    return hashlib.pbkdf2_hmac('sha256', hw_fingerprint, _HKDF_SALT + _HKDF_INFO, 100000)


class BDBMContainer:
    '''
    Manager of .bdbm containers for saving/loading memory.

    Usage:
        container = BDBMContainer()

        container.save(state_dict, path="memory_state.bdbm")

        # Load (portable format with compatibility fallback)
        state_dict = container.load("memory_state.bdbm")
    '''

    def __init__(self):
        '''Initialization – the HW fingerprint is loaded lazily.'''
        self._hw_fingerprint = None

    def _get_hw_fp(self) -> bytes:
        '''Lazy-load of the HW fingerprint.'''
        if self._hw_fingerprint is None:
            self._hw_fingerprint = get_hw_fingerprint()
        return self._hw_fingerprint

    def save(self, state: Dict[str, Any], path: str) -> str:
        '''
        Saves the memory state into a .bdbm container.

        Args:
            state: Complete state_dict from TextMemory (vectors + config + stats).
            path: Target path (the .bdbm extension is appended automatically).
        Returns:
            Actual path of the saved file.
        '''
        import torch

        path = str(path)
        if not path.endswith(_BDBM_EXTENSION):
            path = path.rsplit('.', 1)[0] + _BDBM_EXTENSION

        metadata = self._extract_metadata(state)
        vectors_state = self._extract_vectors(state)

        vectors_buffer = io.BytesIO()
        torch.save(vectors_state, vectors_buffer)
        vectors_bytes = vectors_buffer.getvalue()

        metadata_bytes = json.dumps(metadata, ensure_ascii=False, indent=2).encode('utf-8')

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(_VECTORS_FILENAME, vectors_bytes)
            zf.writestr(_METADATA_FILENAME, metadata_bytes)
        zip_data = zip_buffer.getvalue()

        blob = _MAGIC_PLAIN + zip_data
        logger.debug('State saved: %s', path)

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(blob)
        return path

    def load(self, path: str) -> Dict[str, Any]:
        '''
        Loads the memory state from a .bdbm container.

        Portable containers are loaded directly. The encrypted legacy format is
        decoded with the machine fingerprint for data compatibility.

        Args:
            path: Path to the .bdbm file.

        Returns:
            Complete state_dict for TextMemory.

        Raises:
            FileNotFoundError: The file does not exist.
            ValueError: The file can be neither unpacked nor decrypted.
        '''
        import torch

        path = str(path)
        if not Path(path).exists():
            raise FileNotFoundError(f'State file not found: {path}')

        blob = Path(path).read_bytes()

        zip_data = None
        if blob.startswith(_MAGIC_PLAIN):
            zip_data = blob[len(_MAGIC_PLAIN):]
            logger.debug('State loaded: %s', path)
        elif blob.startswith(_MAGIC_ENCRYPTED):
            try:
                zip_data = self._decrypt(blob)
                logger.debug('Legacy state loaded after machine-bound decryption: %s', path)
            except Exception as e:
                raise ValueError(f'State file cannot be decrypted — it was probably created on another PC. Detail: {e}')
        else:
            try:
                zf_test = zipfile.ZipFile(io.BytesIO(blob))
                zf_test.close()
                zip_data = blob
                logger.warning(f'State loaded as a raw ZIP (without magic header): {path}')
            except zipfile.BadZipFile:
                raise ValueError(f"File '{path}' has an unknown format (neither an encrypted state file nor a valid ZIP).")

        with zipfile.ZipFile(io.BytesIO(zip_data), 'r') as zf:
            if _VECTORS_FILENAME not in zf.namelist():
                raise ValueError(f"Container does not contain '{_VECTORS_FILENAME}'")
            vectors_bytes = zf.read(_VECTORS_FILENAME)
            vectors_buffer = io.BytesIO(vectors_bytes)
            vectors_state = torch.load(vectors_buffer, map_location='cpu', weights_only=False)

            metadata = {}
            if _METADATA_FILENAME in zf.namelist():
                metadata_bytes = zf.read(_METADATA_FILENAME)
                metadata = json.loads(metadata_bytes.decode('utf-8'))

        return self._merge_state(vectors_state, metadata)

    def _decrypt(self, blob: bytes) -> bytes:
        '''AES-256-GCM decryption with the HW key.'''
        key = _derive_encryption_key(self._get_hw_fp())
        data = blob[len(_MAGIC_ENCRYPTED):]
        if HAS_CRYPTO:
            if len(data) < _NONCE_SIZE + 16:
                raise ValueError('Encrypted blob is too short')
            nonce = data[:_NONCE_SIZE]
            ciphertext = data[_NONCE_SIZE:]
            aesgcm = AESGCM(key)
            return aesgcm.decrypt(nonce, ciphertext, None)
        extended_key = key * (len(data) // len(key) + 1)
        return bytes(a ^ b for a, b in zip(data, extended_key))

    @staticmethod
    def _extract_metadata(state: Dict[str, Any]) -> Dict[str, Any]:
        '''
        Extracts text metadata from the state_dict.

        These data are stored as JSON inside the ZIP archive,
        so that .bdbm does not contain only raw vectors without context.
        '''
        metadata = {
            'version': state.get('version', 'unknown'),
            'stats': state.get('stats', {}),
        }
        for prefix in ('ltm_centers', 'stm_centers'):
            if prefix in state:
                center_data = state[prefix]
                texts = {
                    'key_texts': center_data.get('key_texts', []),
                    'value_texts': center_data.get('value_texts', []),
                    'memory_ids': center_data.get('memory_ids', []),
                    'provenances': center_data.get('provenances', []),
                    'record_metadata_version': center_data.get('record_metadata_version', 1),
                }
                metadata[f'{prefix}_texts'] = texts
        return metadata

    @staticmethod
    def _extract_vectors(state: Dict[str, Any]) -> Dict[str, Any]:
        '''
        Extracts vector data from the state_dict.

        Returns the state_dict without text data (those are in metadata.json).
        The text lists are replaced with None markers — on merge they
        are restored from the metadata.
        '''
        vectors = dict(state)
        for prefix in ('ltm_centers', 'stm_centers'):
            if prefix in vectors:
                inner = dict(vectors[prefix])
                inner.pop('key_texts', None)
                inner.pop('value_texts', None)
                inner.pop('memory_ids', None)
                inner.pop('provenances', None)
                inner.pop('record_metadata_version', None)
                vectors[prefix] = inner
        return vectors

    @staticmethod
    def _merge_state(vectors: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
        '''Merges the vector data with the metadata back into the complete state_dict.'''
        state = vectors
        for prefix in ('ltm_centers', 'stm_centers'):
            texts_key = f'{prefix}_texts'
            if texts_key in metadata and prefix in state:
                texts = metadata[texts_key]
                state[prefix]['key_texts'] = texts.get('key_texts', [])
                state[prefix]['value_texts'] = texts.get('value_texts', [])
                state[prefix]['memory_ids'] = texts.get('memory_ids', [])
                state[prefix]['provenances'] = texts.get('provenances', [])
                state[prefix]['record_metadata_version'] = texts.get(
                    'record_metadata_version', 1
                )
        if 'version' in metadata:
            state['version'] = metadata['version']
        if 'stats' in metadata:
            state['stats'] = metadata['stats']
        return state


def save_bdbm(state: Dict[str, Any], path: str) -> str:
    '''Convenience wrapper for BDBMContainer.save().'''
    return BDBMContainer().save(state, path)


def load_bdbm(path: str) -> Dict[str, Any]:
    '''Convenience wrapper for BDBMContainer.load().'''
    return BDBMContainer().load(path)
