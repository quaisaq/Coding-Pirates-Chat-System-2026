import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

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
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.database_path = Path(directory) / "chat.db"
        chatserver.initialize_database(self.database_path)
        self.addCleanup(chatserver.close_database)
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

    async def connect_admin_client(self, username):
        connection, joined = await self.connect(
            username, features=["rooms", "icons", "versions", "room_admin"],
        )
        self.assertIn("room_admin", joined["features"])
        await self.until(connection, "room_state")
        await self.until(connection, "rooms")
        return connection

    async def enter_room(self, connection, room):
        await self.send(connection, {"type": "join_room", "room": room})
        await self.until(connection, "room_joined")
        state = await self.until(connection, "room_state")
        await self.until(connection, "rooms")
        return state

    async def update_room(self, connection, room, **extra):
        await self.send(connection, {
            "type": "update_room", "room": room, "private": False,
            "allowed_usernames": [], "background": "#ffffff", **extra,
        })

    async def test_admin_access_list_revocation_and_background(self):
        admin = await self.connect_admin_client("Owner")
        state = await self.enter_room(admin, "Managed")
        self.assertEqual(state["admin"], "Owner")
        self.assertFalse(state["private"])
        self.assertEqual(state["allowed_usernames"], [])
        guest = await self.connect_admin_client("Guest")
        state = await self.enter_room(guest, "Managed")
        self.assertNotIn("allowed_usernames", state)
        outsider = await self.connect_admin_client("Outsider")

        await self.update_room(
            admin, "Managed", private=True,
            allowed_usernames=[" guest ", "GUEST", "Future"], background="#123ABC",
        )
        state = await self.until(admin, "room_state")
        self.assertEqual(state["background"], "#123abc")
        self.assertEqual(state["allowed_usernames"], ["GUEST", "Future"])
        state = await self.until(guest, "room_state")
        self.assertTrue(state["private"])
        self.assertEqual(state["background"], "#123abc")
        self.assertNotIn("allowed_usernames", state)
        await self.until(guest, "rooms")
        await self.until(outsider, "rooms")
        await self.send(outsider, {"type": "join_room", "room": "managed"})
        self.assertEqual((await self.receive(outsider))["type"], "error")
        await self.send(outsider, {"type": "message", "text": "Denied remains in Lobby"})
        self.assertEqual((await self.receive(outsider))["room"], "Lobby")

        future = await self.connect_admin_client("future")
        state = await self.enter_room(future, "Managed")
        self.assertEqual(state["background"], "#123abc")
        self.assertTrue(state["private"])
        self.assertNotIn("allowed_usernames", state)
        await future.close()
        self.assertIn("Managed", chatserver.rooms)

        await self.update_room(admin, "Managed", private=True, allowed_usernames=[])
        await self.until(admin, "room_state")
        event = await self.until(guest, "room_joined")
        self.assertEqual(event["room"], "Lobby")
        self.assertIn("removed", event["reason"])
        self.assertIsNone((await self.until(guest, "room_state"))["admin"])
        await self.until(guest, "rooms")
        await self.send(guest, {"type": "join_room", "room": "Managed"})
        self.assertEqual((await self.receive(guest))["type"], "error")
        await self.update_room(admin, "Managed", private=False, background="#000000")
        await self.until(admin, "room_state")
        await self.until(guest, "rooms")
        state = await self.enter_room(guest, "Managed")
        self.assertEqual(state["background"], "#000000")
        self.assertFalse(state["private"])

    async def test_room_settings_authorization_and_atomic_validation(self):
        admin = await self.connect_admin_client("Owner")
        await self.enter_room(admin, "Managed")
        guest = await self.connect_admin_client("Guest")
        await self.enter_room(guest, "Managed")
        await self.update_room(guest, "Managed", background="#000000", admin="Guest")
        self.assertEqual((await self.receive(guest))["type"], "error")
        await self.update_room(admin, "Lobby")
        self.assertEqual((await self.until(admin, "error"))["type"], "error")
        invalid = [
            {"private": "true"}, {"allowed_usernames": "Guest"},
            {"allowed_usernames": [""]}, {"allowed_usernames": [42]},
            {"allowed_usernames": ["x" * 25]}, {"allowed_usernames": ["Guest"] * 101},
            {"background": "url(https://example.com)"}, {"background": "#fff"},
        ]
        for change in invalid:
            await self.update_room(admin, "Managed", **change)
            self.assertEqual((await self.receive(admin))["type"], "error")
            settings = chatserver.rooms["Managed"]
            self.assertEqual(settings.admin, "Owner")
            self.assertFalse(settings.private)
            self.assertEqual(settings.background, "#ffffff")
            self.assertEqual(settings.allowed_usernames, [])
        await self.update_room(admin, "Managed", allowed_usernames=[f"Name{i}" for i in range(100)])
        self.assertEqual(len((await self.until(admin, "room_state"))["allowed_usernames"]), 100)

    async def test_admin_departure_preserves_room_and_members(self):
        for mode in ("switch", "disconnect"):
            room_name = f"Managed{mode}"
            admin = await self.connect_admin_client(f"Owner{mode}")
            await self.enter_room(admin, room_name)
            guest = await self.connect_admin_client(f"Guest{mode}")
            await self.enter_room(guest, room_name)
            old_client, _ = await self.connect(f"Old{mode}", modern=True)
            await self.until(old_client, "rooms")
            await self.send(old_client, {"type": "join_room", "room": room_name})
            await self.until(old_client, "room_joined")
            await self.until(old_client, "rooms")
            await self.until(guest, "user_joined")
            await self.until(guest, "rooms")
            if mode == "switch":
                await self.send(admin, {"type": "join_room", "room": "Lobby"})
                await self.until(admin, "room_joined")
            else:
                await admin.close()
            await self.until(guest, "user_left")
            await self.until(guest, "rooms")
            await self.until(old_client, "user_left")
            await self.until(old_client, "rooms")
            self.assertIn(room_name, chatserver.rooms)
            self.assertEqual(chatserver.rooms[room_name].admin, f"Owner{mode}")
            await self.send(old_client, {"type": "message", "text": "Relocated"})
            self.assertEqual((await self.receive(old_client))["room"], room_name)
            self.assertEqual((await self.until(guest, "message"))["room"], room_name)
            await guest.close()
            await old_client.close()
            await admin.close()

    async def test_persistent_history_limit_order_and_room_isolation(self):
        connection, response = await self.connect("Writer", features=["history"], icon=ICON)
        self.assertIn("history", response["features"])
        self.assertEqual((await self.receive(connection))["messages"], [])
        for index in range(105):
            await self.send(connection, {"type": "message", "text": f"Message {index}"})
            await self.receive(connection)
        await connection.close()
        self.assertEqual(chatserver.get_database().execute("SELECT count(*) FROM messages").fetchone()[0], 105)
        chatserver.close_database()
        chatserver.initialize_database(self.database_path)
        reader, _ = await self.connect("Reader", features=["rooms", "history"])
        history = await self.receive(reader)
        self.assertEqual(history["room"], "Lobby")
        self.assertEqual(len(history["messages"]), 100)
        self.assertEqual([entry["text"] for entry in history["messages"]], [
            f"Message {index}" for index in range(5, 105)
        ])
        self.assertEqual(history["messages"][0]["icon"], ICON)
        self.assertTrue(history["messages"][0]["created_at"].endswith("Z"))
        await self.until(reader, "rooms")
        await self.send(reader, {"type": "join_room", "room": "Other"})
        await self.until(reader, "room_joined")
        self.assertEqual((await self.receive(reader))["messages"], [])
        await self.until(reader, "rooms")
        await self.send(reader, {"type": "message", "text": "Other room"})
        await self.receive(reader)
        await self.send(reader, {"type": "join_room", "room": "Other"})
        await self.until(reader, "room_joined")
        self.assertEqual([entry["text"] for entry in (await self.receive(reader))["messages"]], ["Other room"])

    async def test_room_settings_and_private_history_survive_restart(self):
        admin = await self.connect_admin_client("Owner")
        await self.enter_room(admin, "Saved")
        await self.update_room(admin, "Saved", private=True, allowed_usernames=["Friend"], background="#112233")
        await self.until(admin, "room_state")
        await self.until(admin, "rooms")
        await self.send(admin, {"type": "message", "text": "Private history"})
        await self.receive(admin)
        await admin.close()
        chatserver.close_database()
        chatserver.initialize_database(self.database_path)
        self.assertEqual(chatserver.rooms["Saved"], chatserver.Room("Owner", True, ["Friend"], "#112233"))
        restored = await self.connect_admin_client("owner")
        state = await self.enter_room(restored, "saved")
        self.assertTrue(state["is_admin"])
        await self.update_room(restored, "Saved", private=True, allowed_usernames=["Friend"], background="#334455")
        await self.until(restored, "room_state")
        stranger, _ = await self.connect("Stranger", features=["rooms", "history"])
        await self.until(stranger, "history")
        await self.until(stranger, "rooms")
        await self.send(stranger, {"type": "join_room", "room": "Saved"})
        self.assertEqual((await self.receive(stranger))["type"], "error")
        await self.assert_quiet(stranger)
        friend, _ = await self.connect("friend", features=["rooms", "history"])
        await self.until(friend, "history")
        await self.until(friend, "rooms")
        await self.send(friend, {"type": "join_room", "room": "Saved"})
        await self.until(friend, "room_joined")
        self.assertEqual((await self.receive(friend))["messages"][0]["text"], "Private history")

    async def test_failed_database_write_is_not_broadcast_or_acknowledged(self):
        writer, _ = await self.connect("Writer")
        reader, _ = await self.connect("Reader")
        await self.receive(writer)
        chatserver.get_database().execute("PRAGMA query_only = ON")
        with self.assertLogs(level="ERROR") as logs:
            await self.send(writer, {"type": "message", "text": "Cannot save"})
            response = await self.receive(writer)
            self.assertEqual(response["type"], "error")
            self.assertIn("Database operation failed", response["message"])
            await writer.wait_closed()
        self.assertIn("Chat database operation failed", logs.output[0])
        self.assertEqual(writer.close_code, 1011)
        self.assertEqual((await self.receive(reader))["type"], "user_left")
        await self.assert_quiet(reader)
        self.assertEqual(chatserver.get_database().execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        chatserver.get_database().execute("PRAGMA query_only = OFF")

    async def test_failed_room_writes_do_not_change_settings_or_membership(self):
        for create in (True, False):
            admin = await self.connect_admin_client(f"Owner{create}")
            room = f"Room{create}"
            if not create:
                await self.enter_room(admin, room)
            chatserver.get_database().execute("PRAGMA query_only = ON")
            with self.assertLogs(level="ERROR"):
                if create:
                    await self.send(admin, {"type": "join_room", "room": room})
                else:
                    await self.update_room(admin, room, private=True, background="#000000")
                self.assertEqual((await self.receive(admin))["type"], "error")
                await admin.wait_closed()
            if create:
                self.assertNotIn(room, chatserver.rooms)
            else:
                self.assertEqual(chatserver.rooms[room], chatserver.Room(admin=f"Owner{create}"))
            chatserver.get_database().execute("PRAGMA query_only = OFF")

    async def test_lobby_and_old_admin_client_settings_are_protected(self):
        lobby_user = await self.connect_admin_client("LobbyUser")
        await self.update_room(lobby_user, "Lobby")
        self.assertEqual((await self.receive(lobby_user))["type"], "error")
        old, joined = await self.connect("OldAdmin", modern=True)
        self.assertNotIn("room_admin", joined["features"])
        await self.until(old, "rooms")
        await self.send(old, {"type": "join_room", "room": "OldRoom"})
        await self.until(old, "room_joined")
        await self.until(old, "rooms")
        self.assertEqual(chatserver.rooms["OldRoom"].admin, "OldAdmin")
        await self.update_room(old, "OldRoom", private=True)
        self.assertEqual((await self.receive(old))["type"], "error")
        await self.assert_quiet(old)

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
        self.assertEqual(rooms["rooms"], [{"name": "Lobby", "users": 2}, {"name": "Pirates", "users": 0}])
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
