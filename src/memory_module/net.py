'''
Shared network layer of biomem — SSL context and TLS diagnostics.

Motivation (field incident 2026-08-24, see plan_eset.md):
CPython ``load_default_certs()`` loads, besides the ROOT store, the Windows
CA (intermediate) store and promotes ALL certificates from it to trusted
anchors. OpenSSL in trusted-first mode can then anchor on an expired old
copy of an intermediate (e.g. cross-sign ``ISRG Root X2 ← ISRG Root X1``
expiring in 2025) even though a valid path exists in the ROOT store — the
result is ``CERTIFICATE_VERIFY_FAILED: certificate has expired`` for ~5 % of
Windows users, while browsers (Schannel) work fine.

Solution here:
  - ``build_ssl_context()`` — trust = bundled certifi bundle (via
    ``cadata``, never ``cafile`` — an installation in %LOCALAPPDATA% may
    have diacritics in the path that OpenSSL fopen on Windows cannot open)
    + ONLY the Windows ROOT store (for AV MITM roots such as
    "ESET SSL Filter CA" / "Norton Web/Mail Shield Root").
    The CA store is deliberately NOT loaded as anchors.
  - ``describe_peer_certificate()`` — forensic read of the certificate the
    peer actually presents (without trusting the connection content),
    including detection of known TLS interceptors.
'''
import logging
import socket
import ssl
import sys
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger('bdbm.net')

_KNOWN_TLS_INTERCEPTORS = (
    'eset', 'norton', 'web/mail shield', 'avast', 'avg ', 'adguard',
    'kaspersky', 'bitdefender', 'mcafee', 'zscaler', 'fortinet',
    'fortigate', 'sophos', 'netskope', 'bluecoat', 'ssl filter',
    'ssl/tls scanning', 'protocol filter',
)

_ctx_cache = None  # type: Optional[ssl.SSLContext]
_ctx_lock = threading.Lock()


def _load_certifi_cadata(ctx: 'ssl.SSLContext') -> 'bool':
    '''Loads the bundled certifi bundle via cadata (immune to diacritics in the path).'''
    try:
        import certifi
    except ImportError as e:
        logger.warning(f'net: certifi bundle cannot be loaded: {e}')
        return False
    try:
        with open(certifi.where(), 'r', encoding='utf-8') as f:
            bundle = f.read()
        ctx.load_verify_locations(cadata=bundle)
        return True
    except Exception as e:
        logger.warning(f'net: certifi bundle cannot be loaded: {e}')
        return False


def _load_windows_root_store(ctx: 'ssl.SSLContext') -> 'int':
    '''Loads ONLY the Windows ROOT store (trusted roots) as anchors.

    Deliberately skips the CA (intermediate) store — loading it as anchors
    is exactly the CPython bug that caused the field incident
    (an expired copy of an intermediate as a trust anchor).

    The ROOT store is needed for AV MITM roots (ESET, Norton, …),
    which are naturally absent from certifi.
    '''
    if sys.platform != 'win32':
        return 0
    count = 0
    try:
        for cert_bytes, encoding_type, trust in ssl.enum_certificates('ROOT'):
            try:
                ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert_bytes))
                count += 1
            except Exception:
                continue
    except Exception as e:
        logger.warning(f'net: Windows ROOT store cannot be loaded: {e}')
    return count


def build_ssl_context(fresh: 'bool' = False) -> 'ssl.SSLContext':
    '''Returns the SSL context for all outgoing HTTPS connections of biomem.

    Trust anchors: certifi (valid public CAs, via cadata) + Windows ROOT
    store (AV MITM roots). NEVER the Windows CA/intermediate store.

    The context is cached (handshake is thread-safe); ``fresh=True`` forces
    a rebuild (e.g. after a new AV is installed at runtime).
    '''
    global _ctx_cache
    if not fresh and _ctx_cache is not None:
        return _ctx_cache
    with _ctx_lock:
        if not fresh and _ctx_cache is not None:
            return _ctx_cache
        ctx = ssl.create_default_context()
        ok_certifi = False
        try:
            ok_certifi = _load_certifi_cadata(ctx)
        except Exception as e:
            logger.warning(f'net: certifi bundle cannot be loaded: {e}')
            ok_certifi = False
        if not ok_certifi:
            logger.warning('net: no trust anchors — falling back to load_default_certs()')
            ctx.load_default_certs()
        n_windows = 0
        try:
            n_windows = _load_windows_root_store(ctx)
        except Exception as e:
            logger.warning(f'net: Windows ROOT store cannot be loaded: {e}')
        certifi_state = 'OK' if ok_certifi else 'FAIL'
        logger.info(f'net: SSL context ready (certifi={certifi_state}, '
                    f'windows_root={n_windows}, ca_total={len(ctx.get_ca_certs())})')
        if not fresh:
            _ctx_cache = ctx
        return ctx


def describe_peer_certificate(host: 'str', port: 'int' = 443,
                              timeout: 'float' = 10.0) -> 'Optional[Dict[str, Any]]':
    '''Forensic read of the leaf certificate the server (or an interceptor
    in the path) actually presents.

    Uses a NEVER-VERIFIED handshake — no application data is EVER read from
    this connection; it serves exclusively for diagnostics after a
    verification failure.

    Returns:
        dict with subject/issuer/not_before/not_after/fingerprint_sha256/
        expired/interceptor, or None on any failure.
    '''
    try:
        from datetime import datetime, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        context = ssl._create_unverified_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        cert = x509.load_der_x509_certificate(der)
        subject = cert.subject.rfc4514_string()
        issuer = cert.issuer.rfc4514_string()
        not_before = cert.not_valid_before.replace(tzinfo=timezone.utc).isoformat()
        not_after = cert.not_valid_after.replace(tzinfo=timezone.utc).isoformat()
        expired = cert.not_valid_after.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc)
        return {
            'subject': subject,
            'issuer': issuer,
            'not_before': not_before,
            'not_after': not_after,
            'fingerprint_sha256': cert.fingerprint(hashes.SHA256()).hex(),
            'expired': expired,
            'interceptor': detect_interceptor(issuer),
        }
    except Exception as e:
        logger.debug(f'net: peer certificate diagnostics failed: {e}')
        return None


def detect_interceptor(issuer: 'str') -> 'Optional[str]':
    '''Returns the name of a known TLS interceptor if the issuer matches, otherwise None.'''
    if not issuer:
        return None
    issuer_lower = issuer.lower()
    for known in _KNOWN_TLS_INTERCEPTORS:
        if known in issuer_lower:
            return known
    return None


def log_tls_failure_diagnostics(host: 'str', port: 'int' = 443) -> 'Optional[Dict[str, Any]]':
    '''Logs forensic info about the presented certificate after a verification failure.'''
    info = describe_peer_certificate(host, port)
    if info is None:
        logger.warning(f'net: TLS diagnostics: certificate from {host}:{port} could not be read')
        return None
    return info
