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
}


def _read_only_functions():
    from tools import whatsapp_ops_tool as tool

    return {
        "wpp_status": tool.wpp_status,
        "wpp_resolve_contact": tool.wpp_resolve_contact,
        "wpp_list_contacts": tool.wpp_list_contacts,
        "wpp_inbound_lookup": tool.wpp_inbound_lookup,
        "wpp_thread_context": tool.wpp_thread_context,
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


def test_wpp_thread_context_does_not_import_gateway_send_modules(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_thread_context

    monkeypatch.delitem(sys.modules, "gateway.platforms.whatsapp_common", raising=False)

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        result = wpp_thread_context(limit=1)
    finally:
        reset_hermes_home_override(token)

    assert '"ok": true' in result
    assert "gateway.platforms.whatsapp_common" not in sys.modules


def test_wpp_send_approved_positive_control_references_send_path():
    from tools.whatsapp_ops_tool import wpp_send_approved

    source = inspect.getsource(wpp_send_approved)
    bytecode_names = set(wpp_send_approved.__code__.co_names)
    assert "send_via_quepasa" in source or "send_via_quepasa" in bytecode_names
    assert "create_group_via_quepasa" in source or "create_group_via_quepasa" in bytecode_names
