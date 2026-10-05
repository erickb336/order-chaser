"""The key store (T4 U2): save, remove and read of the Kraken API key, with an in-memory keyring (conftest.py).
No test touches the real macOS Keychain. The key here is a dummy value, not a Kraken key."""
import base64
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import keyring
import keyring.errors
import pytest
from keyring.backend import KeyringBackend
from starlette.testclient import TestClient

from order_chaser import keys
from order_chaser.server import KEYCHAIN_TEXT, create_app

BASE = "http://127.0.0.1:5180"
API = "DUMMYapiKEYforTESTSonly" + "A" * 33                                     # 56 characters, as Kraken's
PRIV = base64.b64encode(b"DUMMY-private-key-for-tests-only".ljust(64, b"!")).decode()   # base64 of 64 bytes
ITEM = (keys.SERVICE, keys.ACCOUNT)


@pytest.fixture
def app(tmp_path):
    guard = create_app(tmp_path, connect=False, latency=0)
    with TestClient(guard, base_url=BASE) as client:
        tok = re.search(r'name="oc-token" content="([^"]+)"', client.get("/setup").text).group(1)
        yield client, {"Origin": BASE, "X-Session-Token": tok}, guard.app.state.keys


def save(client, headers, api=API, priv=PRIV):
    return client.post("/api/key", headers=headers, json={"api_key": api, "private_key": priv})


def test_save_puts_the_key_in_the_keychain_and_answers_without_it(app, memory_keyring):
    client, h, store = app
    r = save(client, h)
    assert (r.status_code, r.json()) == (200, {"saved": True})
    assert memory_keyring.items == {ITEM: f"{API}\n{PRIV}"}
    assert (store.key.api_key, store.key.secret) == (API, b"DUMMY-private-key-for-tests-only".ljust(64, b"!"))


@pytest.mark.parametrize("api, priv", [
    (API[:20], PRIV),                                   # too short for an API key
    (API + " ", PRIV),                                  # a space
    (API, PRIV[:-4]),                                   # not whole base64
    (API, base64.b64encode(b"x" * 32).decode()),        # base64, but not the 64 bytes of a Kraken private key
    (API, None), (123, PRIV),
])
def test_a_key_of_the_wrong_shape_is_refused_and_not_saved(app, memory_keyring, api, priv):
    client, h, store = app
    r = save(client, h, api, priv)
    assert (r.status_code, r.json()) == (400, {"errors": [keys.SHAPE_TEXT]})
    assert memory_keyring.items == {} and store.key is None


@pytest.mark.parametrize("headers", [{"Origin": BASE}, {"Origin": "http://evil.example", "X-Session-Token": "x"}, {}])
def test_save_and_remove_need_our_origin_and_the_session_token(app, memory_keyring, headers):
    client, h, store = app
    assert save(client, headers).status_code == 403
    assert memory_keyring.items == {}
    save(client, h)
    assert client.post("/api/key/remove", headers=headers).status_code == 403
    assert list(memory_keyring.items) == [ITEM]


def test_save_has_no_form_and_takes_json_only(app, memory_keyring):
    client, h, store = app
    r = client.post("/api/key", headers=h, data={"api_key": API, "private_key": PRIV})   # a form post
    assert r.status_code == 400 and memory_keyring.items == {}


def test_remove_deletes_the_item_and_forgets_the_key(app, memory_keyring):
    client, h, store = app
    save(client, h)
    r = client.post("/api/key/remove", headers=h)
    assert (r.status_code, r.json()) == (200, {"removed": True})
    assert memory_keyring.items == {} and store.key is None
    assert client.post("/api/key/remove", headers=h).json() == {"removed": True}   # no item: not an error


def test_the_tool_reads_the_keychain_one_time_and_keeps_the_key_in_memory(memory_keyring):
    memory_keyring.items[ITEM] = f"{API}\n{PRIV}"
    reads = []
    get = memory_keyring.get_password
    memory_keyring.get_password = lambda s, u: reads.append(s) or get(s, u)
    store = keys.KeyStore()
    assert store.read().api_key == API
    assert store.read().api_key == API
    assert reads == [keys.SERVICE]          # one macOS prompt for each start (Q9)


def test_a_denied_keychain_read_is_tried_again_at_the_next_read(memory_keyring):
    memory_keyring.items[ITEM] = f"{API}\n{PRIV}"
    get, answers = memory_keyring.get_password, [keyring.errors.KeyringLocked("denied")]
    memory_keyring.get_password = lambda s, u: (_ for _ in ()).throw(answers.pop()) if answers else get(s, u)
    store = keys.KeyStore()
    with pytest.raises(keyring.errors.KeyringLocked):
        store.read()
    assert store.read().api_key == API


def test_no_item_reads_as_no_key(memory_keyring):
    assert keys.KeyStore().read() is None


def test_a_refused_keychain_write_answers_503_and_shows_no_detail(app, memory_keyring):
    client, h, store = app
    memory_keyring.set_password = lambda *a: (_ for _ in ()).throw(keyring.errors.KeyringLocked("locked: " + PRIV))
    r = save(client, h)
    assert (r.status_code, r.json()) == (503, {"errors": [KEYCHAIN_TEXT]})
    assert store.key is None


def test_remove_fails_loudly_when_macos_refuses_the_delete(memory_keyring):
    # keyring's macOS backend wraps a refusal in PasswordDeleteError too; only a NotFound cause means "no item".
    class NotFound(Exception): ...
    class KeychainDenied(Exception): ...

    def delete(cause):
        def f(s, u):
            raise keyring.errors.PasswordDeleteError("x") from cause
        return f
    memory_keyring.delete_password = delete(NotFound())
    keys.KeyStore().remove()
    memory_keyring.delete_password = delete(KeychainDenied())
    with pytest.raises(keyring.errors.PasswordDeleteError):
        keys.KeyStore().remove()


def test_the_key_is_in_no_file_log_or_response(tmp_path, memory_keyring, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    texts = []
    guard = create_app(tmp_path, connect=False, latency=0)
    with TestClient(guard, base_url=BASE) as client:
        tok = re.search(r'name="oc-token" content="([^"]+)"', client.get("/setup").text).group(1)
        h = {"Origin": BASE, "X-Session-Token": tok}
        texts.append(save(client, h, API, "x" + PRIV).text)              # a refused shape
        texts.append(save(client, h).text)                                # the save
        logging.getLogger("order_chaser").error("the key: %r %s", guard.app.state.keys.key, guard.app.state.keys.key)
        for path in ("/api/state", "/new", "/setup", "/chase", "/history", "/api/history", "/api/account"):
            texts.append(client.get(path).text)
    assert memory_keyring.items == {ITEM: f"{API}\n{PRIV}"}               # the save did work
    out, err = capfd.readouterr()
    files = {p.name: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert "order-chaser.sqlite3" in files
    for secret in (API, PRIV, PRIV[:24]):
        assert [n for n, b in files.items() if secret.encode() in b] == []
        assert secret not in caplog.text + out + err
        assert [t[:40] for t in texts if secret in t] == []
    assert "Key(hidden)" in caplog.text


# ----- the Keychain guard: no test, probe or demo reaches the real macOS Keychain -----

class OtherKeyring(KeyringBackend):
    """A stand-in for a real backend (the macOS Keychain): it records each call and stores nothing."""
    priority = 1

    def __init__(self):
        super().__init__()
        self.calls = []

    def get_password(self, service, username):
        self.calls.append("get")

    def set_password(self, service, username, password):
        self.calls.append("set")

    def delete_password(self, service, username):
        self.calls.append("delete")


def test_the_memory_mode_refuses_a_real_keyring_before_any_key_call(tmp_path, monkeypatch):
    other = OtherKeyring()
    keyring.set_keyring(other)
    assert os.environ[keys.MODE] == "memory"                        # conftest.py sets it for every pytest run
    store = keys.KeyStore()
    for call in (store.read, lambda: store.save(keys.Key(API, PRIV)), store.remove):
        with pytest.raises(keys.RealKeyring):
            call()
    with pytest.raises(keys.RealKeyring):
        create_app(tmp_path, connect=False, latency=0)              # the tool does not start
    assert other.calls == []
    monkeypatch.setenv(keys.MODE, "memroy")                         # a typo is not the real Keychain either
    keyring.set_keyring(keys.MemoryKeyring())
    with pytest.raises(keys.RealKeyring):
        keys.KeyStore().read()


def test_the_main_command_stops_with_a_message_in_the_memory_mode_with_a_real_keyring(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHON_KEYRING_BACKEND", "PYTHONPATH")}
    env.update({keys.MODE: "memory", "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"})
    r = subprocess.run([sys.executable, "-c", "from order_chaser.server import main; main()",
                        "--data-dir", str(tmp_path)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert "runs only with the in-memory keyring" in r.stderr
    r = subprocess.run([sys.executable, "-c", "import keyring; from order_chaser import keys; "
                        "keys.KeyStore().save(keys.Key('A' * 56, 'B' * 88)); print(keyring.get_keyring().items)"],
                       env={**env, "PYTHON_KEYRING_BACKEND": "order_chaser.keys.MemoryKeyring"},
                       capture_output=True, text=True, timeout=60)
    assert r.stdout.startswith("{('Kraken API key (order-chaser)', 'order-chaser'): 'AAAA")   # the demo setting works


def test_the_active_backend_is_the_memory_keyring():
    assert type(keyring.get_keyring()) is keys.MemoryKeyring
    assert os.environ[keys.MODE] == "memory"


def test_a_pytest_run_of_one_file_alone_never_selects_the_macos_backend():
    """With no PYTHON_KEYRING_BACKEND, no PYTHONPATH and no ORDER_CHASER_KEYRING: conftest.py alone gives the
    memory keyring and the memory mode, also when one file runs alone."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHON_KEYRING_BACKEND", "PYTHONPATH", keys.MODE)}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/test_keys.py::test_the_active_backend_is_the_memory_keyring"],
                       cwd=Path(__file__).parent.parent, env=env, capture_output=True, text=True, timeout=120)
    assert "1 passed" in r.stdout, r.stdout + r.stderr
