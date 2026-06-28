"""v0.3.11 Telegram AccessGuard tests."""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import pytest

from agentsos.telegram.guard import (
    ADMIN_COMMANDS,
    AccessGuard,
    SecurityAudit,
    totp_now,
    totp_verify,
)


PRIMARY = 8141002315
ATTACKER = 9999999999


@pytest.fixture
def audit(tmp_path: Path) -> SecurityAudit:
    return SecurityAudit(path=tmp_path / "sec.jsonl")


@pytest.fixture
def guard(audit: SecurityAudit) -> AccessGuard:
    return AccessGuard(
        allowed={PRIMARY},
        pin="hunter2",
        totp_secret="JBSWY3DPEHPK3PXP",  # "Hello!" base32 — Google sample
        audit=audit,
        rate_limit_per_hour=5,
    )


def test_totp_roundtrip() -> None:
    secret = "JBSWY3DPEHPK3PXP"
    code = totp_now(secret)
    assert len(code) == 6
    assert totp_verify(secret, code)


def test_totp_rejects_wrong_code() -> None:
    assert not totp_verify("JBSWY3DPEHPK3PXP", "000000")


def test_allowlist_blocks_attacker(guard: AccessGuard) -> None:
    v = guard.check(ATTACKER, "status", first_message_text="/status")
    assert not v.accepted
    assert v.reason == "allowlist"
    assert v.reply is None  # silent drop


def test_primary_can_read_status(guard: AccessGuard) -> None:
    v = guard.check(PRIMARY, "status", first_message_text="/status")
    assert v.accepted
    assert v.reason == "ok"


def test_primary_cannot_run_admin_without_pin(guard: AccessGuard) -> None:
    v = guard.check(PRIMARY, "pause", first_message_text="/pause now")
    assert not v.accepted
    assert v.reason == "pin_locked"
    assert "auth" in (v.reply or "")


def test_unlock_with_correct_pin(guard: AccessGuard) -> None:
    v = guard.check(PRIMARY, "auth", first_message_text="/auth hunter2")
    assert v.accepted
    assert v.reason == "pin_ok"
    assert guard.is_unlocked(PRIMARY)


def test_unlock_with_wrong_pin_locks_after_three(guard: AccessGuard) -> None:
    for _ in range(3):
        v = guard.check(PRIMARY, "auth", first_message_text="/auth wrong")
        assert not v.accepted
    # now locked
    assert not guard.is_unlocked(PRIMARY)
    v = guard.check(PRIMARY, "auth", first_message_text="/auth hunter2")
    assert not v.accepted
    assert v.reason == "pin_fail"


def test_admin_command_needs_totp_after_unlock(audit: SecurityAudit) -> None:
    g = AccessGuard(
        allowed={PRIMARY},
        pin="hunter2",
        totp_secret="JBSWY3DPEHPK3PXP",
        audit=audit,
    )
    assert g.check(PRIMARY, "auth", first_message_text="/auth hunter2").accepted
    # Now /goal without TOTP
    v = g.check(PRIMARY, "goal", first_message_text="/goal list")
    assert not v.accepted
    assert v.reason == "totp_required"
    # With valid TOTP prefix
    code = totp_now("JBSWY3DPEHPK3PXP")
    v = g.check(PRIMARY, "goal", first_message_text=f"/{code} goal list")
    assert v.accepted


def test_admin_command_without_totp_env_no_totp_required(audit: SecurityAudit) -> None:
    g = AccessGuard(
        allowed={PRIMARY}, pin="hunter2", totp_secret=None, audit=audit,
    )
    g.check(PRIMARY, "auth", first_message_text="/auth hunter2")
    v = g.check(PRIMARY, "goal", first_message_text="/goal list")
    assert v.accepted  # no TOTP gate


def test_rate_limit_blocks_after_n(audit: SecurityAudit) -> None:
    g = AccessGuard(
        allowed={PRIMARY}, pin=None, totp_secret=None, audit=audit,
        rate_limit_per_hour=3,
    )
    for _ in range(3):
        assert g.check(PRIMARY, "status", "/status").accepted
    v = g.check(PRIMARY, "status", "/status")
    assert not v.accepted
    assert v.reason == "rate_limit"
    assert "rate" in (v.reply or "")


def test_auth_command_short_circuits_pin_gate(audit: SecurityAudit) -> None:
    g = AccessGuard(
        allowed={PRIMARY}, pin="hunter2", totp_secret=None, audit=audit,
    )
    # Even without PIN unlocked, /auth should be allowed through so user can unlock
    v = g.check(PRIMARY, "auth", first_message_text="/auth hunter2")
    assert v.accepted
    assert v.reason == "pin_ok"


def test_audit_log_records_decisions(guard: AccessGuard, audit: SecurityAudit) -> None:
    guard.check(ATTACKER, "status", "/status")     # allowlist deny
    guard.check(PRIMARY, "pause", "/pause now")    # pin-locked
    guard.check(PRIMARY, "auth", "/auth hunter2")  # unlock
    guard.check(PRIMARY, "status", "/status")      # accept
    lines = audit.path.read_text(encoding="utf-8").strip().splitlines()
    kinds = [__import__("json").loads(ln)["kind"] for ln in lines]
    assert "sec.denied_allowlist" in kinds
    assert "sec.denied_pin_required" in kinds
    assert "sec.pin_ok" in kinds
    assert "sec.accept" in kinds


def test_trust_dev_mode_allows_all(audit: SecurityAudit, monkeypatch) -> None:
    monkeypatch.setenv("RHAIONOS_TRUST_DEV", "1")
    # Reset cached allowlist via fresh guard that re-reads env.
    g = AccessGuard.from_env(audit=audit)
    assert g.is_allowed(ATTACKER) is True


def test_admin_commands_comprehensively_cover_state_changes() -> None:
    """Sanity: every admin cmd in the set is something that needs TOTP."""
    for cmd in ("pause", "resume", "stop", "shutdown", "cancel",
                "goal", "live", "live_stop", "add", "remove",
                "done", "start", "fail", "note"):
        assert cmd in ADMIN_COMMANDS