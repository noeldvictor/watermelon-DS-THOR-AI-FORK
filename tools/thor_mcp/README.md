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

## On-device server (`watermelon-thor`)

Debug builds also carry an MCP server inside the app itself, registered next to `thor` in
`.mcp.json` as `watermelon-thor` (HTTP, `http://127.0.0.1:27184/mcp`). It reads state straight
from the app instead of through `adb shell`: which ROM runs, the debug pause, the view model's
state, and the app's own log. Screenshots, taps, keys, force-stop and pack folders stay here in
`thor`, since they need adb (the app can't capture the second display).

It's off by default and binds 127.0.0.1 only (port 27184; ARMSX2 uses 27183 on the same Thor).
Start it one of three ways, then forward the port:

```sh
# Any time, also with a game in front (doesn't disturb it). This process only;
# add --ez persist true to also turn the settings toggle on.
adb -s <serial> shell am broadcast -f 32 -p me.magnum.melondualds.dev \
    -a me.magnum.melondualds.dev.START_DEV_SERVER
# Cold start from the ROM list
adb -s <serial> shell am start -n me.magnum.melondualds.dev/me.magnum.melonds.ui.romlist.RomListActivity \
    --ez devserver true
# Or: Settings -> General -> "Dev server (MCP)" (debug builds only; persists)

adb -s <serial> forward tcp:27184 tcp:27184
```

`STOP_DEV_SERVER` stops it and turns the toggle off. Both broadcasts answer with the server
state as result data. The server lives as long as the app process: `thor`'s `close`
(force-stop) takes it down, and the toggle brings it back on the next app start.

| Tool | Arguments | Does |
| --- | --- | --- |
| `status` | - | Activities in front, emulator state, running ROM, FPS, debug pause, latest stats lines |
| `settings_get` | `filter`? | Settings as JSON, optionally only keys containing `filter` |
| `settings_set` | `key`, `value`, `type`? | Set one setting, applied live (same code as SET_PREFERENCE) |
| `list_roms` | `query`? | ROM library: name, file, uri |
| `launch` | `query` or `uri`, `first`?, `wait`? (true), `timeout_ms`? (30000), `pause_after`? | Launch and wait until the ROM runs. Refuses while another ROM runs |
| `save_state`, `load_state` | `slot`? (0-8, default 1) or `path`, `pause_after`? | State slots of the running ROM |
| `pause`, `resume` | - | Hold emulation paused / let it run |
| `stats` | `seconds`? (0-30), `misses`? (20) | Latest `HDTexPack[Stats]`, `VulkanOutput[Stats]`, `VulkanPerf[Pacing]`, `CoreJit[State]`, `HDTexPack: indexed`; recent `HDTexPack[Miss]` keys; FPS. `seconds` > 0 samples a window |
| `log` | `lines`? (200), `filter`?, `level`? | Tail of the app's own logcat |

Transport is MCP Streamable HTTP in its simplest form (`POST /mcp`, JSON responses, no SSE);
every tool is also `POST /tool/<name>` with the arguments as the JSON body, for curl (on Windows
PowerShell, call `curl.exe`, not the `curl` alias):

```sh
curl -s -X POST http://127.0.0.1:27184/tool/status
curl -s -X POST http://127.0.0.1:27184/tool/settings_set -d '{"key":"video_hd_texture_filter","value":"0"}'
curl -s -X POST http://127.0.0.1:27184/tool/stats -d '{"seconds":5}'
curl -s -X POST http://127.0.0.1:27184/mcp -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Launching has the same Android limit as the broadcast: the app may only open the emulator
while it's in front, so bring the ROM list up first (the `am start` above) if the Thor shows
something else. Requests carrying a non-loopback `Host` or any browser `Origin` are refused;
any app on the device could still reach the port while the server runs, which is why it's off
by default and absent from release builds.

Code: `app/src/debug/java/me/magnum/melonds/debug/` - `DevServer.kt` (HTTP + JSON-RPC),
`DevTools.kt` (the tools), `DebugCommands.kt` (settings/ROMs/launch/state code shared with
`DebugCommandReceiver`), `DevServerInitializer.kt` (toggle + `devserver` extra).
