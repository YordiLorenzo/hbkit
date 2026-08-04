"""Client-side encryption for Synology Hyper Backup archives.

Scheme (reverse engineered; verified byte-exact against a real archive):

    salt      = MD5(unikey)                       # unikey from _Syno_TaskConfig
    seed      = argon2i13(password, salt, ops=4, mem=16MiB, out=32)
    pub, priv = crypto_box_seed_keypair(seed)     # pub MUST equal Config/public.pem

`pub` is stored in the archive, so deriving it from the password is a complete and
instant correctness check - a wrong password is detected before reading any data.

    vkey  = crypto_box_seal_open(vkey.rsa_vkey)      # 80B sealed box -> 32B AES key
    viv   = crypto_box_seal_open(vkey.rsa_vkey_iv)   # 64B sealed box -> 16B AES IV
    chunk = LZ4( unpad( AES-256-CBC(ciphertext, vkey, viv) ) )

Filenames are encrypted separately, with their own key, and stored base64 with '/'
replaced by '_' (illegal in a filename):

    fnkey = SHA256(priv || unikey)
    fniv  = MD5(unikey || FILENAME_IV_SALT)
    name  = unpad( AES-256-CBC(b64decode(stored), fnkey, fniv) )

Note the column names in vkey.db say "rsa_" but no RSA is involved in this version -
they are libsodium sealed boxes (X25519 + XSalsa20-Poly1305). Older archives used RSA;
that path is not implemented here.
"""
from __future__ import annotations

import base64
import hashlib

# Hardcoded in Synology's binary. Also published in mistersandman/hyperbackup_decrypt (2016).
FILENAME_IV_SALT = b"kkE7sRZRvnbVlJFofhD7WCXumXBGyzki"

ARGON2_OPSLIMIT = 4
ARGON2_MEMLIMIT = 1 << 24          # 16 MiB
ARGON2_ALG = 1                     # crypto_pwhash_ALG_ARGON2I13


class WrongPassword(Exception):
    """The password does not match the archive's stored public key."""


class MissingCryptoDeps(Exception):
    """PyNaCl / cryptography are needed to read encrypted archives."""


def _deps():
    try:
        import nacl.bindings as nb
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:                                   # pragma: no cover
        raise MissingCryptoDeps(
            "encrypted archives need PyNaCl and cryptography (normally installed "
            "with hbkit): pip install --upgrade hbkit"
        ) from e
    return nb, Cipher, algorithms, modes


def unpad(b: bytes) -> bytes:
    """Strip PKCS#7 padding, tolerating data that has none."""
    if not b:
        return b
    n = b[-1]
    return b[:-n] if 1 <= n <= 16 and b[-n:] == bytes([n]) * n else b


class ArchiveCrypto:
    """Holds the unwrapped keys for one encrypted archive."""

    def __init__(self, password: str, unikey: str, public_key: bytes,
                 sealed_vkey: bytes, sealed_viv: bytes):
        nb, Cipher, algorithms, modes = _deps()
        self._Cipher, self._algorithms, self._modes = Cipher, algorithms, modes

        uni = unikey.encode()
        seed = nb.crypto_pwhash_alg(32, password.encode(), hashlib.md5(uni).digest(),
                                    ARGON2_OPSLIMIT, ARGON2_MEMLIMIT, ARGON2_ALG)
        pub, priv = nb.crypto_box_seed_keypair(seed)
        if pub != public_key:
            raise WrongPassword("password does not match this archive")

        self.key = nb.crypto_box_seal_open(sealed_vkey, pub, priv)
        self.iv = nb.crypto_box_seal_open(sealed_viv, pub, priv)
        self.fn_key = hashlib.sha256(priv + uni).digest()
        self.fn_iv = hashlib.md5(uni + FILENAME_IV_SALT).digest()

    def _dec(self, ct: bytes, key: bytes, iv: bytes) -> bytes:
        d = self._Cipher(self._algorithms.AES(key), self._modes.CBC(iv)).decryptor()
        return unpad(d.update(ct) + d.finalize())

    def decrypt_chunk(self, ct: bytes) -> bytes:
        return self._dec(ct, self.key, self.iv)

    def decrypt_name(self, stored: str) -> str:
        """Decrypt one path component. '_' stands in for '/' in the base64 alphabet."""
        raw = base64.b64decode(stored.replace("_", "/"))
        return self._dec(raw, self.fn_key, self.fn_iv).decode("utf-8", "replace")
