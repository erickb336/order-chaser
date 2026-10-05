"""Every test uses an in-memory keyring: no test reads or writes the real macOS Keychain."""
import keyring
import keyring.errors
import pytest
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    """A keyring in memory, with keyring's contract: delete of a missing item raises PasswordDeleteError."""
    priority = 1

    def __init__(self):
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}
        self.reads = 0          # each read is one macOS prompt on a real Mac (Q9)

    def get_password(self, service, username):
        self.reads += 1
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if self.items.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError("not found")


keyring.set_keyring(MemoryKeyring())   # at import: before any app of any test can run


@pytest.fixture(autouse=True)
def memory_keyring():
    k = MemoryKeyring()
    keyring.set_keyring(k)
    yield k
