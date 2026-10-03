#!/usr/bin/env python3
"""
Agent-Mailbox MCP-Server

Cross-Session-Messaging zwischen gleichzeitig laufenden Kiro-CLI-Terminal-Sessions
(oder anderen MCP-Clients) auf derselben Maschine. Jede Session kann sich unter
einem selbstgewaehlten Namen registrieren, Nachrichten an andere registrierte
Sessions senden, die eigene Inbox pruefen und auf empfangene Nachrichten antworten.
Storage ist dateibasiert (JSON/JSONL) unter AGENT_MAILBOX_DIR.

Kein Daemon, kein Netzwerk-Port: Jeder Tool-Call startet/nutzt den MCP-Server-Prozess
pro Session (stdio), der Zustand liegt ausschliesslich auf der Platte und wird per
File-Locking synchronisiert. Dadurch funktioniert das Messaging auch zwischen
voellig unabhaengigen Prozessen, solange sie auf dasselbe AGENT_MAILBOX_DIR zeigen.
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

try:
    import fcntl  # unix file locking

    HAVE_FCNTL = True
except ImportError:
    HAVE_FCNTL = False


# ---------------------------------------------------------------------------
# Storage-Konfiguration
# ---------------------------------------------------------------------------

DEFAULT_MAILBOX_DIR = Path.home() / ".agent-mailbox"
SESSIONS_FILE = "sessions.json"
HEARTBEAT_TIMEOUT_SECONDS = 30 * 60  # 30 Minuten ohne check_inbox -> gilt als inaktiv
SESSION_ID_ENV_VARS = ("KIRO_SESSION_ID", "AGENT_MAILBOX_SESSION_ID")


def _mailbox_dir() -> Path:
    base = os.environ.get("AGENT_MAILBOX_DIR")
    p = Path(base).expanduser() if base else DEFAULT_MAILBOX_DIR
    p.mkdir(parents=True, exist_ok=True)
    (p / "inbox").mkdir(parents=True, exist_ok=True)
    return p


class LockedFile:
    """Kontextmanager fuer eine exklusiv gesperrte Datei (advisory lock via flock).

    Auf Nicht-Unix-Systemen (kein fcntl) wird ohne Lock gearbeitet -- Agent-Mailbox
    ist primaer fuer macOS/Linux gedacht, wo mehrere Terminal-Sessions parallel
    auf dieselben Dateien zugreifen.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        if HAVE_FCNTL:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        self._fh.seek(0)
        return self._fh

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fh:
            if HAVE_FCNTL:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()


def _read_sessions() -> dict[str, Any]:
    path = _mailbox_dir() / SESSIONS_FILE
    if not path.exists():
        return {}
    with LockedFile(path) as fh:
        content = fh.read().strip()
        if not content:
            return {}
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}


def _write_sessions(data: dict[str, Any]) -> None:
    path = _mailbox_dir() / SESSIONS_FILE
    with LockedFile(path) as fh:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(data, indent=2, ensure_ascii=False))
        fh.flush()


def _current_session_id() -> str:
    for var in SESSION_ID_ENV_VARS:
        sid = os.environ.get(var)
        if sid:
            return sid
    # Fallback falls keine Session-ID-Env-Var gesetzt ist (z.B. manueller Testlauf)
    return f"unknown-{uuid.uuid4().hex[:8]}"


def _inbox_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return _mailbox_dir() / "inbox" / f"{safe}.jsonl"


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    with LockedFile(path) as fh:
        fh.seek(0, os.SEEK_END)
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        fh.flush()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with LockedFile(path) as fh:
        fh.seek(0)
        lines = fh.read().splitlines()
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _rewrite_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    with LockedFile(path) as fh:
        fh.seek(0)
        fh.truncate()
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        fh.flush()


def _name_for_session(session_id: str) -> str | None:
    sessions = _read_sessions()
    for name, info in sessions.items():
        if info.get("session_id") == session_id:
            return name
    return None


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("agent-mailbox")


@mcp.tool()
def register(name: str) -> dict[str, Any]:
    """Registriert die aktuelle Session unter einem frei waehlbaren Namen im
    Agent-Mailbox-System, damit andere Sessions ihr Nachrichten schicken koennen.

    Args:
        name: Gewuenschter Anzeigename der Session (z.B. "devops", "backend-dev").
              Muss aktuell nicht bereits von einer anderen AKTIVEN Session belegt sein.
    """
    session_id = _current_session_id()
    cwd = os.getcwd()
    now = time.time()

    sessions = _read_sessions()

    # Pruefen ob der Name von einer anderen, noch aktiven Session belegt ist
    existing = sessions.get(name)
    if existing and existing.get("session_id") != session_id:
        age = now - existing.get("last_heartbeat", 0)
        if age < HEARTBEAT_TIMEOUT_SECONDS:
            return {
                "ok": False,
                "error": f"Name '{name}' ist bereits von einer aktiven Session "
                f"belegt (cwd: {existing.get('cwd')}, zuletzt aktiv vor "
                f"{int(age)}s). Waehle einen anderen Namen.",
            }

    # Falls diese Session unter einem anderen Namen bereits registriert war: entfernen
    for existing_name in list(sessions.keys()):
        if sessions[existing_name].get("session_id") == session_id and existing_name != name:
            del sessions[existing_name]

    sessions[name] = {
        "session_id": session_id,
        "cwd": cwd,
        "registered_at": sessions.get(name, {}).get("registered_at", now)
        if existing and existing.get("session_id") == session_id
        else now,
        "last_heartbeat": now,
    }
    _write_sessions(sessions)
    _inbox_path(name)  # legt die Inbox-Datei an falls noch nicht vorhanden

    return {
        "ok": True,
        "name": name,
        "cwd": cwd,
        "message": f"Session als '{name}' registriert (cwd: {cwd}).",
    }


@mcp.tool()
def list_agents(include_inactive: bool = False) -> dict[str, Any]:
    """Listet alle registrierten Agent-Sessions mit Namen, Arbeitsverzeichnis
    und Aktivitaetsstatus auf. Vor dem Senden einer Nachricht aufrufen, um zu
    sehen, welche Namen aktuell verfuegbar sind.

    Args:
        include_inactive: Wenn True, werden auch Sessions angezeigt, deren
                           Heartbeat laenger als 30 Minuten zurueckliegt
                           (als "inactive" markiert statt ausgefiltert).
    """
    sessions = _read_sessions()
    now = time.time()
    my_session_id = _current_session_id()

    agents = []
    for name, info in sessions.items():
        age = now - info.get("last_heartbeat", 0)
        active = age < HEARTBEAT_TIMEOUT_SECONDS
        if not active and not include_inactive:
            continue
        agents.append(
            {
                "name": name,
                "cwd": info.get("cwd"),
                "status": "active" if active else "inactive",
                "last_heartbeat_seconds_ago": int(age),
                "is_self": info.get("session_id") == my_session_id,
            }
        )

    agents.sort(key=lambda a: (a["status"] != "active", a["name"]))
    return {"ok": True, "agents": agents, "count": len(agents)}


@mcp.tool()
def send_message(to: str, text: str) -> dict[str, Any]:
    """Sendet eine Nachricht an eine andere registrierte Agent-Session. Die
    Zielsession sieht die Nachricht beim naechsten Aufruf von check_inbox
    (typischerweise automatisch zu Beginn ihres naechsten Turns).

    Args:
        to: Name der Zielsession, wie in list_agents angezeigt.
        text: Inhalt der Nachricht (Aufgabe, Frage, Information).
    """
    sessions = _read_sessions()
    if to not in sessions:
        return {
            "ok": False,
            "error": f"Keine Session namens '{to}' registriert. "
            f"Nutze list_agents um verfuegbare Namen zu sehen.",
        }

    from_name = _name_for_session(_current_session_id()) or f"session-{_current_session_id()[:8]}"
    message_id = uuid.uuid4().hex[:12]
    now = time.time()

    message = {
        "id": message_id,
        "from": from_name,
        "to": to,
        "text": text,
        "timestamp": now,
        "status": "open",
    }
    _append_jsonl(_inbox_path(to), message)

    return {
        "ok": True,
        "message_id": message_id,
        "to": to,
        "message": f"Nachricht an '{to}' gesendet (id: {message_id}).",
    }


@mcp.tool()
def check_inbox() -> dict[str, Any]:
    """Prueft die eigene Inbox auf neue, noch offene Nachrichten und
    aktualisiert gleichzeitig den eigenen Heartbeat (haelt die Registrierung
    aktiv). Sollte am Anfang jedes Turns aufgerufen werden.
    """
    session_id = _current_session_id()
    name = _name_for_session(session_id)

    if not name:
        return {
            "ok": True,
            "registered": False,
            "messages": [],
            "note": "Diese Session ist nicht im Agent-Mailbox-System registriert.",
        }

    # Heartbeat aktualisieren
    sessions = _read_sessions()
    if name in sessions:
        sessions[name]["last_heartbeat"] = time.time()
        _write_sessions(sessions)

    entries = _read_jsonl(_inbox_path(name))
    open_messages = [m for m in entries if m.get("status") == "open" and m.get("to") == name]

    return {
        "ok": True,
        "registered": True,
        "name": name,
        "messages": open_messages,
        "count": len(open_messages),
    }


@mcp.tool()
def reply(message_id: str, status: str, text: str) -> dict[str, Any]:
    """Beantwortet eine erhaltene Nachricht und markiert sie als bearbeitet.
    Die Antwort wird dem urspruenglichen Absender in dessen Inbox zugestellt.

    Args:
        message_id: Die id der Nachricht aus check_inbox, die beantwortet wird.
        status: Kurzer Status, z.B. "erledigt", "issue_angelegt", "abgelehnt".
        text: Freitext der Antwort (z.B. was gemacht wurde oder Issue-Link).
    """
    session_id = _current_session_id()
    name = _name_for_session(session_id)
    if not name:
        return {"ok": False, "error": "Diese Session ist nicht registriert."}

    own_inbox = _inbox_path(name)
    entries = _read_jsonl(own_inbox)

    target_entry = None
    for e in entries:
        if e.get("id") == message_id:
            target_entry = e
            break

    if target_entry is None:
        return {"ok": False, "error": f"Nachricht mit id '{message_id}' nicht gefunden."}

    # Original als closed markieren
    for e in entries:
        if e.get("id") == message_id:
            e["status"] = "closed"
    _rewrite_jsonl(own_inbox, entries)

    # Antwort in die Inbox des urspruenglichen Absenders schreiben
    from_name = target_entry.get("from")
    reply_obj = {
        "id": uuid.uuid4().hex[:12],
        "type": "reply",
        "in_reply_to": message_id,
        "from": name,
        "to": from_name,
        "reply_status": status,
        "text": text,
        "timestamp": time.time(),
        "status": "open",
    }
    _append_jsonl(_inbox_path(from_name), reply_obj)

    return {
        "ok": True,
        "message": f"Antwort auf '{message_id}' an '{from_name}' gesendet.",
    }


@mcp.tool()
def check_replies() -> dict[str, Any]:
    """Prueft, ob auf eigene gesendete Nachrichten geantwortet wurde. Antworten
    liegen als Eintraege mit type="reply" in der eigenen Inbox und werden
    hierueber separat von normalen Nachrichten ausgewertet.
    """
    session_id = _current_session_id()
    name = _name_for_session(session_id)
    if not name:
        return {"ok": True, "registered": False, "replies": []}

    entries = _read_jsonl(_inbox_path(name))
    open_replies = [
        e for e in entries if e.get("type") == "reply" and e.get("status") == "open"
    ]

    # Abgerufene Antworten als gelesen markieren
    if open_replies:
        for e in entries:
            if e.get("type") == "reply" and e.get("status") == "open":
                e["status"] = "closed"
        _rewrite_jsonl(_inbox_path(name), entries)

    return {"ok": True, "registered": True, "replies": open_replies, "count": len(open_replies)}


def main() -> None:
    """Entrypoint fuer `agent-mailbox-mcp` (siehe pyproject.toml project.scripts)."""
    mcp.run()


if __name__ == "__main__":
    main()
