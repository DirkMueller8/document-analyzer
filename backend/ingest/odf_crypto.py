"""Open password-protected .odt files in memory (no decrypted copy ever touches the disk).

Supports both schemes LibreOffice writes:
- ODF 1.4 / LibreOffice 24.2+: whole-package encryption. One "encrypted-package" entry,
  AES-256-GCM (IV prepended to the ciphertext), key = Argon2id(SHA256(password)). The
  decrypted, raw-deflated payload is itself the real .odt zip.
- ODF 1.2: per-file encryption, AES-256-CBC, key = PBKDF2-HMAC-SHA1(SHA256(password)),
  verified with the sha256-1k checksum.
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
import zipfile
import zlib
from xml.etree import ElementTree as ET

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

MANIFEST = "META-INF/manifest.xml"
NS = "{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}"


class WrongPassword(Exception):
    pass


class UnsupportedEncryption(Exception):
    pass


def is_encrypted(path_or_file) -> bool:
    with zipfile.ZipFile(path_or_file) as z:
        return b"encryption-data" in z.read(MANIFEST)


def _attr(el: ET.Element, local: str) -> str | None:
    """Attribute by local name, whatever its namespace (manifest: or loext:)."""
    for k, v in el.attrib.items():
        if k.rsplit("}", 1)[-1] == local:
            return v
    return None


def _derive_key(enc: ET.Element, password: str) -> bytes:
    skg = enc.find(f"{NS}start-key-generation")
    skg_name = _attr(skg, "start-key-generation-name") if skg is not None else "sha1"
    pw = password.encode("utf-8")
    start_key = hashlib.sha256(pw).digest() if "sha256" in skg_name.lower() else hashlib.sha1(pw).digest()

    kd = enc.find(f"{NS}key-derivation")
    kd_name = _attr(kd, "key-derivation-name")
    salt = base64.b64decode(_attr(kd, "salt"))
    size = int(_attr(kd, "key-size") or 16)
    if kd_name == "PBKDF2":
        return hashlib.pbkdf2_hmac("sha1", start_key, salt, int(_attr(kd, "iteration-count")), size)
    if kd_name.lower().endswith("argon2id"):
        return hash_secret_raw(
            start_key, salt,
            time_cost=int(_attr(kd, "argon2-iterations")),
            memory_cost=int(_attr(kd, "argon2-memory")),
            parallelism=int(_attr(kd, "argon2-lanes")),
            hash_len=size, type=Type.ID,
        )
    raise UnsupportedEncryption(f"key derivation {kd_name!r}")


def _decrypt_entry(data: bytes, enc: ET.Element, password: str) -> bytes:
    alg = enc.find(f"{NS}algorithm")
    alg_name = _attr(alg, "algorithm-name")
    iv = base64.b64decode(_attr(alg, "initialisation-vector"))
    key = _derive_key(enc, password)

    if alg_name.endswith("aes256-gcm"):
        if data[: len(iv)] == iv:
            data = data[len(iv):]
        try:
            plain = AESGCM(key).decrypt(iv, data, None)
        except InvalidTag:
            raise WrongPassword from None
    elif alg_name.endswith("aes256-cbc"):
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = dec.update(data) + dec.finalize()
        pad = plain[-1]  # W3C xmlenc padding: last byte = pad length
        if not 1 <= pad <= 16:
            raise WrongPassword
        plain = plain[:-pad]
        checksum = _attr(enc, "checksum")  # computed over the unpadded data
        if checksum and _attr(enc, "checksum-type").endswith("sha256-1k"):
            if hashlib.sha256(plain[:1024]).digest() != base64.b64decode(checksum):
                raise WrongPassword
    else:
        raise UnsupportedEncryption(f"cipher {alg_name!r} (re-save the file with a current LibreOffice)")

    try:
        return zlib.decompress(plain, -15)
    except zlib.error:
        return plain  # stored without compression


def decrypt_odt(path_or_file, password: str) -> io.BytesIO:
    """Return an in-memory, unencrypted .odt that odfpy can load."""
    with zipfile.ZipFile(path_or_file) as z:
        manifest = z.read(MANIFEST)
        root = ET.fromstring(manifest)
        encrypted = {
            fe.get(f"{NS}full-path"): fe.find(f"{NS}encryption-data")
            for fe in root.iter(f"{NS}file-entry")
            if fe.find(f"{NS}encryption-data") is not None
        }
        if not encrypted:
            return io.BytesIO(_read_all(path_or_file))

        if "encrypted-package" in encrypted:
            inner = _decrypt_entry(z.read("encrypted-package"), encrypted["encrypted-package"], password)
            return io.BytesIO(inner)

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as w:
            for info in z.infolist():
                name = info.filename
                if name == MANIFEST:
                    clean = re.sub(rb"<manifest:encryption-data.*?</manifest:encryption-data>", b"",
                                   manifest, flags=re.S)
                    w.writestr(name, clean, zipfile.ZIP_DEFLATED)
                elif name in encrypted:
                    w.writestr(name, _decrypt_entry(z.read(name), encrypted[name], password),
                               zipfile.ZIP_DEFLATED)
                else:
                    w.writestr(info, z.read(name))
        out.seek(0)
        return out


def _read_all(path_or_file) -> bytes:
    if hasattr(path_or_file, "read"):
        path_or_file.seek(0)
        return path_or_file.read()
    with open(path_or_file, "rb") as f:
        return f.read()
