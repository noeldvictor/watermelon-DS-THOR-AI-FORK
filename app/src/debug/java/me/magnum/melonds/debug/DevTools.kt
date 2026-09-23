package me.magnum.melonds.debug

import android.content.Context
import android.net.Uri
import android.os.Process
import kotlinx.coroutines.runBlocking
import me.magnum.melonds.MelonEmulator
import me.magnum.melonds.ui.emulator.model.EmulatorState
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * The tools behind [DevServer]. Each takes a JSON object and returns one; an `error` key means
 * failure. Settings, the library, launching and save states run the same [DebugCommands] the
 * adb broadcasts do, under the same lock. Screenshots and input are deliberately absent: the
 * app can't capture the Thor's second display, and the PC-side thor_mcp server does both
 * through adb.
 */
internal class DevTools(private val context: Context) {

    private class Tool(val name: String, val description: String, val schema: JSONObject, val run: (JSONObject) -> JSONObject)

    private val tools: List<Tool> = listOf(
        Tool("status", "App and emulator state: activities in front (the Thor is shared - check before taking it), " +
            "whether a game runs and which ROM, FPS, the debug pause, and the latest HD pack / compositor stats lines.",
            schema(), ::status),
        Tool("settings_get", "Read the app's settings as JSON. `filter`: only keys containing this, e.g. 'filter' or 'video'.",
            schema("filter" to prop("string", "key substring"))) { a ->
            DebugCommands.preferences(entryPoint(), a.optString("filter"))
        },
        Tool("settings_set", "Set one setting; applied live to a running game, like SET_PREFERENCE. The stored type " +
            "decides parsing (e.g. video_hd_texture_filter is a string '0'-'13'). `type` is only needed for a key " +
            "that doesn't exist yet.",
            schema(
                "key" to prop("string", "preference key"),
                "value" to JSONObject().put("type", JSONArray(listOf("string", "number", "boolean")))
                    .put("description", "new value; a set is comma-separated"),
                "type" to prop("string", "boolean|int|long|float|set|string"),
                required = listOf("key", "value"),
            ), ::settingsSet),
        Tool("list_roms", "The ROM library (name, file, uri). Optional `query` substring on name or file.",
            schema("query" to prop("string", "name or file substring"))) { a ->
            val roms = runBlocking { DebugCommands.roms(entryPoint(), a.optString("query").trim()) }
            JSONObject().put("count", roms.size).put("roms", JSONArray(roms.map { DebugCommands.romJson(it) }))
        },
        Tool("launch", "Launch a ROM by `query` (name/file substring; an exact name wins) or `uri`, and by default wait " +
            "until it runs. Android only lets the app open the emulator while it is in front: if the ROM list isn't, " +
            "`adb shell am start -n <package>/me.magnum.melonds.ui.romlist.RomListActivity` first.",
            schema(
                "query" to prop("string", "name or file substring"),
                "uri" to prop("string", "ROM uri from list_roms"),
                "first" to prop("boolean", "take the first of several matches"),
                "wait" to prop("boolean", "wait for the ROM to run (default true)"),
                "timeout_ms" to prop("integer", "how long to wait (default 30000, max 90000)"),
                "pause_after" to prop("boolean", "hold the emulator paused once the ROM runs"),
            ), ::launch),
        Tool("save_state", "Save the running game to state `slot` (0-8, default 1), or to `path`.",
            schema(
                "slot" to prop("integer", "0-8"),
                "path" to prop("string", "absolute file path or URI instead of a slot"),
                "pause_after" to prop("boolean", "stay paused afterwards"),
            )) { a -> stateCommand(a, save = true) },
        Tool("load_state", "Load state `slot` (0-8, default 1) or `path` into the running game. Waits up to 8s for a " +
            "ROM that is still starting.",
            schema(
                "slot" to prop("integer", "0-8"),
                "path" to prop("string", "absolute file path or URI instead of a slot"),
                "pause_after" to prop("boolean", "stay paused afterwards"),
            )) { a -> stateCommand(a, save = false) },
        Tool("pause", "Hold emulation paused (the debug pause; the app's own pause menu or an app switch can resume it).",
            schema()) { setPaused(true) },
        Tool("resume", "Resume emulation.", schema()) { setPaused(false) },
        Tool("stats", "HD pack and renderer stats from this process's log: the latest HDTexPack[Stats], " +
            "VulkanOutput[Stats], VulkanPerf[Pacing], CoreJit[State] and 'HDTexPack: indexed' lines, the recent " +
            "HDTexPack[Miss] keys, and FPS. `seconds` > 0 samples that long and also returns every stats line in the window.",
            schema(
                "seconds" to prop("number", "sample window, 0-30 (default 0: latest lines only)"),
                "misses" to prop("integer", "how many recent miss keys to return (default 20)"),
            ), ::stats),
        Tool("log", "Tail this process's logcat (the app reads its own log). `lines` (default 200), optional " +
            "`filter` substring (case-insensitive), `level` V/D/I/W/E minimum.",
            schema(
                "lines" to prop("integer", "lines to return, 1-5000"),
                "filter" to prop("string", "substring"),
                "level" to prop("string", "V|D|I|W|E"),
            ), ::log),
    )

    fun has(name: String): Boolean = tools.any { it.name == name }

    fun list(): List<JSONObject> = tools.map {
        JSONObject().put("name", it.name).put("description", it.description).put("inputSchema", it.schema)
    }

    fun call(name: String, args: JSONObject): JSONObject {
        val tool = tools.firstOrNull { it.name == name }
            ?: return JSONObject().put("error", "unknown tool: $name").put("tools", JSONArray(tools.map { it.name }))
        return try {
            tool.run(args)
        } catch (e: Throwable) {
            JSONObject().put("error", e.toString())
        }
    }

    fun versionName(): String =
        runCatching { context.packageManager.getPackageInfo(context.packageName, 0).versionName }.getOrNull() ?: "unknown"

    // ---- helpers ----------------------------------------------------------------------------

    private fun prop(type: String, description: String): JSONObject =
        JSONObject().put("type", type).put("description", description)

    private fun schema(vararg props: Pair<String, JSONObject>, required: List<String> = emptyList()): JSONObject {
        val properties = JSONObject()
        for ((k, v) in props) properties.put(k, v)
        val schema = JSONObject().put("type", "object").put("properties", properties)
        if (required.isNotEmpty()) schema.put("required", JSONArray(required))
        return schema
    }

    private fun entryPoint(): DebugCommandEntryPoint = DebugCommandEntryPoint.resolve(context)

    /** Commands that act on the emulator take the broadcast receiver's lock, so the two never interleave. */
    private fun <T> locked(block: suspend () -> T): T = runBlocking { DebugCommandExecutionLock.withLock { block() } }

    private fun error(message: String): JSONObject = JSONObject().put("error", message)

    /** The core keeps its last FPS after a game ends; only report it while something runs. */
    private fun fpsOrNull(running: Boolean): Any = if (running) MelonEmulator.getFPS() else JSONObject.NULL

    private fun runningRom(): EmulatorState.RunningRom? =
        DebugCommandStateStore.currentEmulatorState() as? EmulatorState.RunningRom

    // ---- tools ------------------------------------------------------------------------------

    private fun status(@Suppress("UNUSED_PARAMETER") args: JSONObject): JSONObject {
        val state = DebugCommandStateStore.currentEmulatorState()
        val running = state?.isRunning() == true
        val resumed = DevServer.resumedActivities()
        return JSONObject()
            .put("package", context.packageName)
            .put("version", versionName())
            .put("server", DevServer.statusJson(context))
            .put("appInFront", resumed.isNotEmpty())
            .put("resumedActivities", JSONArray(resumed))
            .put("emulatorActivity", DebugCommandStateStore.hasEmulatorActivity())
            .put("state", state?.javaClass?.simpleName ?: JSONObject.NULL)
            .put("running", running)
            .put("rom", (state as? EmulatorState.RunningRom)?.rom?.let { DebugCommands.romJson(it) } ?: JSONObject.NULL)
            .put("firmware", (state as? EmulatorState.RunningFirmware)?.console?.name ?: JSONObject.NULL)
            .put("fps", fpsOrNull(running))
            .put("debugPauseHeld", DebugCommandStateStore.isDebugPauseHeld())
            .put("stats", latestStats(readLogcat("-t", "4000")))
    }

    private fun settingsSet(args: JSONObject): JSONObject {
        val key = args.optString("key").trim().ifEmpty { return error("key required") }
        if (!args.has("value")) return error("value required")
        val raw = when (val value = args.get("value")) {
            is JSONArray -> (0 until value.length()).joinToString(",") { value.get(it).toString() }
            else -> value.toString()
        }
        val type = args.optString("type").trim().ifEmpty { null }
        return locked { DebugCommands.setPreference(entryPoint(), key, raw, type) }
    }

    private fun launch(args: JSONObject): JSONObject {
        var uri = args.optString("uri").trim()
        var name: String? = null
        if (uri.isEmpty()) {
            val query = args.optString("query").trim().ifEmpty { return error("pass query or uri") }
            val roms = runBlocking { DebugCommands.roms(entryPoint(), query) }
            val rom = roms.singleOrNull()
                ?: roms.firstOrNull { it.name.equals(query, true) || it.fileName.equals(query, true) }
                ?: roms.firstOrNull()?.takeIf { args.optBoolean("first") }
            if (rom == null) {
                if (roms.isEmpty()) return error("no ROM matches '$query'")
                return error("several ROMs match; pass a narrower query, first=true, or a uri")
                    .put("matches", JSONArray(roms.take(20).map { it.name }))
            }
            uri = rom.uri.toString()
            name = rom.name
        }

        // The emulator asks before replacing a running game, which nobody would answer
        runningRom()?.let { current ->
            if (current.rom.uri.toString() == uri) {
                return JSONObject().put("launched", uri).put("alreadyRunning", true).put("rom", DebugCommands.romJson(current.rom))
            }
            return error("${current.rom.name} is running: close it first (the PC-side close tool force-stops the app)")
        }

        val pauseAfter = args.optBoolean("pause_after", false)
        val timeoutMs = args.optInt("timeout_ms", 30_000).coerceIn(1_000, 90_000)
        val activitySeen = locked {
            DebugCommands.launchRom(context, Uri.parse(uri), waitReady = true, pauseAfterReady = pauseAfter, requestedTimeoutMs = timeoutMs)
        }
        val result = JSONObject().put("launched", uri).put("name", name ?: JSONObject.NULL).put("activitySeen", activitySeen)
        if (!activitySeen) {
            return result.put("appInFront", DevServer.resumedActivities().isNotEmpty())
                .put("error", "the emulator did not open. Android blocks activity starts while the app is in the background: " +
                    "bring the ROM list to the front (adb shell am start -n ${context.packageName}/me.magnum.melonds.ui.romlist.RomListActivity) and retry")
        }
        if (!args.optBoolean("wait", true)) return result

        val start = System.currentTimeMillis()
        while (System.currentTimeMillis() - start < timeoutMs) {
            when (val state = DebugCommandStateStore.currentEmulatorState()) {
                is EmulatorState.RunningRom -> if (state.rom.uri.toString() == uri) {
                    return result.put("running", true).put("rom", DebugCommands.romJson(state.rom))
                        .put("waitedMs", System.currentTimeMillis() - start)
                }
                is EmulatorState.RomLoadError, is EmulatorState.RomNotFoundError -> {
                    return result.put("error", "ROM failed to load: ${state.javaClass.simpleName}")
                }
                else -> Unit
            }
            Thread.sleep(200)
        }
        return result.put("running", false)
            .put("state", DebugCommandStateStore.currentEmulatorState()?.javaClass?.simpleName ?: JSONObject.NULL)
            .put("error", "the ROM was not running after ${timeoutMs}ms")
    }

    private fun stateCommand(args: JSONObject, save: Boolean): JSONObject {
        val path = args.optString("path").trim().ifEmpty { null }
        val slot = if (path == null) args.optInt("slot", 1) else null
        if (slot != null && slot !in 0..8) return error("slot must be 0-8")
        // a load waits for a ROM that is still starting; a save needs one now
        if (save && runningRom() == null) return error("no game running")
        val pauseAfter = args.optBoolean("pause_after", false)
        val target = DebugCommands.StateTarget(pathOrUri = path, slot = slot)
        val result = locked {
            val entryPoint = entryPoint()
            if (save) {
                DebugCommands.saveState(context, entryPoint, target, pauseAfter)
            } else {
                DebugCommands.loadState(context, entryPoint, target, waitReady = true,
                    requestedTimeoutMs = DebugCommands.DEFAULT_ROM_READY_TIMEOUT_MS, pauseAfterLoad = pauseAfter)
            }
        }
        val json = JSONObject().put("success", result.success).put("slot", slot ?: JSONObject.NULL)
            .put("uri", result.uri?.toString() ?: JSONObject.NULL).put("paused", pauseAfter)
        if (!result.success) json.put("error", if (result.uri == null) "no game running" else "the core refused the state")
        return json
    }

    private fun setPaused(paused: Boolean): JSONObject {
        if (DebugCommandStateStore.currentEmulatorState()?.isRunning() != true) return error("no game running")
        locked { DebugCommands.setDebugPause(paused) }
        return JSONObject().put("paused", paused)
    }

    private fun stats(args: JSONObject): JSONObject {
        val seconds = args.optDouble("seconds", 0.0).takeIf { !it.isNaN() }?.coerceIn(0.0, 30.0) ?: 0.0
        val misses = args.optInt("misses", 20).coerceIn(0, 500)
        val lines = if (seconds > 0.0) {
            val startSeconds = System.currentTimeMillis() / 1000.0
            Thread.sleep((seconds * 1000).toLong())
            // epoch timestamps, so the window doesn't depend on the device's time zone
            readLogcat("-v", "epoch", "-t", "20000").filter {
                (it.trim().substringBefore(' ').toDoubleOrNull() ?: 0.0) >= startSeconds
            }
        } else {
            readLogcat("-t", "4000")
        }
        val missLines = lines.filter { MISS_TAG in it }
        val running = DebugCommandStateStore.currentEmulatorState()?.isRunning() == true
        val result = JSONObject()
            .put("fps", fpsOrNull(running))
            .put("running", running)
            .put("latest", latestStats(lines))
            .put("missLines", missLines.size)
            .put("misses", JSONArray(missLines.map { it.substringAfter(MISS_TAG).trimStart(':', ' ') }.distinct().takeLast(misses)))
        if (seconds > 0.0) {
            result.put("seconds", seconds)
                .put("lines", JSONArray(lines.filter { line -> WINDOW_TAGS.any { it in line } }.map { message(it) }.takeLast(40)))
        }
        return result
    }

    private fun latestStats(lines: List<String>): JSONObject {
        val latest = JSONObject()
        for (tag in STATS_TAGS) {
            lines.lastOrNull { tag in it }?.let { latest.put(tag, it.substringAfter(tag).trimStart(':', ' ').trim()) }
        }
        return latest
    }

    private fun log(args: JSONObject): JSONObject {
        val n = args.optInt("lines", 200).coerceIn(1, 5000)
        val filter = args.optString("filter")
        val level = args.optString("level").trim().uppercase(Locale.US)
        val logArgs = mutableListOf("-t", (if (filter.isEmpty()) n else 20_000).toString())
        if (level.isNotEmpty()) {
            if (level !in listOf("V", "D", "I", "W", "E", "F")) return error("level must be V, D, I, W, E or F")
            logArgs += "*:$level"
        }
        val picked = readLogcat(*logArgs.toTypedArray())
            .filter { filter.isEmpty() || it.contains(filter, ignoreCase = true) }
            .takeLast(n)
        return JSONObject().put("count", picked.size).put("lines", JSONArray(picked))
    }

    /** This process's own log. An app may read its own lines without READ_LOGS. */
    private fun readLogcat(vararg args: String): List<String> {
        val command = listOf("logcat", "-d", "--pid=${Process.myPid()}") + args
        val process = ProcessBuilder(command).redirectErrorStream(true).start()
        val lines = process.inputStream.bufferedReader().readLines()
        process.waitFor(5, TimeUnit.SECONDS)
        return lines.filter { it.isNotBlank() && !it.startsWith("--------- beginning of") }
    }

    /** The message part of a logcat line (after "TAG : "). */
    private fun message(line: String): String = line.substringAfter(": ", line).trim()

    private companion object {
        const val MISS_TAG = "HDTexPack[Miss]"
        val STATS_TAGS = listOf("HDTexPack[Stats]", "VulkanOutput[Stats]", "VulkanPerf[Pacing]", "CoreJit[State]", "HDTexPack: indexed")
        val WINDOW_TAGS = listOf("HDTexPack[Stats]", "HDTexPack: indexed", "VulkanOutput[Stats]", "VulkanPerf", "CoreJit")
    }
}
