"""Persistent, bounded consumer for the three-contact friends pilot."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from tools.whatsapp_ops_batch import (
    _conn,
    _digest,
    _iso,
    acquire_friends_conversation_lease,
    freeze_friends_message_plan,
)
from tools.whatsapp_ops_conversation import FriendsHermesGenerator


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _friends_enabled() -> bool:
    from hermes_cli.config import load_config

    cfg = load_config()
    return bool(
        isinstance(cfg, dict)
        and isinstance(cfg.get("friends_pilot"), dict)
        and cfg["friends_pilot"].get("enabled") is True
    )


def _optout(text: str) -> str:
    value = text.casefold().strip()
    exact = {
        "pare",
        "parar",
        "não quero",
        "nao quero",
        "remover",
        "sair",
        "stop",
        "cancelar",
    }
    if value in exact:
        return "stop"
    if any(x in value for x in ("pare", "par", "não", "nao", "sair")):
        return "ambiguous"
    return ""


class FriendsBatchDispatcher:
    """One local service consumes durable work; no cron or second gateway needed."""

    def __init__(
        self,
        *,
        profile_id: str = "default",
        generator: Any = None,
        send_client: Callable | None = None,
        send_config: dict[str, Any] | None = None,
        clock: Callable[[], datetime] = _now,
        enabled: Callable[[], bool] = _friends_enabled,
    ) -> None:
        self.profile_id, self.generator, self.send_client, self.send_config = (
            profile_id,
            generator or FriendsHermesGenerator(profile_id=profile_id),
            send_client,
            send_config,
        )
        self.clock, self.enabled = clock, enabled
        self._stop = asyncio.Event()

    def run_once(self, limit: int = 3) -> int:
        if not self.enabled():
            return 0
        now = self.clock()
        processed = 0
        with _conn() as conn:
            rows = conn.execute(
                "SELECT c.*,g.payload_json,g.offer_digest,g.expires_at FROM friends_conversations c JOIN friends_grants g ON g.grant_id=c.grant_id WHERE c.profile_id=? AND c.status='pending' AND c.due_at<=? AND g.status='active' ORDER BY c.due_at LIMIT ?",
                (self.profile_id, now.isoformat(), max(1, min(limit, 3))),
            ).fetchall()
        for row in rows:
            if self._process(dict(row), now):
                processed += 1
        return processed

    def _pause(self, row: dict[str, Any], reason: str) -> None:
        with _conn() as conn:
            conn.execute(
                "UPDATE friends_conversations SET status='paused' WHERE profile_id=? AND grant_id=? AND contact_id=? AND channel_id=?",
                (
                    self.profile_id,
                    row["grant_id"],
                    row["contact_id"],
                    row["channel_id"],
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO friends_qualifications VALUES (?,?,?,?,?,?,?)",
                (
                    row["grant_id"],
                    row["contact_id"],
                    row["channel_id"],
                    "escalate",
                    1,
                    json.dumps({"reason": reason}),
                    self.clock().isoformat(),
                ),
            )

    def _process(self, row: dict[str, Any], now: datetime) -> bool:
        with _conn() as conn:
            events = conn.execute(
                "SELECT event_id,text,received_at FROM friends_inbound_queue WHERE profile_id=? AND grant_id=? AND contact_id=? AND channel_id=? AND status='pending' ORDER BY received_at",
                (
                    self.profile_id,
                    row["grant_id"],
                    row["contact_id"],
                    row["channel_id"],
                ),
            ).fetchall()
        if not events:
            return False
        stop = _optout(str(events[-1]["text"]))
        if stop:
            with _conn() as conn:
                if stop == "stop":
                    conn.execute(
                        "INSERT OR IGNORE INTO friends_suppressions VALUES (?,?,?,?)",
                        (
                            row["contact_id"],
                            row["channel_id"],
                            "opt_out",
                            now.isoformat(),
                        ),
                    )
                conn.execute(
                    "UPDATE friends_conversations SET status='paused' WHERE profile_id=? AND grant_id=? AND contact_id=? AND channel_id=?",
                    (
                        self.profile_id,
                        row["grant_id"],
                        row["contact_id"],
                        row["channel_id"],
                    ),
                )
                conn.execute(
                    "UPDATE friends_inbound_queue SET status='processed' WHERE grant_id=? AND contact_id=? AND channel_id=? AND status='pending'",
                    (row["grant_id"], row["contact_id"], row["channel_id"]),
                )
            return True
        messages = [{"role": "user", "text": str(e["text"])} for e in events]
        try:
            payload = json.loads(row["payload_json"])
            generated = self.generator.generate(
                grant_id=row["grant_id"],
                contact_id=row["contact_id"],
                channel_id=row["channel_id"],
                messages=messages,
                offer=payload.get("offer", {}),
                highwatermark=row["highwatermark"],
            )
        except Exception:
            generated = None
        if not generated or generated["action"] in {"escalate", "stop"}:
            self._pause(row, "generation_invalid_or_escalated")
            return True
        # Re-read highwatermark after the blocking provider call. A late result
        # can never overwrite a newer inbound turn.
        with _conn() as conn:
            current = conn.execute(
                "SELECT highwatermark FROM friends_conversations WHERE profile_id=? AND grant_id=? AND contact_id=? AND channel_id=?",
                (
                    self.profile_id,
                    row["grant_id"],
                    row["contact_id"],
                    row["channel_id"],
                ),
            ).fetchone()
        if not current or current["highwatermark"] != row["highwatermark"]:
            return False
        frozen = freeze_friends_message_plan(
            row["grant_id"],
            contact_id=row["contact_id"],
            channel_id=row["channel_id"],
            blocks=generated["blocks"],
            action=generated["action"],
            context_highwatermark=row["highwatermark"],
            offer_digest=row["offer_digest"],
        )
        if not frozen.get("ok"):
            self._pause(row, "freeze_failed")
            return True
        lease = acquire_friends_conversation_lease(frozen["plan_id"])
        if not lease.get("ok"):
            return False
        from tools.whatsapp_ops_tool import wpp_send_approved

        for _ in generated["blocks"]:
            if not self.enabled():
                break
            sent = json.loads(
                wpp_send_approved(
                    "",
                    batch_plan_id=frozen["plan_id"],
                    batch_lease_fence=lease["fence"],
                    send_client=self.send_client,
                    config=self.send_config,
                )
            )
            if not sent.get("ok"):
                self._pause(row, "send_failed")
                break
        with _conn() as conn:
            conn.execute(
                "UPDATE friends_inbound_queue SET status='processed' WHERE grant_id=? AND contact_id=? AND channel_id=? AND status='pending'",
                (row["grant_id"], row["contact_id"], row["channel_id"]),
            )
            conn.execute(
                "UPDATE friends_conversations SET processed_cursor=?,status='idle',followup_due_at=? WHERE profile_id=? AND grant_id=? AND contact_id=? AND channel_id=?",
                (
                    row["highwatermark"],
                    (now + timedelta(minutes=30)).isoformat(),
                    self.profile_id,
                    row["grant_id"],
                    row["contact_id"],
                    row["channel_id"],
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO friends_qualifications VALUES (?,?,?,?,?,?,?)",
                (
                    row["grant_id"],
                    row["contact_id"],
                    row["channel_id"],
                    generated["action"],
                    1,
                    json.dumps(generated["qualification"], ensure_ascii=False),
                    now.isoformat(),
                ),
            )
        return True

    async def serve(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
