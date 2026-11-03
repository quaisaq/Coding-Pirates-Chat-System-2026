# Chat system

Run `python server\chatserver.py` with the `websockets` package installed, then open
`client\chat.html` and enter the server address and port (default: 12345).

Choose an optional user icon before connecting. The client accepts PNG, JPEG,
GIF and WebP files up to 2 MiB and uploads a PNG thumbnail at most 64 by 64 pixels.
Icons are kept in memory for the connection, not saved to disk.

Everyone starts in **Lobby**. After connecting, enter a room name and select
**Join room** to join or create it. The suggestions show existing rooms and their
user counts. Each user belongs to one room; messages and join/leave notifications
are only sent within that room. Switching rooms clears the client's displayed
messages. There is no message history. Empty rooms disappear except for Lobby.
Room names are case-insensitive, trimmed, and limited to 48 characters. The
server allows up to 32 rooms including Lobby.

## WebSocket protocol and compatibility

Existing clients can still send `{"type":"join","username":"Alice"}` followed by
`{"type":"message","text":"Hello"}`. They receive the same `joined`, `message`,
`user_joined`, `user_left` and `error` event types, and remain in Lobby.
Existing fields retain their meaning; clients should ignore extra fields.

New clients advertise capabilities in the initial join:

```json
{"type":"join","username":"Alice","features":["rooms","icons"],"icon":"data:image/png;base64,..."}
```

`icon` is optional or null. The server accepts base64 PNG, JPEG, GIF and WebP
data URLs up to 64 KiB of decoded data, with matching image signatures. URLs and
SVG are not accepted. Icons are included with messages as `icon` (or null).

Only clients advertising `rooms` receive the new room events:

- `joined` includes `room: "Lobby"` and `features: ["rooms", "icons"]`.
- `rooms` includes `rooms: [{"name":"Lobby","users":1}]`, updated on joins,
  room switches and disconnects.
- Send `{"type":"join_room","room":"Games"}` after `joined`.
- `room_joined` includes the canonical `room` name and confirms a switch.
  An invalid request returns `error` without changing membership.

Messages still use `{"type":"message","text":"Hello"}` and go to the sender's
current room. Received messages include the original `username` and `text`, plus
`room` and `icon`. A message cannot override its sender, room or icon.

The updated browser client also works with the original server: if `joined`
does not advertise capabilities, room controls are hidden and icon support is
reported as unavailable.

Run protocol tests with `python -m unittest discover -s server -p "test_*.py"`.
