"""The Kraken API key: kept in the macOS Keychain (the keyring package), and in this process only in memory.

Rules (T4 design, Q3, Q4, Q9, C6):
- The page sends the key one time (fetch, JSON, with the session token). The tool checks its shape here, at the
  boundary, and writes it to the Keychain. No file, no log, no response and no SQLite row gets the key.
- The tool reads the Keychain at most one time for each start (macOS asks "Allow" then) and keeps the key in memory.
- A key that can withdraw is removed from the Keychain (server.py, after the permission test).
- keyring's macOS backend calls Security.framework in this process: the key is never in the arguments of a command.
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

import httpx
import keyring
import keyring.errors

SERVICE = "Kraken API key (order-chaser)"   # the name of the item in Keychain Access
ACCOUNT = "order-chaser"
API_KEY = re.compile(r"[A-Za-z0-9+/=]{40,128}")   # Kraken: 56 characters of base64
SHAPE_TEXT = ("This is not a Kraken API key. Paste the API key and the private key exactly as Kraken shows them, "
              "with no spaces.")


@dataclass(frozen=True)
class Key:
    api_key: str
    private_key: str      # base64, as Kraken shows it

    @property
    def secret(self) -> bytes:
        return base64.b64decode(self.private_key)

    def __repr__(self) -> str:   # a log line or a traceback must never show the key
        return "Key(hidden)"

    __str__ = __repr__


def parse(api_key, private_key) -> Key | None:
    """The key from the page, or None when it does not have the shape of a Kraken key: the API key is base64 text,
    and the private key is base64 of 64 bytes (the HMAC-SHA512 key of Kraken's signature)."""
    if not (isinstance(api_key, str) and isinstance(private_key, str) and API_KEY.fullmatch(api_key)):
        return None
    try:
        secret = base64.b64decode(private_key, validate=True)
    except (binascii.Error, ValueError):
        return None
    return Key(api_key, private_key) if len(secret) == 64 else None


class KeyStore:
    """The one Keychain item of the tool. Each call can wait for a macOS prompt: the server runs it in a thread."""

    def __init__(self) -> None:
        self.key: Key | None = None   # in memory only, after save() or the first read() of this start

    def save(self, key: Key) -> None:
        keyring.set_password(SERVICE, ACCOUNT, f"{key.api_key}\n{key.private_key}")
        self.key = key

    def read(self) -> Key | None:
        """The key: from memory, or from the Keychain one time (macOS asks). None when no item is there.
        Raises keyring.errors.KeyringError when macOS refuses (Deny, or a locked Keychain): a later read tries again."""
        if self.key is None:
            text = keyring.get_password(SERVICE, ACCOUNT)
            self.key = parse(*text.split("\n", 1)) if text and "\n" in text else None
        return self.key

    def remove(self) -> None:
        """Delete the item from the Keychain and forget the key. No item is not an error; any other failure raises,
        so that the page never says "removed" for a key that is still there."""
        self.key = None
        try:
            keyring.delete_password(SERVICE, ACCOUNT)
        except keyring.errors.PasswordDeleteError as e:
            # keyring's contract: PasswordDeleteError = not found. Its macOS backend also wraps a refusal in it;
            # only its NotFound cause means "no item".
            if e.__cause__ is not None and type(e.__cause__).__name__ != "NotFound":
                raise


class NoKey(Exception):
    """No key is saved in the Keychain."""


KEYCHAIN_TEXT = ("macOS did not let the tool use the Keychain. Unlock the Keychain, click Allow in the macOS prompt, "
                 "and try again.")


def why(e: Exception) -> str:
    """The words for a failed live call: never the text of a Keychain error, never the key."""
    from .rest import KrakenError
    if isinstance(e, NoKey):
        return "No Kraken API key is saved. Finish Setup, step 3."
    if isinstance(e, keyring.errors.KeyringError):
        return KEYCHAIN_TEXT
    if isinstance(e, KrakenError):
        return f"Kraken answered \"{e}\"."
    if isinstance(e, httpx.HTTPError):
        return "Kraken did not answer. Check the connection."
    if isinstance(e, ConnectionError):
        return "The private feed of your fills did not connect."
    return "The tool could not reach Kraken."
