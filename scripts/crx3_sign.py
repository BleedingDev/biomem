#!/usr/bin/env python3
"""CRX3 packer/signer for Chrome extensions (self-signed, no store needed).

Implements the Chromium CRX3 container format:
  "Cr24" + <u32 LE version=3> + <u32 LE header_size> + CrxFileHeader (protobuf) + zip

Signature scheme (matches Chromium's crx_file verifier, RFC 3447 PKCS#1 v1.5 + SHA-256):
  digest = SHA256(b"CRX3 SignedData\\x00" + <u32 LE len(signed)> + signed_header_data + zip_bytes)

Public key in the proof is the DER SubjectPublicKeyInfo. Extension ID (crx_id) =
sha256(SPKI)[:16]. RSA key must be PKCS#8 PEM (2048-bit; matches len + doc in README).

Requires only the Python stdlib + the `openssl` CLI (no pip deps).
"""
from __future__ import annotations

import argparse
import hashlib
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

MAGIC = b"Cr24"
VERSION = 3
SIGNATURE_CONTEXT = b"CRX3 SignedData\x00"


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field_bytes(num: int, payload: bytes) -> bytes:
    """Length-delimited protobuf field."""
    return varint((num << 3) | 2) + varint(len(payload)) + payload


def field_varint(num: int, val: int) -> bytes:
    return varint(num << 3) + varint(val)


def run_openssl(args: list[str], stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["openssl", *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout


def public_key_der(key_pem: Path) -> bytes:
    return run_openssl(["pkey", "-in", str(key_pem), "-pubout", "-outform", "DER"])


def crx_id_from_der(der: bytes) -> bytes:
    return hashlib.sha256(der).digest()[:16]


def crx_id_alphabet(crx_id: bytes) -> str:
    """Chrome's a-p alphabet: each nibble n -> chr(ord('a') + n)."""
    out = []
    for byte in crx_id:
        out.append(chr(ord("a") + (byte >> 4)))
        out.append(chr(ord("a") + (byte & 0x0F)))
    return "".join(out)


def sign_payload(key_pem: Path, payload: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(payload)
        tmp = f.name
    try:
        return run_openssl(["dgst", "-sha256", "-sign", str(key_pem), tmp])
    finally:
        Path(tmp).unlink(missing_ok=True)


def build_zip_manifest(zip_path: Path) -> bytes:
    return zip_path.read_bytes()


def build_crx(zip_path: Path, key_pem: Path, out_path: Path, author: str | None = None) -> bytes:
    if not zip_path.is_file():
        raise SystemExit(f"zip not found: {zip_path}")
    if not key_pem.is_file():
        raise SystemExit(f"key not found: {key_pem} (generate: openssl genrsa -out {key_pem} 2048)")

    zip_data = zip_path.read_bytes()
    der = public_key_der(key_pem)
    crx_id = crx_id_from_der(der)

    # SignedData { bytes crx_id = 1; }
    signed = field_bytes(1, crx_id)
    if author:
        signed += field_bytes(2, author.encode())

    # signature input: context + u32le(len(signed)) + signed + zip
    sig_input = SIGNATURE_CONTEXT + struct.pack("<I", len(signed)) + signed + zip_data
    signature = sign_payload(key_pem, sig_input)

    # AsymmetricKeyProof { bytes public_key = 1; bytes signature = 2; }
    proof = field_bytes(1, der) + field_bytes(2, signature)

    # CrxFileHeader {
    #   repeated AsymmetricKeyProof sha256_with_rsa = 2;
    #   bytes signed_header_data = 10000;
    # }
    header = field_bytes(2, proof) + field_bytes(10000, signed)

    out = MAGIC + struct.pack("<I", VERSION) + struct.pack("<I", len(header)) + header + zip_data
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(out)
    return crx_id


def verify_crx(crx_path: Path, key_pem: Path | None = None) -> tuple[str, bool]:
    data = crx_path.read_bytes()
    if data[:4] != MAGIC:
        raise SystemExit(f"{crx_path}: bad magic")
    version, header_len = struct.unpack("<II", data[4:12])
    if version != VERSION:
        raise SystemExit(f"{crx_path}: unsupported CRX version {version}")
    header = data[12 : 12 + header_len]
    zip_data = data[12 + header_len :]

    # parse CrxFileHeader (only what we need)
    signed = b""
    proofs: list[tuple[bytes, bytes]] = []
    i = 0
    while i < len(header):
        key, i = _read_varint(header, i)
        wire = key & 7
        num = key >> 3
        if wire == 2:
            ln, i = _read_varint(header, i)
            payload = header[i : i + ln]
            i += ln
            if num == 2:  # sha256_with_rsa
                proofs.append(_parse_proof(payload))
            elif num == 10000:  # signed_header_data
                signed = payload
        elif wire == 0:
            _, i = _read_varint(header, i)
        else:
            raise SystemExit(f"unsupported wire type {wire} in header")
    if not signed:
        raise SystemExit("no signed_header_data in CRX header")

    sig_input = SIGNATURE_CONTEXT + struct.pack("<I", len(signed)) + signed + zip_data

    verified = False
    if proofs:
        pubkey = proofs[0][0]
        sig = proofs[0][1]
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(sig_input)
            inp = f.name
            f2 = tempfile.NamedTemporaryFile(delete=False)
            f2.write(pubkey)
            f2.close()
            pub = f2.name
            f3 = tempfile.NamedTemporaryFile(delete=False)
            f3.write(sig)
            f3.close()
            sigf = f3.name
        try:
            res = subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", pub, "-signature", sigf, inp],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            verified = res.returncode == 0 and b"Verified OK" in res.stdout
        finally:
            Path(inp).unlink(missing_ok=True)
            Path(pub).unlink(missing_ok=True)
            Path(sigf).unlink(missing_ok=True)

    expected_id = crx_id_alphabet(crx_id_from_der(proofs[0][0])) if proofs else "?"
    return expected_id, verified


def _parse_proof(payload: bytes) -> tuple[bytes, bytes]:
    pub = b""
    sig = b""
    i = 0
    while i < len(payload):
        key, i = _read_varint(payload, i)
        wire = key & 7
        num = key >> 3
        if wire == 2:
            ln, i = _read_varint(payload, i)
            data = payload[i : i + ln]
            i += ln
            if num == 1:
                pub = data
            elif num == 2:
                sig = data
        else:
            _, i = _read_varint(payload, i)
    return pub, sig


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pack", help="zip + private key -> signed .crx")
    p.add_argument("zip")
    p.add_argument("key")
    p.add_argument("out")
    p.add_argument("--author", default=None)

    v = sub.add_parser("verify", help="verify signature and optional stable identity of a .crx")
    v.add_argument("crx")
    v.add_argument(
        "--expected-id",
        help="fail unless the verified CRX has this exact 32-character Chrome extension ID",
    )

    args = ap.parse_args()
    if args.cmd == "pack":
        crx_id = build_crx(Path(args.zip), Path(args.key), Path(args.out), args.author)
        print(f"wrote {args.out} (crx_id {crx_id_alphabet(crx_id)})")
        _, ok = verify_crx(Path(args.out))
        print("self-verify:", "OK" if ok else "FAILED")
        return 0 if ok else 1
    else:
        crx_id, ok = verify_crx(Path(args.crx))
        print(f"crx_id: {crx_id}")
        print("signature:", "OK" if ok else "FAILED")
        identity_ok = args.expected_id is None or crx_id == args.expected_id
        if args.expected_id is not None:
            print("identity:", "OK" if identity_ok else "FAILED")
        return 0 if ok and identity_ok else 1


if __name__ == "__main__":
    sys.exit(main())
