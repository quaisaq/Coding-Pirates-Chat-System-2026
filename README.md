# Chat system

Run `python server\chatserver.py` with the `websockets` package installed, then open
`client\chat.html` and enter the server address and port (default: 12345).

## Versions

The current release is **1.1.0** (room administration); the first numbered release
was **1.0.0**. The client version is always visible
below the connection/chat interface, including when disconnected. It describes
the loaded client, not the server it connects to. After connection, the server
version is shown separately; older servers are explicitly shown as unknown.

Use `MAJOR.MINOR.PATCH`: major for incompatible changes, minor for new compatible
features, and patch for fixes. Update `CLIENT_VERSION` in `client\chat.html` and
`SERVER_VERSION` in `server\chatserver.py` when releasing changes to each component.
Reload the client or restart the server to load a new version.

Version-aware clients add `"versions"` to `features` and send the optional
`client_version` string in `join`. The server adds `server_version` to `joined`
only when `"versions"` was requested. Versions are informational; no client is
rejected based on its version, and existing clients need no changes.

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

## Room administration

The user who creates a room is its admin. New rooms are open. The admin can
enable **Private room**, enter allowed usernames (one per line, up to 100), and
choose a background color, then select **Save room settings**. Names are trimmed
and compared case-insensitively; the admin always retains access. Switching to
private mode or removing a name immediately moves unauthorized members to Lobby.
Turning off private mode lets everyone join again.

The background is shared with all administration-aware clients in the room,
including later arrivals; text contrast adjusts for light/dark backgrounds.
Lobby has no admin, is always open, and uses the default background.
When the admin switches rooms or disconnects, their room is deleted and remaining
members are moved to Lobby with an explanation. Settings are in memory only.

**Usernames are not authenticated.** This is name-based access control, not
secure identity verification: someone could take an allowed name while its user
is offline. Do not use private rooms to protect sensitive information.

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

### Administration extension (1.1.0)

Clients advertise both `rooms` and `room_admin` in `join.features`. The server
adds `room_admin` to `joined.features` only for clients requesting it. These
clients receive `room_state` after joining and whenever settings change:

```json
{"type":"room_state","room":"Games","admin":"Alice","private":true,"background":"#123abc"}
```

Only the admin also receives `allowed_usernames`. To update the current room,
an administration-aware admin sends all settings together:

```json
{"type":"update_room","room":"Games","private":true,"allowed_usernames":["Bob"],"background":"#123abc"}
```

The server checks admin rights, the current room, boolean privacy, up to 100
usernames of 1–24 characters, and a six-digit hex color. Invalid updates return
`error` without modifying settings; a successful update is confirmed by
`room_state`. Denied room joins likewise leave membership unchanged.

Forced moves use the existing `room_joined` event with `room: "Lobby"` and an
optional `reason`. Old room-aware clients still relocate correctly, but do not
receive `room_state`, cannot edit settings, and cannot display custom backgrounds.
Their room creator still owns the room; update the client to edit its settings.
Pre-room clients remain in Lobby. The updated client hides administration controls
when connecting to older servers.

Run protocol tests with `python -m unittest discover -s server -p "test_*.py"`.
