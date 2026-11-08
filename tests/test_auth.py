import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.auth import (LoginLimiter, Sessions, fingerprint, hash_password, is_local_ip,
                      validate_new_password, validate_username, verify_password)
from app.users import UserStore

PW = "correct horse battery staple"


# ---- hashing ----
def test_hash_roundtrip_and_format():
    h = hash_password(PW)
    assert h.startswith("scrypt:") and "$" not in h   # safe in .env / compose
    assert verify_password(PW, h) and not verify_password("wrong", h)
    assert hash_password(PW) != h                     # random salt


@pytest.mark.parametrize("bad", ["", "plaintext", "scrypt:x:y", "bcrypt:1:2:3:4:5", "scrypt:a:b:c:d:e"])
def test_verify_rejects_malformed_hashes(bad):
    assert verify_password(PW, bad) is False


# ---- policy ----
def test_username_validation():
    assert validate_username("  Nick.P_1 ") == "nick.p_1"
    for bad in ["", "ab", "has space", "x" * 33, "semi;colon", "a/b"]:
        with pytest.raises(ValueError):
            validate_username(bad)


def test_password_policy():
    validate_new_password("a perfectly fine passphrase", "nick")
    for bad in ["short", "manifest", "MANIFEST", "x" * 9]:
        with pytest.raises(ValueError):
            validate_new_password(bad, "nick")
    with pytest.raises(ValueError):
        validate_new_password("administrator", "administrator")   # same as username


@pytest.mark.parametrize("ip,local", [
    ("10.0.0.5", True), ("192.168.1.9", True), ("172.16.3.3", True), ("127.0.0.1", True),
    ("100.64.1.2", True), ("::1", True), ("fe80::1", True),
    ("8.8.8.8", False), ("1.1.1.1", False), ("2606:4700::1111", False),
    ("unknown", False), ("", False), ("testclient", False),
])
def test_is_local_ip(ip, local):
    assert is_local_ip(ip) is local


# ---- sessions ----
def sess(**kw):
    return Sessions(b"k" * 32, **kw)


def test_session_roundtrip_and_expiry():
    s = sess(session_seconds=100)
    fp = fingerprint(hash_password(PW))
    t = s.issue("nick", fp, now=1000)
    assert s.parse(t, now=1050) == ("nick", fp)
    assert s.parse(t, now=1101) is None


@pytest.mark.parametrize("bad", [None, "", "abc", "a.b.c", "x.y"])
def test_garbage_tokens_rejected(bad):
    assert sess().parse(bad) is None


def test_tampered_or_foreign_tokens_rejected():
    s = sess()
    t = s.issue("nick", "fp")
    body, sig = t.split(".")
    assert s.parse(body + "." + sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")) is None
    assert s.parse("AAAA" + t[4:]) is None
    assert Sessions(b"z" * 32).parse(t) is None


def test_fingerprint_changes_with_password():
    assert fingerprint(hash_password(PW)) != fingerprint(hash_password(PW))  # new salt -> new hash -> new fp


# ---- limiter ----
def test_limiter_blocks_after_max_failures_then_recovers():
    lim = LoginLimiter(max_fail=3, window=100)
    for i in range(3):
        assert lim.blocked_for("ip", now=i) == 0
        lim.fail("ip", now=i)
    assert lim.blocked_for("ip", now=3) > 0
    assert lim.blocked_for("other", now=3) == 0
    assert lim.blocked_for("ip", now=101) == 0      # oldest failure aged out
    lim.success("ip")
    assert lim.blocked_for("ip", now=3) == 0


# ---- user store ----
@pytest.fixture
def store(tmp_path, monkeypatch):
    for k in ("MANIFEST_PASSWORD", "MANIFEST_PASSWORD_HASH", "MANIFEST_USERNAME"):
        monkeypatch.delenv(k, raising=False)
    s = UserStore(tmp_path / "users.json")
    s.load_or_seed()
    return s


def test_first_run_creates_default_admin_that_must_change(store):
    u = store.authenticate("admin", "manifest")
    assert u and u["role"] == "admin" and u["must_change"] and u["default"]
    assert store.authenticate("admin", "wrong") is None
    assert store.authenticate("nobody", "manifest") is None
    assert store.authenticate(" ADMIN ", "manifest")          # case/space-insensitive username


def test_change_password_clears_default_and_must_change(store):
    store.set_password("admin", "my brand new passphrase", must_change=False)
    assert store.authenticate("admin", "manifest") is None
    u = store.authenticate("admin", "my brand new passphrase")
    assert u and not u["must_change"] and not u["default"]


def test_state_survives_restart(store, tmp_path):
    store.set_password("admin", "my brand new passphrase", must_change=False)
    store.create("viewer1", "temporary password", "viewer")
    again = UserStore(tmp_path / "users.json")
    again.load_or_seed()
    assert again.authenticate("admin", "my brand new passphrase")
    assert again.authenticate("viewer1", "temporary password")["must_change"]
    assert again.authenticate("admin", "manifest") is None    # default not re-created


def test_new_users_must_change_password_and_validation(store):
    store.create("nick", "a temporary passphrase", "admin")
    u = store.authenticate("nick", "a temporary passphrase")
    assert u["must_change"] and u["role"] == "admin" and not u["default"]
    for args in [("nick", "another passphrase!", "viewer"),     # duplicate
                 ("ab", "another passphrase!", "viewer"),       # bad username
                 ("fresh", "short", "viewer"),                  # weak password
                 ("fresh", "another passphrase!", "root")]:     # bad role
        with pytest.raises(ValueError):
            store.create(*args)


def test_delete_rules(store):
    store.create("vee1", "a temporary passphrase", "viewer")
    store.delete("vee1")
    assert store.get("vee1") is None
    with pytest.raises(ValueError):
        store.delete("admin")          # last administrator
    with pytest.raises(ValueError):
        store.delete("ghost")


def test_api_keys_lifecycle(store, tmp_path):
    assert not store.any_api_keys()
    k = store.create_api_key("admin", "Homepage")
    assert k["key"].startswith("mnf_") and k["prefix"] == k["key"][:8] and "hash" not in k
    assert store.any_api_keys() and store.find_key(k["key"]) == "admin"
    assert store.find_key(k["key"] + "x") is None and store.find_key("") is None
    listing = store.list_api_keys("admin")
    assert len(listing) == 1 and "hash" not in listing[0] and "key" not in listing[0]
    assert listing[0]["last_used"]                                  # recorded on use
    assert k["key"] not in (tmp_path / "users.json").read_text()    # only a hash on disk
    store.revoke_api_key("admin", k["id"])
    assert store.find_key(k["key"]) is None and not store.any_api_keys()
    with pytest.raises(ValueError):
        store.revoke_api_key("admin", k["id"])


def test_api_key_rules(store):
    for bad in ["", "   ", "x" * 41]:
        with pytest.raises(ValueError):
            store.create_api_key("admin", bad)
    for i in range(5):
        store.create_api_key("admin", f"k{i}")
    with pytest.raises(ValueError):
        store.create_api_key("admin", "one too many")
    with pytest.raises(ValueError):
        store.create_api_key("ghost", "x")


def test_api_keys_belong_to_their_user(store, tmp_path):
    store.create("vee3", "a temporary passphrase", "viewer")
    mine = store.create_api_key("admin", "a")
    theirs = store.create_api_key("vee3", "b")
    assert store.find_key(mine["key"]) == "admin" and store.find_key(theirs["key"]) == "vee3"
    with pytest.raises(ValueError):
        store.revoke_api_key("admin", theirs["id"])                 # can't revoke someone else's
    assert store.public_list()[1]["api_keys"] == 1
    store.revoke_all_keys("vee3")
    assert store.find_key(theirs["key"]) is None and store.find_key(mine["key"]) == "admin"
    store.create_api_key("vee3", "again")
    store.delete("vee3")                                             # deleting the user kills the key
    assert store.find_key(theirs["key"]) is None
    again = UserStore(tmp_path / "users.json"); again.load_or_seed()
    assert again.find_key(mine["key"]) == "admin"                    # persisted across restart


def test_set_role_and_last_admin_protection(store):
    store.create("vee2", "a temporary passphrase", "viewer")
    store.set_role("vee2", "admin")
    assert store.get("vee2")["role"] == "admin"
    store.set_role("admin", "viewer")                 # fine: vee2 is also an admin now
    with pytest.raises(ValueError):
        store.set_role("vee2", "viewer")              # would leave no administrator
    with pytest.raises(ValueError):
        store.set_role("vee2", "root")
    with pytest.raises(ValueError):
        store.set_role("ghost", "admin")


def test_reset_sets_temporary_password_and_signs_old_sessions_out(store):
    store.create("vee1", "a temporary passphrase", "viewer")
    old_fp = fingerprint(store.get("vee1")["hash"])
    store.set_password("vee1", "another temp passphrase", must_change=True)
    assert fingerprint(store.get("vee1")["hash"]) != old_fp
    assert store.authenticate("vee1", "another temp passphrase")["must_change"]


def test_seed_from_env_keeps_chosen_password(tmp_path, monkeypatch):
    monkeypatch.setenv("MANIFEST_USERNAME", "Nick")
    monkeypatch.setenv("MANIFEST_PASSWORD", PW)
    monkeypatch.delenv("MANIFEST_PASSWORD_HASH", raising=False)
    s = UserStore(tmp_path / "users.json")
    s.load_or_seed()
    u = s.authenticate("nick", PW)
    assert u and not u["must_change"] and not u["default"]
    assert s.authenticate("admin", "manifest") is None


def test_public_list_hides_hashes(store):
    assert all("hash" not in u for u in store.public_list())
