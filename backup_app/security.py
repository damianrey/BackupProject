"""Criptografia e senhas.

* A chave Fernet (usada para cifrar as credenciais dos bancos) Ã© gerada
  automaticamente na primeira execuÃ§Ã£o e gravada protegida pelo DPAPI do
  Windows (sÃ³ o mesmo usuÃ¡rio do Windows, na mesma mÃ¡quina, consegue lÃª-la).
  Arquivos de chave antigos em texto puro continuam funcionando.
* Senhas de login NÃƒO sÃ£o mais cifradas de forma reversÃ­vel: usam hash
  PBKDF2-SHA256 com salt.
"""

import base64
import binascii
import hashlib
import hmac
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .paths import KEY_FILE, atomic_write_bytes

_DPAPI_PREFIX = b"DPAPI:"


class CryptoKeyError(Exception):
    """Falha ao obter a chave de criptografia."""


# ---------------------------------------------------------------------------
# DPAPI (Windows) via ctypes, sem dependÃªncia do pywin32
# ---------------------------------------------------------------------------
def dpapi_available() -> bool:
    return os.name == "nt"


def _dpapi_call(protect: bool, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    func.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    func.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not func(ctypes.byref(blob_in), None, None, None, None,
                CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise OSError(ctypes.get_last_error(), "Falha na chamada DPAPI")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def dpapi_protect(data: bytes) -> bytes:
    return _dpapi_call(True, data)


def dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi_call(False, data)


# ---------------------------------------------------------------------------
# Chave Fernet
# ---------------------------------------------------------------------------
def encode_key_file(key: bytes, use_dpapi: bool = True) -> bytes:
    """ConteÃºdo do arquivo de chave (protegido por DPAPI quando disponÃ­vel)."""
    if use_dpapi and dpapi_available():
        return _DPAPI_PREFIX + base64.b64encode(dpapi_protect(key))
    return key


def decode_key_file(raw: bytes) -> bytes:
    raw = raw.strip()
    if raw.startswith(_DPAPI_PREFIX):
        try:
            key = dpapi_unprotect(base64.b64decode(raw[len(_DPAPI_PREFIX):]))
        except (OSError, binascii.Error) as exc:
            raise CryptoKeyError(
                "NÃ£o foi possÃ­vel desproteger a chave (DPAPI). Ela sÃ³ pode ser lida pelo mesmo "
                f"usuÃ¡rio do Windows que a criou. Detalhe: {exc}"
            ) from exc
    else:
        key = raw  # formato antigo: chave em texto puro
    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise CryptoKeyError(f"O arquivo de chave '{KEY_FILE}' Ã© invÃ¡lido.") from exc
    return key


def load_key(create: bool = True, path: Path = KEY_FILE) -> bytes:
    """LÃª a chave Fernet. Se nÃ£o existir e create=True, gera uma nova."""
    try:
        return decode_key_file(path.read_bytes())
    except FileNotFoundError:
        if not create:
            raise CryptoKeyError(f"Arquivo de chave nÃ£o encontrado: '{path}'.")
    except OSError as exc:
        raise CryptoKeyError(f"Erro ao ler o arquivo de chave '{path}': {exc}") from exc

    key = Fernet.generate_key()
    write_key(key, path)
    return key


def write_key(key: bytes, path: Path = KEY_FILE, use_dpapi: bool = True) -> None:
    atomic_write_bytes(path, encode_key_file(key, use_dpapi))


class SecretBox:
    """Cifra/decifra textos com a chave Fernet."""

    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def encrypt(self, text: str) -> str:
        if not text:
            return ""
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")

    def try_decrypt(self, token: str) -> Optional[str]:
        try:
            return self.decrypt(token)
        except (InvalidToken, UnicodeError, ValueError):
            return None

    def decrypt_or_plain(self, value: str) -> str:
        """Decifra o valor; se ele nÃ£o estiver cifrado (arquivos antigos, como a
        porta ou o banco do SQL Server), devolve como estÃ¡."""
        decrypted = self.try_decrypt(value)
        return value if decrypted is None else decrypted


# ---------------------------------------------------------------------------
# Hash de senhas de login
# ---------------------------------------------------------------------------
_HASH_ALGO = "pbkdf2_sha256"
_ITERATIONS = 390_000


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([
        _HASH_ALGO, str(iterations),
        base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii"),
    ])


def is_password_hash(stored: str) -> bool:
    return stored.startswith(_HASH_ALGO + "$")


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != _HASH_ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(digest, expected)


def password_needs_rehash(stored: str) -> bool:
    try:
        return int(stored.split("$")[1]) < _ITERATIONS
    except (IndexError, ValueError):
        return True
