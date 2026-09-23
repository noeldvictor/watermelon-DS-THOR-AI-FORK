# thor_mcp

An MCP server that drives Watermelon Thor on an attached device over adb, so an agent (Claude
Code, or any MCP client) can check settings, launch games, take screenshots of both displays and
read the renderer's stats without anyone poking the handheld.

It's registered in the repo's `.mcp.json`; Claude Code offers to enable it when it starts in the
repo. It needs `adb` on PATH, a debug (`.dev`) build of the app, and Python 3 (Pillow optional,
for smaller screenshots). The device is the attached AYN Thor unless `THOR_SERIAL` names another.

| Tool | Does |
| --- | --- |
| `status` | What's in front, whether a game runs, FPS, latest HD pack / compositor stats |
| `settings_get`, `settings_set` | Read settings (optional key filter); set one, applied live |
| `list_roms`, `launch`, `close` | The ROM library; launch by name; force-stop the emulator |
| `screenshot` | Top and/or bottom display, returned as images |
| `stats` | Sample HD pack hits/lookups, compositor stats and FPS for N seconds |
| `logcat` | Tail the log with a filter |
| `pack` | List HD packs on the device, or switch one on/off (applies at next launch) |
| `tap`, `key` | Touch a display or send a key event |
| `save_state`, `load_state` | State slots, for repeatable A/B comparisons |

Settings, the ROM list, launching and FPS go through the app's debug command receiver
(`app/src/debug/.../DebugCommandReceiver.kt`: GET_PREFERENCES, SET_PREFERENCE, LIST_ROMS,
GET_FPS, LAUNCH_ROM, ...), which answers in the broadcast's result data. Screenshots, logs and
pack folders go through adb directly: the app can't capture the second display itself.

The Thor may be shared with other sessions: `status` says what's in front, and nothing should
take the device while another app is in use there.
