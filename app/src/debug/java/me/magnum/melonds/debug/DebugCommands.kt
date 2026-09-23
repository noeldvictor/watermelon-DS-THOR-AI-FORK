package me.magnum.melonds.debug

import android.app.ActivityOptions
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.util.Log
import androidx.core.content.edit
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import me.magnum.melonds.MelonEmulator
import me.magnum.melonds.domain.model.SaveStateSlot
import me.magnum.melonds.domain.model.rom.Rom
import me.magnum.melonds.ui.emulator.EmulatorActivity
import me.magnum.melonds.ui.emulator.model.EmulatorState
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.LinkedHashSet

/**
 * The debug commands both transports share: [DebugCommandReceiver] (adb broadcasts, used by
 * tools/thor_mcp) and [DevServer] (the on-device MCP server). Each takes plain arguments, so
 * the receiver only parses intent extras and the server only parses JSON.
 */
internal object DebugCommands {
    private const val TAG = "DebugCommand"
    private const val REQUEST_CODE_LAUNCH_ROM = 1
    private const val ACTION_LAUNCH_ROM_SUFFIX = "LAUNCH_ROM"
    private const val LAUNCH_ACTIVITY_SEEN_TIMEOUT_MS = 2_000L
    private const val ROM_URI_RESOLVE_TIMEOUT_MS = 4_000L
    private const val ROM_URI_RESOLVE_STEP_MS = 100L
    const val DEFAULT_ROM_READY_TIMEOUT_MS = 8_000
    const val MAX_RECEIVER_WAIT_TIMEOUT_MS = 8_000

    /** Where a save state goes: an explicit path/URI, or a slot of a ROM (default: the running one). */
    data class StateTarget(
        val pathOrUri: String? = null,
        val slot: Int? = null,
        val romUri: String? = null,
    )

    data class StateResult(val success: Boolean, val uri: Uri?)

    /** All preferences as a JSON object, optionally only keys containing `filter`. */
    fun preferences(entryPoint: DebugCommandEntryPoint, filter: String): JSONObject {
        val json = JSONObject()
        entryPoint.sharedPreferences().all.toSortedMap().forEach { (key, value) ->
            if (filter.isEmpty() || key.contains(filter, ignoreCase = true)) {
                json.put(key, if (value is Set<*>) JSONArray(value.toList()) else value ?: JSONObject.NULL)
            }
        }
        return json
    }

    /**
     * Sets one preference. The stored value's type decides how `raw` is parsed; a key that
     * doesn't exist yet needs `type` (boolean, int, long, float, set, string). Written through
     * SharedPreferences, so the settings listeners apply it to a running game the same way the
     * settings screen does.
     */
    fun setPreference(entryPoint: DebugCommandEntryPoint, key: String, raw: String, type: String?): JSONObject {
        val preferences = entryPoint.sharedPreferences()
        val old = preferences.all[key]
        val resolvedType = type ?: when (old) {
            is Boolean -> "boolean"
            is Int -> "int"
            is Long -> "long"
            is Float -> "float"
            is Set<*> -> "set"
            null -> throw IllegalArgumentException("Unknown preference $key: pass type to create it")
            else -> "string"
        }
        preferences.edit(commit = true) {
            when (resolvedType) {
                "boolean" -> putBoolean(key, raw.toBooleanStrict())
                "int" -> putInt(key, raw.toInt())
                "long" -> putLong(key, raw.toLong())
                "float" -> putFloat(key, raw.toFloat())
                "set" -> putStringSet(key, raw.split(',').filter { it.isNotEmpty() }.toSet())
                "string" -> putString(key, raw)
                else -> throw IllegalArgumentException("Unknown type $resolvedType")
            }
        }
        val new = preferences.all[key]
        // push the change into a running game, as the other setting commands do
        val refreshed = DebugCommandStateStore.requestSettingsRefresh()
        Log.w(TAG, "action=set_preference key=$key old=$old new=$new refreshed=${if (refreshed) 1 else 0}")
        return JSONObject()
            .put("key", key)
            .put("type", resolvedType)
            .put("old", (old as? Set<*>)?.let { JSONArray(it.toList()) } ?: old ?: JSONObject.NULL)
            .put("new", (new as? Set<*>)?.let { JSONArray(it.toList()) } ?: new ?: JSONObject.NULL)
            .put("appliedToRunningGame", refreshed)
    }

    fun fps(): JSONObject {
        return JSONObject()
            .put("fps", MelonEmulator.getFPS())
            .put("running", DebugCommandStateStore.isRunningRom())
    }

    /** The ROM library, sorted by name, optionally only names/files containing `query`. */
    suspend fun roms(entryPoint: DebugCommandEntryPoint, query: String): List<Rom> {
        return entryPoint.romsRepository().getRoms().first()
            .filter { query.isEmpty() || it.name.contains(query, true) || it.fileName.contains(query, true) }
            .sortedBy { it.name }
    }

    /** The ROM library as JSON: name, file and the URI [launchRom] takes. */
    suspend fun listRoms(entryPoint: DebugCommandEntryPoint, query: String): JSONArray {
        val roms = JSONArray()
        roms(entryPoint, query).forEach { roms.put(romJson(it)) }
        return roms
    }

    fun romJson(rom: Rom): JSONObject {
        return JSONObject().put("name", rom.name).put("file", rom.fileName).put("uri", rom.uri.toString())
    }

    /**
     * Opens the emulator on `romUri`. Returns whether an EmulatorActivity was seen after
     * [LAUNCH_ACTIVITY_SEEN_TIMEOUT_MS]; the ROM itself may still be loading. Android refuses
     * the start while the app is in the background.
     */
    suspend fun launchRom(
        context: Context,
        romUri: Uri,
        waitReady: Boolean,
        pauseAfterReady: Boolean,
        requestedTimeoutMs: Int,
    ): Boolean {
        if (waitReady) {
            DebugCommandStateStore.requestPauseAfterNextRunningRom(pauseAfterReady)
        }

        startEmulatorActivity(
            context = context,
            launchIntent = Intent(context, EmulatorActivity::class.java).apply {
                action = "${context.packageName}.$ACTION_LAUNCH_ROM_SUFFIX"
                data = romUri
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
                addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
            },
        )

        delay(LAUNCH_ACTIVITY_SEEN_TIMEOUT_MS)
        val activitySeen = DebugCommandStateStore.hasEmulatorActivity()
        val ready = DebugCommandStateStore.isRunningRom()
        if (ready && waitReady) {
            setDebugPause(pauseAfterReady)
        }
        Log.w(
            TAG,
            "action=launch_rom uri=$romUri waitReady=${if (waitReady) 1 else 0} activitySeen=${if (activitySeen) 1 else 0} ready=${if (ready) 1 else 0} pauseAfter=${if (pauseAfterReady) 1 else 0} requestedTimeoutMs=$requestedTimeoutMs deferredReady=1",
        )
        return activitySeen
    }

    private fun startEmulatorActivity(context: Context, launchIntent: Intent) {
        val options = ActivityOptions.makeBasic()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            val mode = if (Build.VERSION.SDK_INT >= 36) {
                ActivityOptions.MODE_BACKGROUND_ACTIVITY_START_ALLOW_ALWAYS
            } else {
                @Suppress("DEPRECATION")
                ActivityOptions.MODE_BACKGROUND_ACTIVITY_START_ALLOWED
            }
            options.setPendingIntentBackgroundActivityStartMode(mode)
            options.setPendingIntentCreatorBackgroundActivityStartMode(mode)
        }

        val pendingIntent = PendingIntent.getActivity(
            context,
            REQUEST_CODE_LAUNCH_ROM,
            launchIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        pendingIntent.send(
            context,
            0,
            null,
            null,
            null,
            null,
            options.toBundle(),
        )
    }

    suspend fun loadState(
        context: Context,
        entryPoint: DebugCommandEntryPoint,
        target: StateTarget,
        waitReady: Boolean,
        requestedTimeoutMs: Int,
        pauseAfterLoad: Boolean,
    ): StateResult {
        val timeoutMs = requestedTimeoutMs.coerceAtMost(MAX_RECEIVER_WAIT_TIMEOUT_MS)
        if (waitReady) {
            val ready = DebugCommandStateStore.waitForRunningRom(timeoutMs.toLong())
            if (!ready) {
                Log.w(
                    TAG,
                    "action=load_state waitReady=1 ready=0 success=0 pauseAfter=${if (pauseAfterLoad) 1 else 0} timeoutMs=$timeoutMs requestedTimeoutMs=$requestedTimeoutMs",
                )
                return StateResult(success = false, uri = null)
            }
        }
        val stateUri = resolveStateUri(context, entryPoint, target, preferExistingSlotFallback = true)
            ?: throw IllegalArgumentException("Missing load target. Provide slot or path.")
        MelonEmulator.pauseEmulation()
        val success = try {
            MelonEmulator.loadState(stateUri)
        } finally {
            if (pauseAfterLoad) {
                DebugCommandStateStore.setDebugPauseHeld(true)
            } else {
                DebugCommandStateStore.setDebugPauseHeld(false)
                MelonEmulator.resumeEmulation()
            }
        }
        Log.w(
            TAG,
            "action=load_state uri=$stateUri waitReady=${if (waitReady) 1 else 0} success=${if (success) 1 else 0} pauseAfter=${if (pauseAfterLoad) 1 else 0} timeoutMs=$timeoutMs requestedTimeoutMs=$requestedTimeoutMs",
        )
        return StateResult(success, stateUri)
    }

    suspend fun saveState(
        context: Context,
        entryPoint: DebugCommandEntryPoint,
        target: StateTarget,
        pauseAfterSave: Boolean,
    ): StateResult {
        val stateUri = resolveStateUri(context, entryPoint, target, preferExistingSlotFallback = false)
            ?: throw IllegalArgumentException("Missing save target. Provide slot or path.")
        MelonEmulator.pauseEmulation()
        val success = try {
            MelonEmulator.saveState(stateUri)
        } finally {
            if (pauseAfterSave) {
                DebugCommandStateStore.setDebugPauseHeld(true)
            } else {
                DebugCommandStateStore.setDebugPauseHeld(false)
                MelonEmulator.resumeEmulation()
            }
        }
        Log.w(
            TAG,
            "action=save_state uri=$stateUri success=${if (success) 1 else 0} pauseAfter=${if (pauseAfterSave) 1 else 0}",
        )
        return StateResult(success, stateUri)
    }

    /** Holds the emulator paused (true) or lets it run (false), as the pause_after options do. */
    fun setDebugPause(paused: Boolean) {
        if (paused) {
            DebugCommandStateStore.setDebugPauseHeld(true)
            MelonEmulator.pauseEmulation()
        } else {
            DebugCommandStateStore.setDebugPauseHeld(false)
            MelonEmulator.resumeEmulation()
        }
    }

    private suspend fun resolveStateUri(
        context: Context,
        entryPoint: DebugCommandEntryPoint,
        target: StateTarget,
        preferExistingSlotFallback: Boolean,
    ): Uri? {
        target.pathOrUri?.takeIf { it.isNotBlank() }?.let { pathOrUri ->
            return parseUri(pathOrUri)
        }

        val slot = target.slot ?: return null
        require(slot in 0..8) { "Unsupported save state slot=$slot" }

        val romUri = resolveRomUriForSlot(context, target.romUri) ?: return null
        val rom = entryPoint.romsRepository().getRomAtUri(romUri) ?: return null
        val resolvedUri = entryPoint.saveStatesRepository().getRomSaveStateUri(
            rom,
            SaveStateSlot(slot, exists = true, lastUsedDate = null, screenshot = null),
        )
        if (!preferExistingSlotFallback) {
            return resolvedUri
        }

        val fallbackUri = resolveExistingSlotFallbackUri(
            preferredUri = resolvedUri,
            romFileName = rom.fileName,
            slot = slot,
        ) ?: return resolvedUri
        Log.w(TAG, "action=slot_fallback slot=$slot preferred=$resolvedUri fallback=$fallbackUri")
        return fallbackUri
    }

    private suspend fun resolveRomUriForSlot(context: Context, explicitRomUri: String?): Uri? {
        explicitRomUri?.takeIf { it.isNotBlank() }?.let { return Uri.parse(it) }

        // The running ROM first: the stored last URI only follows debug launches (the ROM list
        // passes the ROM as an extra, not as intent data), so it can name another game.
        (DebugCommandStateStore.currentEmulatorState() as? EmulatorState.RunningRom)?.let { return it.rom.uri }

        var romUri = DebugCommandStateStore.getLastRomUri(context)
        if (romUri != null) {
            return romUri
        }

        val deadlineAt = System.nanoTime() + ROM_URI_RESOLVE_TIMEOUT_MS * 1_000_000L
        while (romUri == null && System.nanoTime() < deadlineAt) {
            delay(ROM_URI_RESOLVE_STEP_MS)
            romUri = DebugCommandStateStore.getLastRomUri(context)
        }
        return romUri
    }

    private fun resolveExistingSlotFallbackUri(
        preferredUri: Uri,
        romFileName: String,
        slot: Int,
    ): Uri? {
        if (preferredUri.scheme != "file") {
            return null
        }
        val preferredPath = preferredUri.path ?: return null
        val preferredFile = File(preferredPath)
        if (preferredFile.exists() && preferredFile.length() > 0L) {
            return null
        }
        val parentDirectory = preferredFile.parentFile
            ?.takeIf { it.exists() && it.isDirectory }
            ?: return null

        val romName = romFileName.substringBeforeLast('.', romFileName).trim()
        if (romName.isEmpty()) {
            return null
        }
        val candidateFile = buildAlternativeSaveStateNames(romName).asSequence()
            .map { candidateName -> File(parentDirectory, "$candidateName.ml$slot") }
            .firstOrNull { file -> file.exists() && file.length() > 0L }
            ?: return null
        return Uri.fromFile(candidateFile)
    }

    private fun buildAlternativeSaveStateNames(romName: String): List<String> {
        val normalized = romName.trim()
        if (normalized.isEmpty()) {
            return emptyList()
        }

        val names = LinkedHashSet<String>()
        val analogSuffixes = listOf(" Analog", " (Analog)", " [Analog]", "[Analog]")
        analogSuffixes.forEach { suffix ->
            if (normalized.endsWith(suffix, ignoreCase = true)) {
                val stripped = normalized.dropLast(suffix.length).trimEnd()
                if (stripped.isNotEmpty()) {
                    names.add(stripped)
                }
            }
        }
        if (!normalized.endsWith(" Analog", ignoreCase = true)) {
            names.add("$normalized Analog")
        }
        return names.toList()
    }

    private fun parseUri(pathOrUri: String): Uri {
        val file = File(pathOrUri)
        return if (file.isAbsolute) {
            Uri.fromFile(file)
        } else {
            Uri.parse(pathOrUri)
        }
    }
}
