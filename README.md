# Chat system

Run `python server\chatserver.py` with the `websockets` package installed, then open
`client\chat.html` and enter the server address and port (default: 12345).

## Versions

The current client release is **1.3.2** (image URLs without file extensions); the server remains
**1.2.0** because the protocol and server behavior are unchanged. The first numbered release
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
The uploaded icon is saved with each message so historical messages can display it.

Everyone starts in **Lobby**. After connecting, enter a room name and select
**Join room** to join or create it. The suggestions show existing rooms and their
user counts. Each user belongs to one room; messages and join/leave notifications
are only sent within that room. Switching rooms clears the client's displayed
messages and loads the last 100 saved messages for the new room.
Empty rooms remain saved, even when their admin is offline.
Room names are case-insensitive, trimmed, and limited to 48 characters. The
server allows up to 32 rooms including Lobby.

## Room administration

The user who creates a room is its admin. New rooms are open. The admin can
enable **Private room**, enter allowed usernames (one per line, up to 100), and
choose a background color, then select **Save room settings**. Names are trimmed
and compared case-insensitively; the admin always retains access. Switching to
private mode or removing a name immediately moves unauthorized members to Lobby.
Turning off private mode lets everyone join again.

Admin settings are collapsed by default to leave more space for chat. Select
**Room administration** to expand or collapse them (also accessible with the
keyboard). Collapsing preserves unsaved input. Switching rooms or reconnecting
collapses the panel again; saving settings leaves it open.

The background is shared with all administration-aware clients in the room,
including later arrivals; text contrast adjusts for light/dark backgrounds.
Lobby has no admin, is always open, and uses the default background.
Rooms, admin names, access lists and backgrounds are saved in the database.
Starting with 1.2.0, an admin switching rooms or disconnecting no longer closes
their room. They regain admin rights when reconnecting with the same username
(case-insensitive). Members can stay in the room while the admin is offline.

**Usernames are not authenticated.** This is name-based access control, not
secure identity verification: someone could take an allowed name while its user
is offline, including the saved admin name. Do not use private rooms to protect
sensitive information.

## Database and refresh

The server automatically creates `server\chat.db` using Python's built-in SQLite;
no database service or extra package is required. The path is relative to the
server script, not the working directory. Rooms and every accepted message
(username, text, icon and UTC timestamp) are committed before confirmation or
broadcast. Database errors are logged and reported to the client, not silently
treated as successful sends.

All messages are retained, but updated clients receive only the latest **100**
per room, oldest first. History is loaded after connection and each successful
room join, including forced moves to Lobby. Private-room access is checked before
loading its history. Denied joins do not expose that room's messages.

The browser remembers the username, address, port and last confirmed room in
`sessionStorage` for the current tab. Refresh automatically reconnects, rejoins
that room if still allowed, and restores history. If rejoining is denied, the
client stays in Lobby with an error. If the server is unavailable, use **Connect**
to retry. The selected icon file is not remembered across refresh; select it again
if needed. Previous messages retain their saved icons. If browser storage is
unavailable, enter connection details again; server history is still saved.

The database survives server restarts. Back it up while the server is stopped
and protect it as chat data. It is excluded from Git. Messages sent before this
feature was installed cannot be recovered.

## Sending images by URL

Paste a direct HTTP or HTTPS image URL into a message, optionally with other text.
The updated client tries each distinct HTTP/HTTPS URL as an image underneath the
original message. No file extension or `format` parameter is required, so image
endpoints such as `/images?id=123` work if they return a browser-supported image.
Query strings and fragments are preserved. Redirects are handled by the browser.
Ordinary webpage links cannot be displayed as images.

Images fit the message width and are at most 240 pixels tall. Failed loads show
a "No image preview available" notice under the message, leaving the original text
intact. The browser cannot distinguish a non-image response from a blocked,
expired or otherwise unavailable image. Hosts requiring login or blocking image
embedding may still fail; this feature does not bypass those restrictions.
Only URL text is
stored in the database; previews are also rendered from history after refresh.
The external image must remain available. This feature does not upload image files.

Preview attempts contact every HTTP/HTTPS URL in a message, not just known image
hosts. Images load directly from the host in the recipient's browser without a
referrer header; that host still sees the recipient's IP and may receive its own
cookies. Only send image URLs you trust. Non-HTTP(S) URLs and URLs containing
credentials are not previewed. SVG is loaded as an image, never embedded as HTML.
No new protocol fields or events are needed: older clients show the URL as text,
and the updated client can preview image URLs received from older clients/servers.

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

### History extension (1.2.0)

Clients request `history` in `join.features`. The server acknowledges it in
`joined.features` and sends this event only to those clients:

```json
{"type":"history","room":"Lobby","messages":[{"id":1,"username":"Alice","text":"Hello","icon":null,"created_at":"2026-11-03T17:00:00.000Z"}]}
```

The array contains at most 100 messages from the current room in insertion order.
It is a snapshot, not additional live messages. Live `message` events retain their
existing shape. Messages from old clients are saved too, but old clients do not
receive the new event. The updated client reports when an older server cannot
restore history. Administration-aware `room_state` also includes the boolean
`is_admin`, so reconnecting with different username capitalization works correctly.

Run protocol tests with `python -m unittest discover -s server -p "test_*.py"`.
