import asyncio
import base64
import json
import unittest

import websockets

import chatserver


ICON = "data:image/png;base64," + base64.b64encode(
    # A one-pixel PNG.
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
    )
).decode("ascii")


class ChatProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server = await self.enterAsyncContext(
            websockets.serve(chatserver.handler, "127.0.0.1", 0, max_size=None)
        )
        self.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"

    async def connect(self, username, modern=False, **extra):
        connection = await self.enterAsyncContext(websockets.connect(self.url, max_queue=None))
        event = {"type": "join", "username": username, **extra}
        if modern:
            event["features"] = ["rooms", "icons"]
        await self.send(connection, event)
        return connection, await self.receive(connection)

    async def send(self, connection, event):
        await connection.send(json.dumps(event))

    async def receive(self, connection):
        return json.loads(await asyncio.wait_for(connection.recv(), timeout=2))

    async def until(self, connection, event_type):
        for _ in range(20):
            event = await self.receive(connection)
            if event["type"] == event_type:
                return event
        self.fail(f"Did not receive {event_type}")

    async def assert_quiet(self, connection):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(connection.recv(), timeout=0.05)

    async def test_version_handshake_is_optional(self):
        connection, response = await self.connect(
            "Versioned", features=["rooms", "icons", "versions"], client_version="1.0.0",
        )
        self.assertEqual(response["server_version"], chatserver.SERVER_VERSION)
        self.assertEqual(response["room"], "Lobby")
        await self.until(connection, "rooms")
        await self.send(connection, {"type": "message", "text": "Versioned chat"})
        self.assertEqual((await self.receive(connection))["text"], "Versioned chat")
        _, response = await self.connect("Unversioned", modern=True)
        self.assertNotIn("server_version", response)

    async def test_legacy_protocol_and_room_isolation(self):
        legacy, joined = await self.connect("Old")
        self.assertEqual(joined, {"type": "joined", "username": "Old"})
        modern, joined = await self.connect("New", modern=True, icon=ICON)
        self.assertEqual(joined["room"], "Lobby")
        self.assertEqual(joined["features"], ["rooms", "icons"])
        self.assertEqual(await self.receive(legacy), {"type": "user_joined", "username": "New"})
        self.assertEqual((await self.receive(modern))["type"], "rooms")
        await self.send(modern, {"type": "message", "text": "Hello"})
        message = await self.receive(legacy)
        self.assertEqual(message, {
            "type": "message", "username": "New", "text": "Hello", "room": "Lobby", "icon": ICON,
        })
        self.assertEqual(await self.receive(modern), message)

        await self.send(modern, {"type": "join_room", "room": " Pirates "})
        self.assertEqual(await self.receive(modern), {"type": "room_joined", "room": "Pirates"})
        self.assertEqual((await self.receive(modern))["rooms"], [
            {"name": "Lobby", "users": 1}, {"name": "Pirates", "users": 1},
        ])
        self.assertEqual(await self.receive(legacy), {"type": "user_left", "username": "New"})
        await self.send(modern, {"type": "message", "text": "Private room"})
        self.assertEqual((await self.receive(modern))["room"], "Pirates")
        await self.assert_quiet(legacy)
        await self.send(legacy, {"type": "message", "text": "Still in lobby"})
        self.assertEqual((await self.receive(legacy))["text"], "Still in lobby")
        await self.assert_quiet(modern)

        await self.send(modern, {"type": "join_room", "room": "lobby"})
        self.assertEqual((await self.receive(modern))["room"], "Lobby")
        self.assertEqual((await self.receive(legacy))["type"], "user_joined")
        rooms = await self.until(modern, "rooms")
        self.assertEqual(rooms["rooms"], [{"name": "Lobby", "users": 2}])
        await modern.close()
        self.assertEqual(await self.receive(legacy), {"type": "user_left", "username": "New"})
        await self.assert_quiet(legacy)

    async def test_room_presence_and_same_room(self):
        first, _ = await self.connect("First", modern=True)
        await self.until(first, "rooms")
        await self.send(first, {"type": "join_room", "room": "Games"})
        await self.until(first, "room_joined")
        await self.until(first, "rooms")
        second, _ = await self.connect("Second", modern=True)
        await self.until(second, "rooms")
        await self.until(first, "rooms")
        await self.send(second, {"type": "join_room", "room": "games"})
        self.assertEqual((await self.until(second, "room_joined"))["room"], "Games")
        await self.until(second, "rooms")
        self.assertEqual(await self.receive(first), {"type": "user_joined", "username": "Second"})
        await self.until(first, "rooms")
        await self.send(second, {"type": "join_room", "room": "Games"})
        self.assertEqual((await self.receive(second))["type"], "room_joined")
        await self.assert_quiet(first)
        await self.send(second, {"type": "message", "text": "Together"})
        self.assertEqual((await self.receive(first))["text"], "Together")
        self.assertEqual((await self.receive(second))["text"], "Together")

    async def test_invalid_rooms_preserve_membership(self):
        connection, _ = await self.connect("New", modern=True)
        await self.until(connection, "rooms")
        for room in (None, 42, "", " ", "x" * 49):
            await self.send(connection, {"type": "join_room", "room": room})
            self.assertEqual((await self.receive(connection))["type"], "error")
        original_limit = chatserver.MAX_ROOMS
        chatserver.MAX_ROOMS = 1
        try:
            await self.send(connection, {"type": "join_room", "room": "Full"})
            self.assertEqual((await self.receive(connection))["type"], "error")
        finally:
            chatserver.MAX_ROOMS = original_limit
        await self.send(connection, {"type": "message", "text": "Lobby"})
        self.assertEqual((await self.receive(connection))["room"], "Lobby")

    async def test_join_validation_and_legacy_feature_gate(self):
        connection, _ = await self.connect("Taken")
        duplicate, response = await self.connect("taken", modern=True)
        self.assertEqual(response["type"], "error")
        await duplicate.wait_closed()
        self.assertEqual(duplicate.close_code, 1008)
        await self.send(connection, {"type": "join_room", "room": "Other"})
        self.assertEqual((await self.receive(connection))["type"], "error")
        await self.send(connection, {"type": "message", "text": 42})
        self.assertEqual((await self.receive(connection))["type"], "error")
        await connection.send("{")
        self.assertEqual((await self.receive(connection))["type"], "error")
        await self.send(connection, [])
        self.assertEqual((await self.receive(connection))["type"], "error")
        await self.send(connection, {"type": "message", "text": "Works"})
        self.assertEqual((await self.receive(connection))["text"], "Works")
        unjoined = await self.enterAsyncContext(websockets.connect(self.url))
        await self.send(unjoined, {"type": "join_room", "room": "Other"})
        self.assertEqual((await self.receive(unjoined))["type"], "error")
        await unjoined.wait_closed()
        self.assertEqual(unjoined.close_code, 1008)

    async def test_invalid_icons_are_rejected(self):
        for icon in (
            42, "https://example.com/avatar.png", "data:image/svg+xml;base64,PHN2Zz4=",
            "data:image/png;base64,@@@", "data:image/png;base64,aGVsbG8=",
            "data:image/png;base64," + base64.b64encode(
                b"\x89PNG\r\n\x1a\n" + b"x" * chatserver.MAX_ICON_BYTES
            ).decode("ascii"),
        ):
            connection, response = await self.connect("Icon", modern=True, icon=icon)
            self.assertEqual(response["type"], "error")
            await connection.wait_closed()
            self.assertEqual(connection.close_code, 1008)
        connection, response = await self.connect("Icon", modern=True, icon=ICON)
        self.assertEqual(response["type"], "joined")
        await self.until(connection, "rooms")
        await self.send(connection, {"type": "message", "text": "Icon"})
        self.assertEqual((await self.receive(connection))["icon"], ICON)

    async def test_exact_icon_size_limit(self):
        image = base64.b64decode(ICON.partition(",")[2])
        image += b"\0" * (chatserver.MAX_ICON_BYTES - len(image))
        boundary_icon = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        chatserver.validate_icon(boundary_icon)
        oversized = "data:image/png;base64," + base64.b64encode(image + b"\0").decode("ascii")
        with self.assertRaises(ValueError):
            chatserver.validate_icon(oversized)

    async def test_exact_room_count_limit(self):
        for index in range(31):
            connection, _ = await self.connect(f"User{index}", modern=True)
            await self.until(connection, "rooms")
            await self.send(connection, {"type": "join_room", "room": f"Room{index}"})
            await self.until(connection, "room_joined")
            await self.until(connection, "rooms")
        requester, _ = await self.connect("Requester", modern=True)
        rooms = await self.until(requester, "rooms")
        self.assertEqual(len(rooms["rooms"]), 32)
        await self.send(requester, {"type": "join_room", "room": "Room32"})
        self.assertEqual((await self.receive(requester))["type"], "error")
        await self.send(requester, {"type": "join_room", "room": "Room0"})
        self.assertEqual((await self.receive(requester))["room"], "Room0")


if __name__ == "__main__":
    unittest.main()
