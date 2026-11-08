import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import totp
from app.auth import Sessions, fingerprint, hash_password
from app.users import UserStore

# RFC 6238 appendix B secret ("12345678901234567890") and SHA-1 vectors (6-digit tails of the 8-digit values)
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
RFC_VECTORS = [(59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
               (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130")]


@pytest.mark.parametrize("t,code", RFC_VECTORS)
def test_rfc6238_vectors(t, code):
    assert totp.hotp(RFC_SECRET, t // 30) == code


def test_verify_accepts_current_and_adjacent_steps_only():
    now = 1_700_000_000
    step = totp.current_step(now)
    for s, ok in [(step, True), (step - 1, True), (step + 1, True), (step - 2, False), (step + 2, False)]:
        assert (totp.verify(RFC_SECRET, totp.hotp(RFC_SECRET, s), 0, now=now) is not None) is ok


def test_verify_rejects_replay_and_garbage():
    now = 1_700_000_000
    code = totp.hotp(RFC_SECRET, totp.current_step(now))
    step = totp.verify(RFC_SECRET, code, 0, now=now)
    assert step == totp.current_step(now)
    assert totp.verify(RFC_SECRET, code, step, now=now) is None      # same code again
    for bad in ["", "abc123", "12345", "1234567", None]:
        assert totp.verify(RFC_SECRET, bad, 0, now=now) is None
    assert totp.verify(RFC_SECRET, " " + code[:3] + " " + code[3:], 0, now=now)   # spaces are fine


def test_new_secret_and_uri():
    s = totp.new_secret()
    assert len(s) == 32 and s.isalnum() and s == s.upper()
    assert totp.new_secret() != s
    uri = totp.otpauth_uri(s, "nick p", "Manifest")
    assert uri.startswith("otpauth://totp/Manifest%3Anick%20p?secret=" + s) and "issuer=Manifest" in uri


def test_challenge_tokens_are_not_sessions():
    sess = Sessions(b"k" * 32)
    fp = fingerprint(hash_password("x"))
    challenge = sess.issue("nick", fp, purpose="2fa", seconds=300)
    assert sess.parse(challenge, purpose="2fa") == ("nick", fp)
    assert sess.parse(challenge) is None                      # cannot be used as a login session
    assert sess.parse(sess.issue("nick", fp), purpose="2fa") is None
    assert sess.parse(challenge, now=10 ** 12, purpose="2fa") is None   # expired


@pytest.fixture
def store(tmp_path, monkeypatch):
    for k in ("MANIFEST_PASSWORD", "MANIFEST_PASSWORD_HASH", "MANIFEST_USERNAME"):
        monkeypatch.delenv(k, raising=False)
    s = UserStore(tmp_path / "users.json")
    s.load_or_seed()
    return s


def enrol(store):
    secret = store.begin_totp("admin")
    return secret, store.confirm_totp("admin", totp.hotp(secret, totp.current_step()))


def test_enrolment_requires_a_valid_code(store):
    secret = store.begin_totp("admin")
    assert not store.has_totp("admin")                       # pending only
    with pytest.raises(ValueError):
        store.confirm_totp("admin", "000000" if totp.hotp(secret, totp.current_step()) != "000000" else "111111")
    assert not store.has_totp("admin")
    codes = store.confirm_totp("admin", totp.hotp(secret, totp.current_step()))
    assert store.has_totp("admin") and len(codes) == 8 and len(set(codes)) == 8
    assert store.totp_status("admin") == {"enabled": True, "recovery_remaining": 8}
    with pytest.raises(ValueError):
        store.begin_totp("admin")                             # already on


def test_confirm_without_begin_fails(store):
    with pytest.raises(ValueError):
        store.confirm_totp("admin", "123456")


def test_login_code_works_once_per_step(store):
    secret, _ = enrol(store)
    # the enrolment code consumed this step, so the same code cannot log in again
    assert not store.check_second_factor("admin", totp.hotp(secret, totp.current_step()))
    nxt = totp.hotp(secret, totp.current_step() + 1)          # next step is within the drift window
    assert store.check_second_factor("admin", nxt)
    assert not store.check_second_factor("admin", nxt)        # replay refused


def test_recovery_codes_are_single_use_and_forgiving_of_format(store):
    _, codes = enrol(store)
    c = codes[0]
    assert store.check_second_factor("admin", c.upper().replace("-", " "))
    assert not store.check_second_factor("admin", c)          # used up
    assert store.totp_status("admin")["recovery_remaining"] == 7
    assert not store.check_second_factor("admin", "aaaaaa-bbbbbb")
    assert store.check_second_factor("admin", codes[1])


def test_wrong_and_unenrolled(store):
    assert not store.check_second_factor("admin", "123456")   # no 2FA set up
    enrol(store)
    assert not store.check_second_factor("admin", "")
    assert not store.check_second_factor("nobody", "123456")


def test_disable_clears_everything_and_persists(store, tmp_path):
    enrol(store)
    store.disable_totp("admin")
    assert not store.has_totp("admin")
    again = UserStore(tmp_path / "users.json"); again.load_or_seed()
    assert not again.has_totp("admin")
    assert "totp_secret" not in again.users["admin"] and "recovery" not in again.users["admin"]


def test_state_persists_and_secrets_not_listed(store, tmp_path):
    enrol(store)
    again = UserStore(tmp_path / "users.json"); again.load_or_seed()
    assert again.has_totp("admin")
    listing = again.public_list()
    assert listing[0]["totp"] is True
    assert "totp_secret" not in str(listing) and "recovery" not in str(listing)
    assert "hash" not in str(listing)


def test_password_change_keeps_2fa(store):
    enrol(store)
    store.set_password("admin", "a brand new passphrase", must_change=False)
    assert store.has_totp("admin")
