"""Private authority object minted only after Telegram callback authentication."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _AuthenticatedFriendsAuthority:
    profile_id: str
    chat_id: str
    thread_id: str
    operator_id: str
    pending_id: str
    envelope_digest: str


def issue_authenticated_friends_authority(*, profile_id: str, chat_id: str, thread_id: str | None,
                                          operator_id: str, pending_id: str, envelope_digest: str) -> object:
    """Internal Telegram-adapter bridge; not a model/public approval API."""
    return _AuthenticatedFriendsAuthority(
        profile_id=str(profile_id or ""), chat_id=str(chat_id or ""),
        thread_id=str(thread_id or ""), operator_id=str(operator_id or ""),
        pending_id=str(pending_id or ""), envelope_digest=str(envelope_digest or ""),
    )


def authority_binding(authority: object) -> dict[str, str] | None:
    if not isinstance(authority, _AuthenticatedFriendsAuthority):
        return None
    values = authority.__dict__.copy()
    return values if all(values.values()) else None
