"""Read-only operator view over the real pilot ledger; never creates a DB."""

from __future__ import annotations

import json
import sqlite3

from hermes_constants import get_hermes_home
from hermes_cli.whatsapp_ops_commands import _safe_text
from tools.whatsapp_ops_store import get_db_path


def friends_pilot_status(grant_id: str | None = None) -> dict:
    result = {
        "ok": True,
        "simulation": True,
        "counts": {"sent": 0, "pending": 0, "errors": 0},
        "grants": [],
        "conversations": [],
    }
    path = get_db_path()
    if not path.is_file():
        return result
    try:
        with sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            names = {
                x[0]
                for x in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "friends_grants" not in names:
                return result
            grants = conn.execute(
                "SELECT g.* FROM friends_grants g JOIN friends_pending_envelopes p ON p.envelope_digest=g.envelope_digest WHERE p.profile_id=? ORDER BY g.created_at DESC LIMIT 20",
                (get_hermes_home().name,),
            ).fetchall()
            for grant in grants:
                if grant_id and grant["grant_id"] != grant_id:
                    continue
                result["grants"].append({
                    k: grant[k]
                    for k in (
                        "grant_id",
                        "status",
                        "expires_at",
                        "messages_reserved",
                        "global_cap",
                    )
                })
                blocks = conn.execute(
                    "SELECT b.status,count(*) AS n FROM friends_blocks b JOIN friends_plans p ON p.plan_id=b.plan_id WHERE p.grant_id=? GROUP BY b.status",
                    (grant["grant_id"],),
                ).fetchall()
                for block in blocks:
                    key = (
                        "sent"
                        if block["status"] == "sent"
                        else (
                            "errors"
                            if block["status"] in ("uncertain", "failed")
                            else "pending"
                            if block["status"] in ("pending", "reserved")
                            else None
                        )
                    )
                    if key:
                        result["counts"][key] += block["n"]
                rows = conn.execute(
                    "SELECT cv.status,c.display_name,q.outcome,q.detail_json FROM friends_conversations cv JOIN contacts c ON c.id=cv.contact_id LEFT JOIN friends_qualifications q ON q.grant_id=cv.grant_id AND q.contact_id=cv.contact_id AND q.channel_id=cv.channel_id WHERE cv.grant_id=? ORDER BY c.display_name",
                    (grant["grant_id"],),
                ).fetchall()
                for row in rows:
                    detail = json.loads(row["detail_json"] or "{}")
                    result["conversations"].append({
                        "label": _safe_text(row["display_name"], max_len=80),
                        "state": row["status"],
                        "outcome": row["outcome"],
                        "next_step": _safe_text(
                            detail.get("next_step") or detail.get("reason") or "",
                            max_len=160,
                        ),
                    })
    except (sqlite3.Error, ValueError, TypeError):
        return {"ok": False, "error": "pilot_status_unavailable"}
    return result


def select_current_grant() -> str:
    status = friends_pilot_status()
    grants = status.get("grants", [])
    active = [x for x in grants if x["status"] == "active"]
    choices = active or grants[:1]
    if len(choices) != 1:
        raise ValueError("pilot_grant_ambiguous_or_missing")
    return choices[0]["grant_id"]


def render_friends_pilot_status() -> str:
    status = friends_pilot_status()
    if not status.get("ok"):
        return "Piloto: status indisponível."
    counts = status["counts"]
    lines = [
        f"Piloto simulado: {counts['sent']} mensagens confirmadas; {counts['pending']} pendentes; {counts['errors']} incertas/falhas."
    ]
    lines += [
        f"{row['label']}: {row['state']} — {row['next_step'] or row['outcome'] or 'aguardando'}"
        for row in status["conversations"]
    ]
    return "\n".join(lines)
