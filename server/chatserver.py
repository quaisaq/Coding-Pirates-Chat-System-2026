import asyncio
import base64
import binascii
import json
import re
from dataclasses import dataclass, field

import websockets

clients = {}
SERVER_VERSION = "1.1.0"
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


@dataclass
class Room:
    admin: str | None = None
    private: bool = False
    allowed_usernames: list[str] = field(default_factory=list)
    background: str = "#ffffff"


rooms = {LOBBY: Room()}


def room_state(client):
    settings = rooms[client.room]
    event = {
        "type": "room_state",
        "room": client.room,
        "admin": settings.admin,
        "private": settings.private,
        "background": settings.background,
    }
    if settings.admin == client.username:
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
        or settings.admin == client.username
        or client.username.casefold() in {name.casefold() for name in settings.allowed_usernames}
    )


def close_owned_room(room, username):
    if room == LOBBY or rooms[room].admin != username:
        return
    del rooms[room]
    for websocket, member in list(clients.items()):
        if member.room == room:
            move_client(websocket, LOBBY, "Room closed because its admin left.")


def move_client(websocket, room, reason=None):
    client = clients[websocket]
    previous_room = client.room
    # Move first so closing an owned room only relocates the remaining members.
    client.room = room
    event = {"type": "room_joined", "room": room}
    if reason is not None:
        event["reason"] = reason
    websockets.broadcast({websocket}, encode_event(event))
    notify_room_state(websocket)
    if previous_room != room:
        broadcast({"type": "user_left", "username": client.username}, exclude=websocket, room=previous_room)
        broadcast({"type": "user_joined", "username": client.username}, exclude=websocket, room=room)
        previous_settings = rooms.get(previous_room)
        if previous_settings is not None and previous_settings.admin == client.username:
            close_owned_room(previous_room, client.username)


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
                await websocket.send(encode_event(response))
                notify_room_state(websocket)
                broadcast(
                    {"type": "user_joined", "username": username},
                    exclude=websocket, room=client.room,
                )
                broadcast_rooms()
                continue

            client = clients[websocket]
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
                    rooms[room] = Room(admin=username)
                previous_room = client.room
                move_client(websocket, room)
                if previous_room != room:
                    broadcast_rooms()
                continue

            if event.get("type") == "update_room":
                settings = rooms[client.room]
                if (
                    not client.supports_room_admin
                    or settings.admin != username
                    or event.get("room") != client.room
                ):
                    await send_error(websocket, "Only the room admin can change its settings.")
                    continue
                try:
                    private, allowed, background = validate_room_settings(event)
                except ValueError as error:
                    await send_error(websocket, str(error))
                    continue
                settings.private = private
                settings.allowed_usernames = allowed
                settings.background = background
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

            broadcast({
                "type": "message",
                "username": username,
                "text": text,
                "room": client.room,
                "icon": client.icon,
            }, room=client.room)
    finally:
        disconnected_client = clients.pop(websocket, None)
        if disconnected_client is not None:
            close_owned_room(disconnected_client.room, disconnected_client.username)
            broadcast(
                {"type": "user_left", "username": disconnected_client.username},
                room=disconnected_client.room,
            )
            broadcast_rooms()


async def main():
    async with websockets.serve(handler, "0.0.0.0", 12345, max_size=None):
        print(f"Chat server v{SERVER_VERSION} running on ws://0.0.0.0:12345")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
