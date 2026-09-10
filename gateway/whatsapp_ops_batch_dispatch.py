"""Bounded, profile-scoped consumer; all WhatsApp sends use the existing chokepoint."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from tools.whatsapp_ops_batch import friends_profile_id
from tools.whatsapp_ops_batch import (
    _conn,
    _init,
    _iso,
    acquire_friends_conversation_lease,
    freeze_friends_message_plan,
)
from tools.whatsapp_ops_conversation import (
    FriendsHermesGenerator,
    validate_friends_generation,
)
from tools.whatsapp_ops_friends_queue import history, maintain


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _friends_enabled() -> bool:
    from hermes_cli.config import load_config

    cfg = load_config()
    return (
        isinstance(cfg, dict)
        and isinstance(cfg.get("friends_pilot"), dict)
        and cfg["friends_pilot"].get("enabled") is True
    )


class FriendsBatchDispatcher:
    def __init__(
        self,
        *,
        profile_id: str | None = None,
        generator: Any = None,
        send_client: Callable | None = None,
        send_config: dict | None = None,
        clock: Callable = _now,
        enabled: Callable = _friends_enabled,
        generation_timeout_seconds: float = 90.0,
    ) -> None:
        self.profile_id = profile_id or friends_profile_id()
        self.generator = generator or FriendsHermesGenerator(profile_id=self.profile_id)
        self.send_client, self.send_config = send_client, send_config
        self.clock, self.enabled = clock, enabled
        self.timeout = max(0.01, min(90.0, float(generation_timeout_seconds)))
        self.operator_adapter = lambda: None
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._late: concurrent.futures.Future | None = None

    @staticmethod
    def _key(row) -> tuple:
        return (
            row["profile_id"],
            row["grant_id"],
            row["contact_id"],
            row["channel_id"],
        )

    def run_once(self, limit: int = 3) -> int:
        if (
            self._stop.is_set()
            or not self.enabled()
            or not self._busy.acquire(blocking=False)
        ):
            return 0
        try:
            _init()
            now = self.clock()
            with _conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                maintain(conn, self.profile_id, now)
            # A model ignoring interruption may finish late, but cannot send or
            # schedule more model threads. At most one such call exists here.
            if self._late is not None and not self._late.done():
                return 0
            with _conn() as conn:
                rows = conn.execute(
                    "SELECT c.*,g.payload_json,g.offer_digest,g.expires_at FROM friends_conversations c JOIN friends_grants g ON g.grant_id=c.grant_id WHERE c.profile_id=? AND c.status='pending' AND c.due_at<=? AND g.status='active' ORDER BY c.due_at,c.contact_id LIMIT ?",
                    (self.profile_id, now.isoformat(), max(1, min(3, limit))),
                ).fetchall()
            count = 0
            for row in rows:
                if self._stop.is_set() or (
                    self._late is not None and not self._late.done()
                ):
                    break
                count += int(self._process(dict(row)))
            return count
        finally:
            self._busy.release()

    def _claim(self, row) -> str | None:
        fence = secrets.token_urlsafe(18)
        now = self.clock()
        expiry = (now + timedelta(seconds=180)).isoformat()
        with _conn() as conn:
            changed = conn.execute(
                "UPDATE friends_conversations SET status='generating',lease_fence_hash=?,lease_expires_at=? WHERE profile_id=? AND grant_id=? AND contact_id=? AND channel_id=? AND status='pending' AND highwatermark=? AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
                (fence, expiry, *self._key(row), row["highwatermark"], now.isoformat()),
            ).rowcount
        return fence if changed == 1 else None

    def _current(self, row, fence) -> bool:
        if self._stop.is_set() or not self.enabled():
            return False
        with _conn() as conn:
            value = conn.execute(
                "SELECT c.*,g.status AS grant_status,g.expires_at FROM friends_conversations c JOIN friends_grants g ON g.grant_id=c.grant_id WHERE c.profile_id=? AND c.grant_id=? AND c.contact_id=? AND c.channel_id=?",
                self._key(row),
            ).fetchone()
            suppressed = conn.execute(
                "SELECT 1 FROM friends_suppressions WHERE contact_id=? AND channel_id=?",
                self._key(row)[2:],
            ).fetchone()
        return bool(
            value
            and value["status"] == "generating"
            and value["highwatermark"] == row["highwatermark"]
            and value["lease_fence_hash"] == fence
            and _iso(value["lease_expires_at"]) > self.clock()
            and value["grant_status"] == "active"
            and _iso(value["expires_at"]) > self.clock()
            and not suppressed
        )

    def _pause(self, row, reason, fence) -> None:
        with _conn() as conn:
            changed = conn.execute(
                "UPDATE friends_conversations SET status='paused',followup_due_at=NULL,lease_fence_hash=NULL,lease_expires_at=NULL WHERE profile_id=? AND grant_id=? AND contact_id=? AND channel_id=? AND lease_fence_hash=? AND highwatermark=?",
                (*self._key(row), fence, row["highwatermark"]),
            ).rowcount
            if changed:
                conn.execute(
                    "INSERT OR REPLACE INTO friends_qualifications VALUES (?,?,?,?,?,?,?)",
                    (
                        *self._key(row)[1:],
                        "escalate",
                        1,
                        json.dumps({"reason": reason}),
                        self.clock().isoformat(),
                    ),
                )

    def _generate(self, row):
        with _conn() as conn:
            messages = history(conn, row)
        context = contextvars.copy_context()
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="friends-generation"
        )
        future = pool.submit(
            context.run,
            self.generator.generate,
            grant_id=row["grant_id"],
            contact_id=row["contact_id"],
            channel_id=row["channel_id"],
            messages=messages,
            offer=json.loads(row["payload_json"]).get("offer", {}),
            highwatermark=row["highwatermark"],
            job_kind=row["job_kind"],
        )
        deadline = time.monotonic() + self.timeout
        try:
            while not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    return validate_friends_generation(
                        future.result(timeout=min(0.05, remaining))
                    )
                except concurrent.futures.TimeoutError:
                    continue
            self._late = future
            interrupt = getattr(self.generator, "interrupt", None)
            if callable(interrupt):
                interrupt()
            return None
        except Exception:
            # Provider-controlled exception details never enter commercial output.
            return None
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _process(self, row) -> bool:
        fence = self._claim(row)
        if not fence:
            return False
        if row["active_plan_id"] and row["generation_json"]:
            generated = validate_friends_generation(json.loads(row["generation_json"]))
            plan_id = row["active_plan_id"]
        else:
            generated = self._generate(row)
            if not self._current(row, fence):
                return False
            if not generated:
                self._pause(row, "generation_invalid_or_timeout", fence)
                return True
            if generated["action"] in {"escalate", "stop"}:
                self._pause(
                    row,
                    "commercial_exception"
                    if generated["action"] == "escalate"
                    else "commercial_stop",
                    fence,
                )
                if generated["action"] == "stop":
                    from tools.whatsapp_ops_batch import note_friends_optout

                    note_friends_optout(row["contact_id"], row["channel_id"])
                return True
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
                self._pause(row, "freeze_failed", fence)
                return True
            plan_id = frozen["plan_id"]
            with _conn() as conn:
                changed = conn.execute(
                    "UPDATE friends_conversations SET active_plan_id=?,generation_json=? WHERE profile_id=? AND grant_id=? AND contact_id=? AND channel_id=? AND lease_fence_hash=? AND highwatermark=?",
                    (
                        plan_id,
                        json.dumps(generated, ensure_ascii=False),
                        *self._key(row),
                        fence,
                        row["highwatermark"],
                    ),
                ).rowcount
            if not changed:
                return False
        if not generated:
            self._pause(row, "persisted_generation_invalid", fence)
            return True
        return self._send_plan(row, fence, plan_id, generated)

    def _send_plan(self, row, fence, plan_id, generated) -> bool:
        from tools.whatsapp_ops_tool import wpp_send_approved

        with _conn() as conn:
            plan = conn.execute(
                "SELECT status FROM friends_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
        if not plan:
            self._pause(row, "persisted_plan_missing", fence)
            return True
        if plan["status"] != "completed":
            lease = acquire_friends_conversation_lease(plan_id)
            if not lease.get("ok"):
                self._pause(row, "send_lease_unavailable", fence)
                return True
            for _ in range(len(generated["blocks"])):
                if not self._current(row, fence):
                    return False
                with _conn() as conn:
                    done = conn.execute(
                        "SELECT status FROM friends_plans WHERE plan_id=?", (plan_id,)
                    ).fetchone()
                if done["status"] == "completed":
                    break
                result = json.loads(
                    wpp_send_approved(
                        "",
                        batch_plan_id=plan_id,
                        batch_lease_fence=lease["fence"],
                        send_client=self.send_client,
                        config=self.send_config,
                    )
                )
                if not result.get("ok"):
                    self._pause(row, "send_failed_or_uncertain", fence)
                    return True
        return self._complete(row, fence, generated)

    def _complete(self, row, fence, generated) -> bool:
        now = self.clock()
        terminal = generated["action"] in ("brief", "refer")
        followup = (
            None
            if terminal or row["job_kind"] == "followup" or row["followup_sent"]
            else (now + timedelta(minutes=30)).isoformat()
        )
        with _conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE friends_conversations SET processed_cursor=?,status=?,followup_due_at=?,followup_sent=CASE WHEN job_kind='followup' THEN 1 ELSE followup_sent END,lease_fence_hash=NULL,lease_expires_at=NULL WHERE profile_id=? AND grant_id=? AND contact_id=? AND channel_id=? AND status='generating' AND lease_fence_hash=? AND highwatermark=?",
                (
                    row["highwatermark"],
                    "closed" if terminal else "idle",
                    followup,
                    *self._key(row),
                    fence,
                    row["highwatermark"],
                ),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE friends_inbound_queue SET status='processed' WHERE grant_id=? AND contact_id=? AND channel_id=? AND status='pending'",
                    self._key(row)[1:],
                )
                conn.execute(
                    "INSERT OR REPLACE INTO friends_qualifications VALUES (?,?,?,?,?,?,?)",
                    (
                        *self._key(row)[1:],
                        generated["action"],
                        1,
                        json.dumps(
                            {
                                "qualification": generated["qualification"],
                                "next_step": generated["next_step"],
                            },
                            ensure_ascii=False,
                        ),
                        now.isoformat(),
                    ),
                )
        return bool(changed)

    async def serve(self) -> None:
        while not self._stop.is_set():
            await asyncio.to_thread(self.run_once)
            if self.enabled() and not self._stop.is_set():
                from gateway.whatsapp_ops_friends_notifications import (
                    drain_friends_operator_events,
                )

                await drain_friends_operator_events(
                    self.profile_id, adapter=self.operator_adapter()
                )
            await asyncio.to_thread(self._stop.wait, 0.5)

    def stop(self) -> None:
        self._stop.set()
