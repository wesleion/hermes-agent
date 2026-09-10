import asyncio
from datetime import datetime, timedelta, timezone

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


class _Query:
    def __init__(self, data, user_id="7"):
        self.data = data
        self.from_user = type("User", (), {"id": user_id, "first_name": "Operator"})()
        self.message = type("Message", (), {"chat_id": "42", "message_thread_id": "9", "chat": type("Chat", (), {"type": "private"})()})()
        self.answers = []
    async def answer(self, text=""):
        self.answers.append(text)
    async def edit_message_reply_markup(self, **_kwargs):
        return None


def test_authenticated_telegram_callback_is_the_only_activation_boundary(tmp_path):
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from tools.whatsapp_ops_batch import friends_grant_status, persist_friends_pending, prepare_friends_envelope
    from tools.whatsapp_ops_store import list_contact_channels, register_contact_local
    token = set_hermes_home_override(tmp_path)
    try:
        rows=[register_contact_local(alias=f"friend-{i}",raw_ref=f"5511888800{i}@s.whatsapp.net",allow_send=True) for i in range(3)]
        contacts=[{"contact_id":r["contact_id"],"channel_id":list_contact_channels(r["contact_id"])[0]["channel_id"]} for r in rows]
        now=datetime.now(timezone.utc)
        preview=prepare_friends_envelope(campaign_id="friends",contacts=contacts,offer={},playbook={},issuer="telegram:7",starts_at=now.isoformat(),expires_at=(now+timedelta(minutes=10)).isoformat())
        pending=persist_friends_pending(preview,profile_id=str(tmp_path),chat_id="42",thread_id="9",operator_id="7")
        adapter=type("FakeTelegram", (), {"name":"telegram", "_is_callback_user_authorized":lambda self,*_args,**_kwargs: True})()
        query=_Query(f"wppf:a:{pending['pending_id']}")
        asyncio.run(TelegramAdapter._handle_callback_query(adapter,type("Update",(),{"callback_query":query})(),None))
        # The safe status lookup proves grant creation without exposing contacts.
        from tools.whatsapp_ops_batch import _conn
        with _conn() as conn: grant_id=conn.execute("SELECT grant_id FROM friends_grants").fetchone()["grant_id"]
        assert friends_grant_status(grant_id)["status"]=="active" and query.answers == ["Friends grant approved."]
    finally:
        reset_hermes_home_override(token)
