import json
from datetime import datetime, timedelta, timezone

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


class _Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self, *_): return b'{"success":true,"message":{"id":"provider-1"}}'


def test_canonical_sender_consumes_frozen_block_with_quepasa_receipt_and_flags(tmp_path, monkeypatch):
    from gateway.whatsapp_ops_batch_approval import issue_authenticated_friends_authority
    from tools.whatsapp_ops_batch import acquire_friends_conversation_lease, activate_friends_pending, freeze_friends_message_plan, persist_friends_pending, prepare_friends_envelope
    from tools.whatsapp_ops_quepasa import send_via_quepasa
    from tools.whatsapp_ops_store import list_contact_channels, register_contact_local
    from tools.whatsapp_ops_tool import wpp_send_approved
    token = set_hermes_home_override(tmp_path)
    try:
        rows=[register_contact_local(alias=f"friend-{i}",raw_ref=f"5511888800{i}@s.whatsapp.net",allow_send=True) for i in range(3)]
        contacts=[{"contact_id":r["contact_id"],"channel_id":list_contact_channels(r["contact_id"])[0]["channel_id"]} for r in rows]
        now=datetime.now(timezone.utc); preview=prepare_friends_envelope(campaign_id="friends",contacts=contacts,offer={"offer":"a"},playbook={"p":"a"},issuer="telegram:7",starts_at=now.isoformat(),expires_at=(now+timedelta(minutes=10)).isoformat())
        pending=persist_friends_pending(preview,profile_id="p",chat_id="42",thread_id="9",operator_id="7")
        authority=issue_authenticated_friends_authority(profile_id="p",chat_id="42",thread_id="9",operator_id="7",pending_id=pending["pending_id"],envelope_digest=pending["envelope_digest"])
        grant=activate_friends_pending(pending["pending_id"],decision="approved",authority=authority)
        plan=freeze_friends_message_plan(grant["grant_id"],contact_id=contacts[0]["contact_id"],channel_id=contacts[0]["channel_id"],blocks=["frozen one","frozen two"],action="offer",context_highwatermark="ctx",offer_digest=grant["offer_digest"])
        lease=acquire_friends_conversation_lease(plan["plan_id"])
        monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY","test-key")
        monkeypatch.setattr("urllib.request.urlopen",lambda *_args,**_kw:_Response())
        cfg={"send_enabled":True,"kill_switch":False,"quepasa":{"send_enabled":True,"send_url":"https://quepasa.invalid/send"},"friends_pilot":{"enabled":True}}
        result=json.loads(wpp_send_approved("",config=cfg,batch_plan_id=plan["plan_id"],batch_lease_fence=lease["fence"],send_client=send_via_quepasa))
        assert result["ok"] is True and result["status"]=="sent"
        no_flags=json.loads(wpp_send_approved("",config={"send_enabled":True,"kill_switch":"true","quepasa":{"send_enabled":True},"friends_pilot":{"enabled":True}},batch_plan_id=plan["plan_id"],batch_lease_fence=lease["fence"],send_client=send_via_quepasa))
        assert no_flags["ok"] is False
    finally:
        reset_hermes_home_override(token)
