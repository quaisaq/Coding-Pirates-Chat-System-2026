import asyncio
import json

import websockets

clients = {}
MAX_USERNAME_LENGTH = 24


def encode_event(event):
    return json.dumps(event, ensure_ascii=False)


def broadcast(event, exclude=None):
    message = encode_event(event)
    recipients = set(clients) - ({exclude} if exclude is not None else set())
    if recipients:
        websockets.broadcast(recipients, message)


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
                    existing.casefold() == requested_username.casefold()
                    for existing in clients.values()
                ):
                    await reject_connection(websocket, "That username is already in use.")
                    return

                username = requested_username
                clients[websocket] = username
                await websocket.send(encode_event({
                    "type": "joined",
                    "username": username,
                }))
                broadcast({"type": "user_joined", "username": username}, exclude=websocket)
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
            })
    finally:
        disconnected_username = clients.pop(websocket, None)
        if disconnected_username is not None:
            broadcast({"type": "user_left", "username": disconnected_username})


async def main():
    async with websockets.serve(handler, "0.0.0.0", 12345, max_size=None):
        print("Chat server running on ws://0.0.0.0:12345")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
