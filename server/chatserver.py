import asyncio
import base64
import binascii
import json
from dataclasses import dataclass

import websockets

clients = {}
SERVER_VERSION = "1.0.0"
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
    names = {LOBBY} | {client.room for client in clients.values()}
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
                )
                clients[websocket] = client
                response = {
                    "type": "joined",
                    "username": username,
                }
                if client.supports_rooms:
                    response.update({"room": client.room, "features": ["rooms", "icons"]})
                if isinstance(features, list) and "versions" in features:
                    response["server_version"] = SERVER_VERSION
                await websocket.send(encode_event(response))
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
                previous_room = client.room
                client.room = room
                await websocket.send(encode_event({"type": "room_joined", "room": room}))
                if previous_room != room:
                    broadcast(
                        {"type": "user_left", "username": username},
                        exclude=websocket, room=previous_room,
                    )
                    broadcast(
                        {"type": "user_joined", "username": username},
                        exclude=websocket, room=room,
                    )
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
