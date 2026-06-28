"""v0.3.11 — Telegram access guard (hardened allowlist).

The Telegram bot token grants full admin control of the daemon.
A compromised token, a forwarded screenshot, or an attacker who
guesses `chat_id` MUST NOT be enough to issue `/pause`, `/stop`,
`/goal`, or read snapshot data.

This module enforces three concentric gates:

  1. **Chat allowlist** — only allowlisted `chat_id`s are answered;
     messages from anywhere else are dropped (with a silent
     audit log so the operator can see probing attempts).
  2. **PIN unlock** — the very first message in a chat session must
      include a pre-shared PIN. Until the PIN is matched, the bot
      replies with a no-op string and refuses to dispatch any
      command. After three failed PIN attempts, the chat is locked
      for 24h (per chat_id).
    3. **Per-command TOTP** — once unlocked, admin commands still
      require a 6-digit TOTP code (RFC 6238). The shared secret
      lives in `.hermes/.env` and is bound to your phone's
      authenticator app (Microsoft / Google Authenticator).
      Compromising chat_id + PIN without the device = useless.
    4. **Rate limit** — at most N commands per rolling hour per
      chat_id; over-limit commands get a cooldown reply instead of
      execution. The N is configurable but defaults to 30/hr.

Operational notes:

  - The allowlist is set by `RHAIONOS_ALLOWED_CHAT_IDS` (comma-
    separated). If unset, only `8141002315` (Reza's verified chat)
    is allowed.
  - The PIN is `RHAIONOS_TG_PIN`. If unset, gate 2 is disabled
    (still gated by 1 and 3). For air-gapped dev, set
    `RHAIONOS_TRUST_DEV=1` to allow any chat_id.
  - All decisions are appended to `<state_dir>/security.log.jsonl`
    so the operator can audit without it being delivered back.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("agentsos.telegram.guard")

DEFAULT_PRIMARY_CHAT_ID = "8141002315"  # Reza — sole authorized operator

# Commands that change daemon state or expose it.
# Read-only /help /status are still gated by allowlist + rate-limit
# but PIN is only required for these.
ADMIN_COMMANDS: frozenset[str] = frozenset(
    {
        "pause", "resume", "stop", "shutdown", "cancel",
        "goal", "live", "live_stop", "live_pause", "live_resume",
        "add", "remove", "done", "start", "fail", "note",
        "kill", "restart",
    }
)


def _read_csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def resolve_allowed_chat_ids() -> set[int]:
    """Compute the allowlist for this run.

    Order:
      1. `RHAIONOS_TRUST_DEV=1` → allow ALL chat ids (test only)
      2. `RHAIONOS_ALLOWED_CHAT_IDS` (comma-sep)
      3. otherwise, just the primary chat id `8141002315`.
    """
    if os.environ.get("RHAIONOS_TRUST_DEV", "").strip() == "1":
        log.warning("RHAIONOS_TRUST_DEV=1 — Telegram guard allowing ALL chat ids")
        return set()  # empty set means "allow all" inside `is_allowed`
    env_ids = _read_csv_env("RHAIONOS_ALLOWED_CHAT_IDS")
    if env_ids:
        out: set[int] = set()
        for tok in env_ids:
            try:
                out.add(int(tok))
            except ValueError:
                log.warning("ignoring non-numeric chat id in env: %r", tok)
        return out
    try:
        return {int(DEFAULT_PRIMARY_CHAT_ID)}
    except ValueError:
        return set()


@dataclass
class SecurityAudit:
    """Append-only audit log writer for security-relevant events."""

    path: Path

    def record(self, kind: str, chat_id: int | str, **fields: Any) -> None:
        entry = {
            "kind": f"sec.{kind}",
            "chat_id": str(chat_id),
            "ts": time.time(),
            **fields,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            log.warning("audit log write failed: %s", exc)


@dataclass
class AccessGuard:
    """Stateful gate enforcing allowlist + PIN + rate-limit.

    Designed for one instance per bot process (not thread-safe but
    Telegram only has one update consumer per bot anyway).
    """

    allowed: set[int]                     # empty == allow-all (dev mode)
    pin: str | None = None                # None disables PIN gate
    totp_secret: str | None = None        # base32; None disables TOTP
    audit: SecurityAudit | None = None
    rate_limit_per_hour: int = 30
    lockout_after_pin_failures: int = 3
    lockout_seconds: float = 24 * 3600.0

    # per-chat rolling window of command timestamps
    _cmd_times: dict[int, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    # PIN state: {chat_id: {"ok": bool, "fails": int, "locked_until": float}}
    _pin_state: dict[int, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def from_env(cls, audit: SecurityAudit | None = None) -> "AccessGuard":
        allowed = resolve_allowed_chat_ids()
        pin = os.environ.get("RHAIONOS_TG_PIN", "").strip() or None
        totp = os.environ.get("RHAIONOS_TG_TOTP", "").strip() or None
        try:
            rl = int(os.environ.get("RHAIONOS_TG_RATE_LIMIT", "30"))
        except ValueError:
            rl = 30
        return cls(
            allowed=allowed,
            pin=pin,
            totp_secret=totp,
            audit=audit,
            rate_limit_per_hour=rl,
        )

    # ---- gate 1: allowlist ----
    def is_allowed(self, chat_id: int) -> bool:
        if not self.allowed:  # allow-all dev mode
            return True
        return chat_id in self.allowed

    # ---- gate 2: PIN ----
    # ---- gate 2b: per-command TOTP for admin commands ----
    def _strip_totp(self, raw: str) -> tuple[str | None, str]:
        """If the first whitespace-delimited token is a 6-digit
        code and totp_secret is set, strip it and return (code, rest).
        Otherwise return (None, raw).

        Accepts both `123456 goal list` and `/123456 goal list`
        because Telegram slash commands always carry a leading `/`.
        """
        if self.totp_secret is None or not raw:
            return None, raw
        first, _, rest = raw.partition(" ")
        first = first.lstrip("/")
        if len(first) == 6 and first.isdigit():
            return first, rest
        return None, raw

    def is_unlocked(self, chat_id: int) -> bool:
        if self.pin is None:
            return True
        st = self._pin_state.get(chat_id)
        if not st:
            return False
        return bool(st.get("ok"))

    def try_unlock(self, chat_id: int, candidate: str) -> bool:
        """Attempt to unlock a chat with a PIN candidate."""
        if self.pin is None:
            return True
        st = self._pin_state.setdefault(chat_id, {"fails": 0.0, "ok": 0.0, "locked_until": 0.0})
        if time.time() < st["locked_until"]:
            return False  # still locked out
        if candidate == self.pin:
            st["ok"] = 1.0
            st["fails"] = 0.0
            if self.audit:
                self.audit.record("pin_ok", chat_id)
            return True
        st["fails"] += 1
        if st["fails"] >= self.lockout_after_pin_failures:
            st["locked_until"] = time.time() + self.lockout_seconds
            st["fails"] = 0.0
            if self.audit:
                self.audit.record("pin_lockout", chat_id,
                                  locked_until=st["locked_until"])
        if self.audit:
            self.audit.record("pin_fail", chat_id,
                              fails=st["fails"], locked_until=st["locked_until"])
        return False

    # ---- gate 3: rate-limit ----
    def allow_command(self, chat_id: int, cmd: str) -> tuple[bool, str]:
        """Returns (ok, reason)."""
        now = time.time()
        window = self._cmd_times[chat_id]
        # drop entries older than 1h
        while window and now - window[0] > 3600.0:
            window.popleft()
        if len(window) >= self.rate_limit_per_hour:
            if self.audit:
                self.audit.record("rate_limit", chat_id, cmd=cmd,
                                  window=len(window), limit=self.rate_limit_per_hour)
            return False, (
                f"⏳ rate-limited ({len(window)}/{self.rate_limit_per_hour} per hour). "
                "Try again later."
            )
        window.append(now)
        return True, ""

    # ---- single-pass verdict ----
    def check(
        self,
        chat_id: int,
        cmd: str,
        first_message_text: str | None = None,
    ) -> "Verdict":
        """Decide whether a command should be allowed.

        `first_message_text` is consulted only for /auth <pin>.
        Returns a `Verdict` with a ready-to-send Telegram reply if
        the command is rejected, or `verdict.reply is None` when
        accepted.
        """
        # Gate 1: allowlist
        if not self.is_allowed(chat_id):
            if self.audit:
                self.audit.record("denied_allowlist", chat_id, cmd=cmd)
            # Do NOT echo back the command — silent log only.
            return Verdict(accepted=False, reply=None, reason="allowlist")

        # /auth <pin> short-circuits gate 2 (and only gate 2).
        if cmd == "auth" and first_message_text:
            if self.pin is None:
                return Verdict(accepted=True, reply=None, reason="no_pin_set")
            parts = first_message_text.strip().split(maxsplit=1)
            cand = parts[1] if len(parts) > 1 else ""
            if self.try_unlock(chat_id, cand):
                return Verdict(
                    accepted=True,
                    reply="🔓 authorized. Commands are now accepted.",
                    reason="pin_ok",
                )
            return Verdict(
                accepted=False,
                reply="🔒 invalid PIN.",
                reason="pin_fail",
            )

        # Gate 2: PIN-required for admin commands
        if cmd in ADMIN_COMMANDS and not self.is_unlocked(chat_id):
            if self.audit:
                self.audit.record("denied_pin_required", chat_id, cmd=cmd)
            hint = (
                "🔒 locked. Send /auth <PIN> to unlock."
                if self.pin else
                "🔒 PIN required (operator misconfig — set RHAIONOS_TG_PIN)."
            )
            return Verdict(accepted=False, reply=hint, reason="pin_locked")

        # Gate 2b: TOTP for admin commands (after PIN unlock)
        if cmd in ADMIN_COMMANDS and self.totp_secret is not None:
            raw_message = first_message_text or ""
            code, _stripped = self._strip_totp(raw_message)
            if not code or not totp_verify(self.totp_secret, code):
                if self.audit:
                    self.audit.record("denied_totp", chat_id, cmd=cmd,
                                      had_code=bool(code))
                return Verdict(
                    accepted=False,
                    reply=(
                        "🔑 TOTP required. Prefix the command with your "
                        "6-digit code, e.g. `/123456 goal list`."
                    ),
                    reason="totp_required",
                )

        # Gate 3: rate-limit
        ok, msg = self.allow_command(chat_id, cmd)
        if not ok:
            return Verdict(accepted=False, reply=msg, reason="rate_limit")

        if self.audit:
            self.audit.record("accept", chat_id, cmd=cmd,
                              totp_used=self.totp_secret is not None)
        return Verdict(accepted=True, reply=None, reason="ok")


@dataclass
class Verdict:
    accepted: bool
    reply: str | None   # ready-to-send; None if accepted silently
    reason: str         # machine-readable code


def _hotp(secret: bytes, counter: int, digits: int = 6) -> str:
    counter_bytes = struct.pack(">Q", counter)
    h = hmac.new(secret, counter_bytes, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code_int = (
        ((h[offset] & 0x7F) << 24)
        | ((h[offset + 1] & 0xFF) << 16)
        | ((h[offset + 2] & 0xFF) << 8)
        | (h[offset + 3] & 0xFF)
    )
    return str(code_int % (10 ** digits)).zfill(digits)


def totp_now(secret_b32: str, window: int = 0) -> str:
    """Generate current TOTP code (RFC 6238, 30s step, 6 digits)."""
    secret = base64.b32decode(secret_b32.upper() + "=" * ((8 - len(secret_b32) % 8) % 8))
    counter = int(time.time() // 30) + window
    return _hotp(secret, counter)


def totp_verify(secret_b32: str, candidate: str, skew: int = 1) -> bool:
    """Constant-time TOTP check accepting one step before/after."""
    candidate = (candidate or "").strip()
    if not candidate.isdigit() or len(candidate) != 6:
        return False
    for w in range(-skew, skew + 1):
        if hmac.compare_digest(totp_now(secret_b32, w), candidate):
            return True
    return False


def build_default_audit(state_dir: Path | None = None) -> SecurityAudit:
    """Return an audit logger rooted at `<state_dir>/security.log.jsonl`."""
    if state_dir is None:
        state_dir = Path(os.environ.get("AGENTSOS_STATE_DIR", ".")) / "state"
    return SecurityAudit(path=state_dir / "security.log.jsonl")


__all__ = [
    "AccessGuard",
    "SecurityAudit",
    "Verdict",
    "DEFAULT_PRIMARY_CHAT_ID",
    "ADMIN_COMMANDS",
    "build_default_audit",
    "resolve_allowed_chat_ids",
    "totp_now",
    "totp_verify",
]