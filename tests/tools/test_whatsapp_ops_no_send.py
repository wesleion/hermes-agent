"""Regression checks: read-only WhatsApp Ops tools must not touch send paths."""

from __future__ import annotations

import dis
import inspect
import sys

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


FORBIDDEN_SEND_NAMES = {
    "send_via_quepasa",
    "create_group_via_quepasa",
    "_direct_deliver",
    "handle_message",
    "direct_deliver",
    "create_draft",
    "create_approval",
    "reserve_outbox_send",
    "mark_outbox_result",
    "mark_outbox_blocked",
    "update_draft_status",
}


def _read_only_functions():
    from tools import whatsapp_ops_tool as tool

    return {
        "wpp_status": tool.wpp_status,
        "wpp_resolve_contact": tool.wpp_resolve_contact,
        "wpp_list_contacts": tool.wpp_list_contacts,
        "wpp_inbound_lookup": tool.wpp_inbound_lookup,
        "wpp_thread_context": tool.wpp_thread_context,
        "wpp_conversation_summary": tool.wpp_conversation_summary,
        "wpp_cockpit_overview": tool.wpp_cockpit_overview,
    }


def test_read_only_whatsapp_tools_source_has_no_send_references():
    for name, fn in _read_only_functions().items():
        source = inspect.getsource(fn)
        for forbidden in FORBIDDEN_SEND_NAMES:
            assert forbidden not in source, f"{name} source references {forbidden}"


def test_read_only_whatsapp_tools_bytecode_has_no_send_references():
    for name, fn in _read_only_functions().items():
        bytecode_names = set(fn.__code__.co_names) | set(fn.__code__.co_freevars)
        loaded_names = {instr.argval for instr in dis.get_instructions(fn) if isinstance(instr.argval, str)}
        names = bytecode_names | loaded_names
        for forbidden in FORBIDDEN_SEND_NAMES:
            assert forbidden not in names, f"{name} bytecode references {forbidden}"


def test_read_only_context_tools_do_not_import_gateway_or_quepasa_modules(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_conversation_summary, wpp_thread_context

    forbidden_modules = {
        "gateway.platforms.whatsapp_common",
        "tools.whatsapp_ops_quepasa",
    }
    for module_name in forbidden_modules:
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        thread_result = wpp_thread_context(limit=1)
        summary_result = wpp_conversation_summary(limit=1, mode="stats")
    finally:
        reset_hermes_home_override(token)

    assert '"ok": true' in thread_result
    assert '"ok": true' in summary_result
    for module_name in forbidden_modules:
        assert module_name not in sys.modules


def test_wpp_send_approved_positive_control_references_send_path():
    from tools.whatsapp_ops_tool import wpp_send_approved

    source = inspect.getsource(wpp_send_approved)
    bytecode_names = set(wpp_send_approved.__code__.co_names)
    assert "send_via_quepasa" in source or "send_via_quepasa" in bytecode_names
    assert "create_group_via_quepasa" in source or "create_group_via_quepasa" in bytecode_names
