"""Funktionstests fuer die Agent-Mailbox MCP-Tools.

Ruft die zugrunde liegenden Python-Funktionen direkt auf (FastMCP-Tools bleiben
in dieser SDK-Version normale, aufrufbare Funktionen).
"""

import importlib
import time

import pytest

import kiro_agent_mailbox.server as srv


@pytest.fixture(autouse=True)
def isolated_mailbox(tmp_path, monkeypatch):
    """Jeder Test bekommt ein frisches, temporaeres AGENT_MAILBOX_DIR."""
    monkeypatch.setenv("AGENT_MAILBOX_DIR", str(tmp_path / "mailbox"))
    importlib.reload(srv)
    yield srv


def as_session(monkeypatch, session_id):
    monkeypatch.setenv("KIRO_SESSION_ID", session_id)


def test_register_success(isolated_mailbox, monkeypatch):
    as_session(monkeypatch, "session-A")
    r = isolated_mailbox.register(name="devops")
    assert r["ok"] is True
    assert r["name"] == "devops"


def test_register_name_collision_with_active_session(isolated_mailbox, monkeypatch):
    as_session(monkeypatch, "session-A")
    isolated_mailbox.register(name="devops")

    as_session(monkeypatch, "session-B")
    r = isolated_mailbox.register(name="devops")
    assert r["ok"] is False
    assert "bereits" in r["error"]


def test_list_agents_shows_registered_sessions(isolated_mailbox, monkeypatch):
    as_session(monkeypatch, "session-A")
    isolated_mailbox.register(name="devops")
    as_session(monkeypatch, "session-B")
    isolated_mailbox.register(name="backend")

    r = isolated_mailbox.list_agents()
    names = sorted(a["name"] for a in r["agents"])
    assert names == ["backend", "devops"]


def test_send_message_to_unknown_name_fails(isolated_mailbox, monkeypatch):
    as_session(monkeypatch, "session-A")
    isolated_mailbox.register(name="devops")

    r = isolated_mailbox.send_message(to="nonexistent", text="hallo")
    assert r["ok"] is False


def test_send_and_receive_message(isolated_mailbox, monkeypatch):
    as_session(monkeypatch, "session-A")
    isolated_mailbox.register(name="devops")
    as_session(monkeypatch, "session-B")
    isolated_mailbox.register(name="backend")

    as_session(monkeypatch, "session-A")
    r = isolated_mailbox.send_message(to="backend", text="Bitte Endpoint anpassen")
    assert r["ok"] is True
    msg_id = r["message_id"]

    as_session(monkeypatch, "session-B")
    inbox = isolated_mailbox.check_inbox()
    assert inbox["registered"] is True
    assert inbox["count"] == 1
    assert inbox["messages"][0]["text"] == "Bitte Endpoint anpassen"
    assert inbox["messages"][0]["from"] == "devops"
    assert inbox["messages"][0]["id"] == msg_id


def test_reply_closes_message_and_notifies_sender(isolated_mailbox, monkeypatch):
    as_session(monkeypatch, "session-A")
    isolated_mailbox.register(name="devops")
    as_session(monkeypatch, "session-B")
    isolated_mailbox.register(name="backend")

    as_session(monkeypatch, "session-A")
    sent = isolated_mailbox.send_message(to="backend", text="Aufgabe X")
    msg_id = sent["message_id"]

    as_session(monkeypatch, "session-B")
    r = isolated_mailbox.reply(message_id=msg_id, status="erledigt", text="Fertig, commit abc123")
    assert r["ok"] is True

    # Inbox von backend sollte die Nachricht nun als closed markiert haben
    inbox_after = isolated_mailbox.check_inbox()
    assert inbox_after["count"] == 0

    as_session(monkeypatch, "session-A")
    replies = isolated_mailbox.check_replies()
    assert replies["count"] == 1
    assert replies["replies"][0]["reply_status"] == "erledigt"
    assert "abc123" in replies["replies"][0]["text"]

    # Zweiter Aufruf: Antwort wurde als gelesen markiert, keine erneute Zustellung
    replies_again = isolated_mailbox.check_replies()
    assert replies_again["count"] == 0


def test_reply_to_unknown_message_id_fails(isolated_mailbox, monkeypatch):
    as_session(monkeypatch, "session-A")
    isolated_mailbox.register(name="devops")

    r = isolated_mailbox.reply(message_id="does-not-exist", status="erledigt", text="x")
    assert r["ok"] is False


def test_heartbeat_expiry_filters_inactive_sessions(isolated_mailbox, monkeypatch):
    as_session(monkeypatch, "session-A")
    isolated_mailbox.register(name="devops")
    as_session(monkeypatch, "session-B")
    isolated_mailbox.register(name="backend")

    sessions = isolated_mailbox._read_sessions()
    sessions["backend"]["last_heartbeat"] = time.time() - (31 * 60)
    isolated_mailbox._write_sessions(sessions)

    active = isolated_mailbox.list_agents()
    names_active = sorted(a["name"] for a in active["agents"])
    assert names_active == ["devops"]

    with_inactive = isolated_mailbox.list_agents(include_inactive=True)
    backend_entry = next(a for a in with_inactive["agents"] if a["name"] == "backend")
    assert backend_entry["status"] == "inactive"


def test_name_can_be_reclaimed_after_expiry(isolated_mailbox, monkeypatch):
    as_session(monkeypatch, "session-A")
    isolated_mailbox.register(name="devops")

    sessions = isolated_mailbox._read_sessions()
    sessions["devops"]["last_heartbeat"] = time.time() - (31 * 60)
    isolated_mailbox._write_sessions(sessions)

    as_session(monkeypatch, "session-B")
    r = isolated_mailbox.register(name="devops")
    assert r["ok"] is True


def test_check_inbox_unregistered_session(isolated_mailbox, monkeypatch):
    as_session(monkeypatch, "session-unregistered")
    r = isolated_mailbox.check_inbox()
    assert r["registered"] is False
    assert r["messages"] == []
