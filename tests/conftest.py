"""Every test uses an in-memory keyring: no test reads or writes the real macOS Keychain.

At import (before any test module, also when one file runs alone): the tool's memory mode (ORDER_CHASER_KEYRING=memory),
in which each key call refuses any backend but a MemoryKeyring, and a MemoryKeyring as keyring's active backend.
PYTHON_KEYRING_BACKEND=conftest.MemoryKeyring (with PYTHONPATH=tests) names the same class."""
import os

import keyring
import pytest

from order_chaser.keys import MODE, MemoryKeyring

os.environ[MODE] = "memory"
keyring.set_keyring(MemoryKeyring())


@pytest.fixture(autouse=True)
def memory_keyring():
    k = MemoryKeyring()
    keyring.set_keyring(k)
    yield k
