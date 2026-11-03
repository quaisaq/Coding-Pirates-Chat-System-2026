import asyncio
import base64
import binascii
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import websockets

clients = {}
SERVER_VERSION = "1.3.0"
MAX_POLLS_PER_ROOM = 100
MAX_POLL_OPTIONS = 20
DATABASE_PATH = Path(__file__).with_name("chat.db")
HISTORY_LIMIT = 100
database: sqlite3.Connection | None = None
MAX_USERNAME_LENGTH = 24
MAX_ROOM_LENGTH = 48
MAX_ROOMS = 32
MAX_ICON_BYTES = 64 * 1024
LOBBY = "Lobby"


@dataclass
class Client:
    username: str
    room: str = LOBBY
    icon: str | None = None
    supports_rooms: bool = False
    supports_room_admin: bool = False
    supports_history: bool = False
    supports_polls: bool = False


@dataclass
class Room:
    admin: str | None = None
    private: bool = False
    allowed_usernames: list[str] = field(default_factory=list)
    background: str = "#ffffff"


rooms = {LOBBY: Room()}


def initialize_database(path=DATABASE_PATH):
    global database
    if database is not None:
        raise RuntimeError("Database is already open.")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS rooms (
                    name TEXT PRIMARY KEY,
                    admin TEXT,
                    private INTEGER NOT NULL,
                    allowed_usernames TEXT NOT NULL,
                    background TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    room TEXT NOT NULL REFERENCES rooms(name),
                    username TEXT NOT NULL,
                    text TEXT NOT NULL,
                    icon TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS messages_room_id ON messages(room, id)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS polls (
                    id INTEGER PRIMARY KEY,
                    room TEXT NOT NULL REFERENCES rooms(name),
                    username TEXT NOT NULL,
                    question TEXT NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS polls_room_id ON polls(room, id)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS poll_options (
                    id INTEGER PRIMARY KEY,
                    poll_id INTEGER NOT NULL REFERENCES polls(id),
                    text TEXT NOT NULL,
                    text_key TEXT NOT NULL,
                    UNIQUE(poll_id, text_key),
                    UNIQUE(poll_id, id)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS poll_votes (
                    poll_id INTEGER NOT NULL REFERENCES polls(id),
                    username_key TEXT NOT NULL,
                    username TEXT NOT NULL,
                    option_id INTEGER NOT NULL,
                    PRIMARY KEY(poll_id, username_key),
                    FOREIGN KEY(poll_id, option_id) REFERENCES poll_options(poll_id, id)
                )
            """)
            connection.execute(
                "INSERT OR IGNORE INTO rooms VALUES (?, NULL, 0, '[]', '#ffffff')", (LOBBY,),
            )
        loaded_rooms = {
            name: Room(admin, bool(private), json.loads(allowed), background)
            for name, admin, private, allowed, background in connection.execute(
                "SELECT name, admin, private, allowed_usernames, background FROM rooms"
            )
        }
    except (sqlite3.Error, ValueError):
        connection.close()
        raise
    database = connection
    rooms.clear()
    rooms.update(loaded_rooms)


def close_database():
    global database
    if database is not None:
        database.close()
        database = None


def get_database():
    if database is None:
        raise RuntimeError("Database has not been initialized.")
    return database


def save_room(name, settings):
    connection = get_database()
    with connection:
        connection.execute("""
            INSERT INTO rooms VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                admin=excluded.admin, private=excluded.private,
                allowed_usernames=excluded.allowed_usernames, background=excluded.background
        """, (name, settings.admin, settings.private, json.dumps(settings.allowed_usernames), settings.background))


def save_message(client, text):
    connection = get_database()
    with connection:
        connection.execute(
            "INSERT INTO messages (room, username, text, icon) VALUES (?, ?, ?, ?)",
            (client.room, client.username, text, client.icon),
        )


def message_history(room):
    rows = get_database().execute("""
        SELECT id, username, text, icon, created_at FROM messages
        WHERE room = ? ORDER BY id DESC LIMIT ?
    """, (room, HISTORY_LIMIT)).fetchall()
    return [
        {"id": identifier, "username": username, "text": text, "icon": icon, "created_at": created_at}
        for identifier, username, text, icon, created_at in reversed(rows)
    ]


def notify_history(websocket):
    client = clients[websocket]
    if client.supports_history:
        websockets.broadcast({websocket}, encode_event({
            "type": "history", "room": client.room, "messages": message_history(client.room),
        }))


def poll_state(poll_id):
    connection = get_database()
    identifier, room, username, question = connection.execute(
        "SELECT id, room, username, question FROM polls WHERE id = ?", (poll_id,),
    ).fetchone()
    options = [
        {"id": option_id, "text": text, "voters": []}
        for option_id, text in connection.execute(
            "SELECT id, text FROM poll_options WHERE poll_id = ? ORDER BY id", (poll_id,),
        )
    ]
    by_id = {option["id"]: option for option in options}
    for option_id, voter in connection.execute(
        "SELECT option_id, username FROM poll_votes WHERE poll_id = ? ORDER BY username_key", (poll_id,),
    ):
        by_id[option_id]["voters"].append(voter)
    return {"id": identifier, "room": room, "username": username, "question": question, "options": options}


def notify_polls(websocket):
    client = clients[websocket]
    if client.supports_polls:
        identifiers = get_database().execute(
            "SELECT id FROM polls WHERE room = ? ORDER BY id", (client.room,),
        ).fetchall()
        websockets.broadcast({websocket}, encode_event({
            "type": "polls", "room": client.room,
            "polls": [poll_state(identifier) for (identifier,) in identifiers],
        }))


def poll_text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{label} must be 1 to {maximum} characters.")
    return value.strip()


def change_poll(client, event):
    connection = get_database()
    if event["type"] == "create_poll":
        question = poll_text(event.get("question"), "Question", 240)
        options = event.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= MAX_POLL_OPTIONS:
            raise ValueError(f"Provide 2 to {MAX_POLL_OPTIONS} options.")
        options = [poll_text(option, "Option", 100) for option in options]
        if len({option.casefold() for option in options}) != len(options):
            raise ValueError("Poll options must be distinct.")
        if connection.execute("SELECT count(*) FROM polls WHERE room = ?", (client.room,)).fetchone()[0] >= MAX_POLLS_PER_ROOM:
            raise ValueError(f"A room allows at most {MAX_POLLS_PER_ROOM} polls.")
        with connection:
            cursor = connection.execute(
                "INSERT INTO polls (room, username, question) VALUES (?, ?, ?)",
                (client.room, client.username, question),
            )
            identifier = cursor.lastrowid
            connection.executemany(
                "INSERT INTO poll_options (poll_id, text, text_key) VALUES (?, ?, ?)",
                [(identifier, option, option.casefold()) for option in options],
            )
        return identifier

    identifier = event.get("poll_id")
    if type(identifier) is not int or not 1 <= identifier <= 2**63 - 1 or connection.execute(
        "SELECT id FROM polls WHERE id = ? AND room = ?", (identifier, client.room),
    ).fetchone() is None:
        raise ValueError("Choose a poll in your current room.")
    if event["type"] == "add_poll_option":
        text = poll_text(event.get("text"), "Option", 100)
        if connection.execute(
            "SELECT count(*) FROM poll_options WHERE poll_id = ?", (identifier,),
        ).fetchone()[0] >= MAX_POLL_OPTIONS:
            raise ValueError(f"A poll allows at most {MAX_POLL_OPTIONS} options.")
        if connection.execute(
            "SELECT id FROM poll_options WHERE poll_id = ? AND text_key = ?", (identifier, text.casefold()),
        ).fetchone() is not None:
            raise ValueError("That option already exists.")
        with connection:
            connection.execute(
                "INSERT INTO poll_options (poll_id, text, text_key) VALUES (?, ?, ?)",
                (identifier, text, text.casefold()),
            )
    else:
        option_id = event.get("option_id")
        if type(option_id) is not int or not 1 <= option_id <= 2**63 - 1 or connection.execute(
            "SELECT id FROM poll_options WHERE id = ? AND poll_id = ?", (option_id, identifier),
        ).fetchone() is None:
            raise ValueError("Choose an option belonging to this poll.")
        with connection:
            connection.execute("""
                INSERT INTO poll_votes VALUES (?, ?, ?, ?)
                ON CONFLICT(poll_id, username_key) DO UPDATE SET
                    username=excluded.username, option_id=excluded.option_id
            """, (identifier, client.username.casefold(), client.username, option_id))
    return identifier


def is_admin(client, settings):
    return settings.admin is not None and settings.admin.casefold() == client.username.casefold()


def room_state(client):
    settings = rooms[client.room]
    event = {
        "type": "room_state",
        "room": client.room,
        "admin": settings.admin,
        "private": settings.private,
        "background": settings.background,
        "is_admin": is_admin(client, settings),
    }
    if is_admin(client, settings):
        event["allowed_usernames"] = settings.allowed_usernames
    return event


def notify_room_state(websocket):
    client = clients[websocket]
    if client.supports_room_admin:
        websockets.broadcast({websocket}, encode_event(room_state(client)))


def may_enter(client, room):
    settings = rooms[room]
    return (
        not settings.private
        or is_admin(client, settings)
        or client.username.casefold() in {name.casefold() for name in settings.allowed_usernames}
    )


def move_client(websocket, room, reason=None):
    client = clients[websocket]
    previous_room = client.room
    client.room = room
    event = {"type": "room_joined", "room": room}
    if reason is not None:
        event["reason"] = reason
    websockets.broadcast({websocket}, encode_event(event))
    notify_room_state(websocket)
    notify_history(websocket)
    notify_polls(websocket)
    if previous_room != room:
        broadcast({"type": "user_left", "username": client.username}, exclude=websocket, room=previous_room)
        broadcast({"type": "user_joined", "username": client.username}, exclude=websocket, room=room)


def validate_room_settings(event):
    private = event.get("private")
    allowed = event.get("allowed_usernames")
    background = event.get("background")
    if not isinstance(private, bool):
        raise ValueError("Private must be a boolean.")
    if not isinstance(allowed, list) or len(allowed) > 100:
        raise ValueError("Provide at most 100 allowed usernames.")
    normalized = {}
    for name in allowed:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_USERNAME_LENGTH:
            raise ValueError(f"Allowed usernames must be 1 to {MAX_USERNAME_LENGTH} characters.")
        normalized[name.strip().casefold()] = name.strip()
    if not isinstance(background, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", background) is None:
        raise ValueError("Background must be a six-digit hex color, such as #ffffff.")
    return private, list(normalized.values()), background.lower()


def validate_icon(icon):
    if icon is None:
        return
    if not isinstance(icon, str) or len(icon) > 4 * ((MAX_ICON_BYTES + 2) // 3) + 32:
        raise ValueError("Icon must be a PNG, JPEG, GIF or WebP data URL up to 64 KiB.")
    header, separator, encoded = icon.partition(",")
    signatures = {
        "data:image/png;base64": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
        "data:image/jpeg;base64": lambda data: data.startswith(b"\xff\xd8\xff"),
        "data:image/gif;base64": lambda data: data.startswith((b"GIF87a", b"GIF89a")),
        "data:image/webp;base64": lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if not separator or header not in signatures:
        raise ValueError("Icon must be a PNG, JPEG, GIF or WebP data URL.")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Icon contains invalid base64.") from error
    if len(image) > MAX_ICON_BYTES or not signatures[header](image):
        raise ValueError("Icon is invalid or exceeds 64 KiB.")


def encode_event(event):
    return json.dumps(event, ensure_ascii=False)


def broadcast(event, exclude=None, room=None, rooms_only=False):
    message = encode_event(event)
    recipients = {
        websocket for websocket, client in clients.items()
        if websocket != exclude
        and (room is None or client.room == room)
        and (not rooms_only or client.supports_rooms)
    }
    if recipients:
        websockets.broadcast(recipients, message)


def room_list():
    names = set(rooms)
    return [
        {"name": name, "users": sum(client.room == name for client in clients.values())}
        for name in sorted(names, key=str.casefold)
    ]


def broadcast_rooms():
    broadcast({"type": "rooms", "rooms": room_list()}, rooms_only=True)


async def send_error(websocket, message):
    await websocket.send(encode_event({"type": "error", "message": message}))


async def reject_connection(websocket, message):
    await websocket.send(encode_event({"type": "error", "message": message}))
    await websocket.close(code=1008, reason=message)


async def handler(websocket):
    username = None
    try:
        async for raw_message in websocket:
            try:
                event = json.loads(raw_message)
            except json.JSONDecodeError:
                await websocket.send(encode_event({
                    "type": "error",
                    "message": "Messages must be valid JSON.",
                }))
                continue

            if not isinstance(event, dict):
                await websocket.send(encode_event({
                    "type": "error",
                    "message": "Each event must be a JSON object.",
                }))
                continue

            if username is None:
                requested_username = event.get("username")
                if event.get("type") != "join" or not isinstance(requested_username, str):
                    await reject_connection(websocket, "Join the chat with a username first.")
                    return

                requested_username = requested_username.strip()
                if not requested_username or len(requested_username) > MAX_USERNAME_LENGTH:
                    await reject_connection(
                        websocket,
                        f"Username must be 1 to {MAX_USERNAME_LENGTH} characters.",
                    )
                    return

                if any(
                    existing.username.casefold() == requested_username.casefold()
                    for existing in clients.values()
                ):
                    await reject_connection(websocket, "That username is already in use.")
                    return

                try:
                    validate_icon(event.get("icon"))
                except ValueError as error:
                    await reject_connection(websocket, str(error))
                    return

                username = requested_username
                features = event.get("features", [])
                client = Client(
                    username=username,
                    icon=event.get("icon"),
                    supports_rooms=isinstance(features, list) and "rooms" in features,
                    supports_room_admin=isinstance(features, list) and "rooms" in features and "room_admin" in features,
                    supports_history=isinstance(features, list) and "history" in features,
                    supports_polls=isinstance(features, list) and "polls" in features,
                )
                clients[websocket] = client
                response = {
                    "type": "joined",
                    "username": username,
                }
                if client.supports_rooms:
                    response.update({"room": client.room, "features": ["rooms", "icons"]})
                    if client.supports_room_admin:
                        response["features"].append("room_admin")
                if isinstance(features, list) and "versions" in features:
                    response["server_version"] = SERVER_VERSION
                if client.supports_history:
                    response.setdefault("features", []).append("history")
                if client.supports_polls:
                    response.setdefault("features", []).append("polls")
                await websocket.send(encode_event(response))
                notify_room_state(websocket)
                notify_history(websocket)
                notify_polls(websocket)
                broadcast(
                    {"type": "user_joined", "username": username},
                    exclude=websocket, room=client.room,
                )
                broadcast_rooms()
                continue

            client = clients[websocket]
            if event.get("type") in ("create_poll", "add_poll_option", "vote_poll"):
                if not client.supports_polls:
                    await send_error(websocket, "Declare the polls feature when joining first.")
                    continue
                try:
                    identifier = change_poll(client, event)
                except ValueError as error:
                    await send_error(websocket, str(error))
                    continue
                recipients = {
                    member_socket for member_socket, member in clients.items()
                    if member.room == client.room and member.supports_polls
                }
                websockets.broadcast(recipients, encode_event({
                    "type": "poll_updated", "room": client.room, "poll": poll_state(identifier),
                    "action": event["type"], "actor": username,
                }))
                continue
            if event.get("type") == "join_room":
                if not client.supports_rooms:
                    await send_error(websocket, "Declare the rooms feature when joining first.")
                    continue
                room = event.get("room")
                if not isinstance(room, str) or not room.strip() or len(room.strip()) > MAX_ROOM_LENGTH:
                    await send_error(websocket, f"Room name must be 1 to {MAX_ROOM_LENGTH} characters.")
                    continue
                room = room.strip()
                names = {entry["name"] for entry in room_list()}
                room = next((name for name in names if name.casefold() == room.casefold()), room)
                if room not in names and len(names) >= MAX_ROOMS:
                    await send_error(websocket, f"The server allows at most {MAX_ROOMS} rooms.")
                    continue
                if room in rooms and not may_enter(client, room):
                    await send_error(websocket, "You are not allowed to enter this private room.")
                    continue
                if room not in rooms:
                    settings = Room(admin=username)
                    save_room(room, settings)
                    rooms[room] = settings
                previous_room = client.room
                move_client(websocket, room)
                if previous_room != room:
                    broadcast_rooms()
                continue

            if event.get("type") == "update_room":
                settings = rooms[client.room]
                if (
                    not client.supports_room_admin
                    or not is_admin(client, settings)
                    or event.get("room") != client.room
                ):
                    await send_error(websocket, "Only the room admin can change its settings.")
                    continue
                try:
                    private, allowed, background = validate_room_settings(event)
                except ValueError as error:
                    await send_error(websocket, str(error))
                    continue
                updated_settings = Room(settings.admin, private, allowed, background)
                save_room(client.room, updated_settings)
                rooms[client.room] = updated_settings
                for member_socket, member in list(clients.items()):
                    if member.room == client.room:
                        if not may_enter(member, client.room):
                            move_client(member_socket, LOBBY, "Your access to the private room was removed.")
                        else:
                            notify_room_state(member_socket)
                broadcast_rooms()
                continue

            if event.get("type") != "message":
                await websocket.send(encode_event({
                    "type": "error",
                    "message": "Unsupported event type.",
                }))
                continue

            text = event.get("text")
            if not isinstance(text, str):
                await websocket.send(encode_event({
                    "type": "error",
                    "message": "Message text must be a string.",
                }))
                continue

            save_message(client, text)
            broadcast({
                "type": "message",
                "username": username,
                "text": text,
                "room": client.room,
                "icon": client.icon,
            }, room=client.room)
    except sqlite3.Error:
        logging.exception("Chat database operation failed")
        await send_error(websocket, "Database operation failed. Reconnect and retry; the operation was not confirmed.")
        await websocket.close(code=1011, reason="Database operation failed")
    finally:
        disconnected_client = clients.pop(websocket, None)
        if disconnected_client is not None:
            broadcast(
                {"type": "user_left", "username": disconnected_client.username},
                room=disconnected_client.room,
            )
            broadcast_rooms()


async def main():
    initialize_database()
    try:
        async with websockets.serve(handler, "0.0.0.0", 12345, max_size=None):
            print(f"Chat server v{SERVER_VERSION} running on ws://0.0.0.0:12345")
            print(f"Database: {DATABASE_PATH}")
            await asyncio.Future()
    finally:
        close_database()

if __name__ == "__main__":
    asyncio.run(main())
