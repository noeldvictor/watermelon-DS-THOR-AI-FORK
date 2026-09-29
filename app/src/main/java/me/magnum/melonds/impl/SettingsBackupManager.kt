package me.magnum.melonds.impl

import android.content.Context
import android.content.ContentValues
import android.content.SharedPreferences
import android.content.SharedPreferences.OnSharedPreferenceChangeListener
import android.database.sqlite.SQLiteDatabase
import android.net.Uri
import android.util.Log
import androidx.core.net.toUri
import androidx.documentfile.provider.DocumentFile
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import me.magnum.melonds.database.MelonDatabase
import me.magnum.melonds.database.entities.CheatDatabaseEntity
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.atomic.AtomicBoolean
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class SettingsBackupManager @Inject constructor(
    @param:ApplicationContext private val context: Context,
    private val preferences: SharedPreferences,
    private val database: MelonDatabase,
) : OnSharedPreferenceChangeListener {
    companion object {
        private const val TAG = "SettingsBackupManager"
        // Not WatermelonDS's "melonDualDS.opts": both apps can share a ROM directory, and
        // pruneStaleMirrors deletes the mirror file from directories it no longer uses
        private const val MELON_DUAL_DS_OPTIONS_FILE = "WatermelonThor.opts"
        private const val SETTINGS_FILE = "settings.json"
        private const val CONTROLLER_FILE = "controller_config.json"
        private const val LAYOUTS_FILE = "layouts.json"
        private const val BACKGROUNDS_FILE = "backgrounds.json"
        private const val INTERNAL_LAYOUT_FILE = "internal_layout.json"
        private const val EXTERNAL_LAYOUT_FILE = "external_layout.json"
        private const val ROM_DATA_FILE = "rom_data.json"
        private const val ROM_METADATA_MIRROR_FILE = "rom_metadata_mirror.json"
        private const val SETTINGS_MIRROR_FALLBACK_URI = "settings_mirror_fallback_uri"
        private const val SETTINGS_MIRROR_ENABLED = "save_internal_config_as_file"
        private val EXCLUDED_PREF_KEYS = setOf(
            "ra_token",
            "rom_search_dirs",
            "bios_dir",
            "dsi_bios_dir",
            SETTINGS_MIRROR_FALLBACK_URI,
        )
        private val CHEAT_TABLES = listOf(
            "cheat_database" to listOf("id", "name"),
            "game" to listOf("id", "name", "game_code", "game_checksum"),
            "cheat_folder" to listOf("id", "game_id", "name"),
            "cheat" to listOf("id", "cheat_folder_id", "cheat_database_id", "name", "description", "code", "enabled"),
        )
        private val CHEAT_RESTORE_DELETE_ORDER = listOf("cheat", "cheat_folder", "game", "cheat_database")
        // 2: only games with enabled or custom cheats, grouped by game
        private const val CHEATS_BACKUP_FORMAT = 2
        private val LONG_PREF_KEYS = setOf(
            "ra_hash_library_last_updated",
            "last_version",
        )
    }

    private val mirrorScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val mirrorWriteQueued = AtomicBoolean(false)

    init {
        preferences.registerOnSharedPreferenceChangeListener(this)
    }

    fun initializeMirror() {
        requestMirrorWrite()
    }

    fun isMirrorEnabled(): Boolean {
        return preferences.getBoolean(SETTINGS_MIRROR_ENABLED, false)
    }

    fun requestMirrorWrite() {
        if (!isMirrorEnabled()) {
            return
        }
        if (!mirrorWriteQueued.compareAndSet(false, true)) {
            return
        }

        mirrorScope.launch {
            try {
                writeInternalMirror()
            } catch (e: Exception) {
                Log.w(TAG, "Failed to write settings mirror", e)
            } finally {
                mirrorWriteQueued.set(false)
            }
        }
    }

    fun backup(treeUri: Uri) {
        val root = DocumentFile.fromTreeUri(context, treeUri) ?: return

        val backupDoc = root.findFile(MELON_DUAL_DS_OPTIONS_FILE)
            ?: root.createFile("application/octet-stream", MELON_DUAL_DS_OPTIONS_FILE)
            ?: return
        context.contentResolver.openOutputStream(backupDoc.uri)?.use { outStream ->
            outStream.writer().use { it.write(createBackupJson().toString()) }
        }
    }

    fun hasMirrorAt(treeUri: Uri): Boolean {
        val mirrorDocument = getMirrorDocument(treeUri)?.takeIf { it.isFile } ?: return false
        return runCatching {
            context.contentResolver.openInputStream(mirrorDocument.uri)?.use { input ->
                JSONObject(input.reader().readText())
            } != null
        }.onFailure {
            Log.w(TAG, "Ignoring invalid settings mirror at $treeUri", it)
        }.getOrDefault(false)
    }

    fun restoreMirrorFrom(treeUri: Uri) {
        runCatching {
            val mirrorDocument = getMirrorDocument(treeUri) ?: return
            context.contentResolver.openInputStream(mirrorDocument.uri)?.use { input ->
                restoreBackupJson(JSONObject(input.reader().readText()))
            }
        }.onFailure {
            Log.w(TAG, "Failed to restore settings mirror from $treeUri", it)
        }
    }

    /** The backup [backup] writes, to an app-private file (developer commands). */
    fun writeBackupTo(file: File) {
        writeTextAtomically(file, createBackupJson().toString())
    }

    /** Restores a file written by [writeBackupTo] or [backup] (developer commands). */
    fun restoreBackupFrom(file: File) {
        restoreBackupJson(JSONObject(file.readText()))
    }

    fun overwriteMirrorAt(treeUri: Uri) {
        writeMirrorToTree(treeUri, createBackupJson().toString())
    }

    fun rememberMirrorFallback(treeUri: Uri) {
        preferences.edit().putString(SETTINGS_MIRROR_FALLBACK_URI, treeUri.toString()).apply()
    }

    fun getActiveMirrorDirectory(): Uri? {
        return getConfiguredMirrorDirectory()
    }

    private fun writeInternalMirror() {
        val mirrorJson = createBackupJson().toString()
        writeTextAtomically(File(context.filesDir, MELON_DUAL_DS_OPTIONS_FILE), mirrorJson)
        val mirrorDirectory = getConfiguredMirrorDirectory()
        if (mirrorDirectory != null) {
            writeMirrorToTree(mirrorDirectory, mirrorJson)
        }
        pruneStaleMirrors(mirrorDirectory)
    }

    private fun getConfiguredMirrorDirectory(): Uri? {
        if (!preferences.getBoolean("use_rom_dir", true)) {
            preferences.getStringSet("sram_dir", null)?.firstOrNull()?.let { return it.toUri() }
        }
        preferences.getString(SETTINGS_MIRROR_FALLBACK_URI, null)?.let { return it.toUri() }
        return preferences.getStringSet("rom_search_dirs", null)?.firstOrNull()?.toUri()
    }

    private fun writeMirrorToTree(treeUri: Uri, mirrorJson: String) {
        val root = DocumentFile.fromTreeUri(context, treeUri) ?: return
        val mirrorDocument = root.findFile(MELON_DUAL_DS_OPTIONS_FILE)
            ?: root.createFile("application/octet-stream", MELON_DUAL_DS_OPTIONS_FILE)
            ?: return
        context.contentResolver.openOutputStream(mirrorDocument.uri, "wt")?.use { output ->
            output.writer().use { it.write(mirrorJson) }
        }
    }

    private fun getMirrorDocument(treeUri: Uri): DocumentFile? {
        return DocumentFile.fromTreeUri(context, treeUri)?.findFile(MELON_DUAL_DS_OPTIONS_FILE)
    }

    private fun pruneStaleMirrors(currentMirrorDirectory: Uri?) {
        val candidates = mutableSetOf<Uri>()
        preferences.getStringSet("sram_dir", null)?.forEach { candidates.add(it.toUri()) }
        preferences.getStringSet("rom_search_dirs", null)?.forEach { candidates.add(it.toUri()) }
        preferences.getString(SETTINGS_MIRROR_FALLBACK_URI, null)?.let { candidates.add(it.toUri()) }
        candidates
            .filter { it != currentMirrorDirectory }
            .forEach { uri ->
                runCatching { getMirrorDocument(uri)?.delete() }.onFailure {
                    Log.w(TAG, "Failed to delete stale settings mirror from $uri", it)
                }
            }
    }

    private fun createBackupJson(): JSONObject {
        return JSONObject().apply {
            put("version", 1)
            put("settings", createSettingsJson())
            putFileJson("controllerConfig", CONTROLLER_FILE)
            putFileJson("layouts", LAYOUTS_FILE)
            putFileJson("backgrounds", BACKGROUNDS_FILE)
            createSanitizedRomsJson()?.let { put("roms", it) }
            put("cheats", createCheatsJson())
        }
    }

    private fun createSettingsJson(): JSONObject {
        val json = JSONObject()
        for ((key, value) in preferences.all) {
            if (key in EXCLUDED_PREF_KEYS) continue
            when (value) {
                is Boolean, is Int, is Long, is Float, is String -> json.put(key, value)
                is Set<*> -> {
                    val array = JSONArray()
                    value.forEach { array.put(it) }
                    json.put(key, array)
                }
            }
        }
        return json
    }

    private fun JSONObject.putFileJson(key: String, fileName: String) {
        readJsonFile(fileName)?.let { put(key, it) }
    }

    private fun readJsonFile(fileName: String): Any? {
        val file = File(context.filesDir, fileName)
        if (!file.exists()) {
            return null
        }
        val text = file.readText()
        return when (text.trim().firstOrNull()) {
            '[' -> JSONArray(text)
            '{' -> JSONObject(text)
            else -> null
        }
    }

    private fun createSanitizedRomsJson(): JSONArray? {
        val roms = readJsonFile(ROM_DATA_FILE) as? JSONArray ?: return null
        val sanitized = JSONArray()
        for (i in 0 until roms.length()) {
            val rom = roms.optJSONObject(i) ?: continue
            sanitized.put(JSONObject(rom.toString()).apply {
                remove("uri")
                remove("parentTreeUri")
            })
        }
        return sanitized
    }

    fun restore(treeUri: Uri) {
        val root = DocumentFile.fromTreeUri(context, treeUri) ?: return
        val backupDoc = root.findFile(MELON_DUAL_DS_OPTIONS_FILE)
        if (backupDoc != null) {
            context.contentResolver.openInputStream(backupDoc.uri)?.use { input ->
                restoreBackupJson(JSONObject(input.reader().readText()))
            }
            return
        }

        val settingsDoc = root.findFile(SETTINGS_FILE)
        settingsDoc?.uri?.let { uri ->
            context.contentResolver.openInputStream(uri)?.use { input ->
                val text = input.reader().readText()
                val json = JSONObject(text)
                val editor = preferences.edit()
                for (key in json.keys()) {
                    if (key in EXCLUDED_PREF_KEYS) continue
                    val value = json.get(key)
                    when (value) {
                        is Boolean -> editor.putBoolean(key, value)
                        is Int -> {
                            if (key in LONG_PREF_KEYS) {
                                editor.putLong(key, value.toLong())
                            } else {
                                editor.putInt(key, value)
                            }
                        }
                        is Long -> editor.putLong(key, value)
                        is Double -> editor.putFloat(key, value.toFloat())
                        is String -> editor.putString(key, value)
                        is JSONArray -> {
                            val set = mutableSetOf<String>()
                            for (i in 0 until value.length()) {
                                set.add(value.getString(i))
                            }
                            editor.putStringSet(key, set)
                        }
                        is Number -> {
                            val current = preferences.all[key]
                            when {
                                current is Long || key in LONG_PREF_KEYS -> editor.putLong(key, value.toLong())
                                current is Int -> editor.putInt(key, value.toInt())
                                current is Float -> editor.putFloat(key, value.toFloat())
                                value is Double -> editor.putFloat(key, value.toFloat())
                                else -> editor.putLong(key, value.toLong())
                            }
                        }
                    }
                }
                editor.apply()
            }
        }

        val controllerDoc = root.findFile(CONTROLLER_FILE)
        controllerDoc?.uri?.let { uri ->
            val dest = File(context.filesDir, CONTROLLER_FILE)
            context.contentResolver.openInputStream(uri)?.use { input ->
                writeBytesAtomically(dest, input.readBytes())
            }
        }

        val layoutsDoc = root.findFile(LAYOUTS_FILE)
        layoutsDoc?.uri?.let { uri ->
            val dest = File(context.filesDir, LAYOUTS_FILE)
            context.contentResolver.openInputStream(uri)?.use { input ->
                writeBytesAtomically(dest, input.readBytes())
            }
        }

        val romDataDoc = root.findFile(ROM_DATA_FILE)
        romDataDoc?.uri?.let { uri ->
            context.contentResolver.openInputStream(uri)?.use { input ->
                val text = input.reader().readText()
                JSONArray(text)
                writeTextAtomically(File(context.filesDir, ROM_DATA_FILE), text)
            }
        }
    }

    private fun restoreBackupJson(json: JSONObject) {
        json.optJSONObject("settings")?.let { restoreSettingsJson(it) }
        restoreJsonFile(json, "controllerConfig", CONTROLLER_FILE)
        restoreJsonFile(json, "layouts", LAYOUTS_FILE)
        restoreJsonFile(json, "backgrounds", BACKGROUNDS_FILE)
        restoreRomsJson(json)
        json.optJSONObject("cheats")?.let { restoreCheatsJson(it) }
        requestMirrorWrite()
    }

    private fun restoreSettingsJson(json: JSONObject) {
        val editor = preferences.edit()
        for (key in json.keys()) {
            if (key in EXCLUDED_PREF_KEYS) continue
            val value = json.get(key)
            when (value) {
                is Boolean -> editor.putBoolean(key, value)
                is Int -> {
                    if (key in LONG_PREF_KEYS) {
                        editor.putLong(key, value.toLong())
                    } else {
                        editor.putInt(key, value)
                    }
                }
                is Long -> editor.putLong(key, value)
                is Double -> editor.putFloat(key, value.toFloat())
                is String -> editor.putString(key, value)
                is JSONArray -> {
                    val set = mutableSetOf<String>()
                    for (i in 0 until value.length()) {
                        set.add(value.getString(i))
                    }
                    editor.putStringSet(key, set)
                }
                is Number -> {
                    val current = preferences.all[key]
                    when {
                        current is Long || key in LONG_PREF_KEYS -> editor.putLong(key, value.toLong())
                        current is Int -> editor.putInt(key, value.toInt())
                        current is Float -> editor.putFloat(key, value.toFloat())
                        value is Double -> editor.putFloat(key, value.toFloat())
                        else -> editor.putLong(key, value.toLong())
                    }
                }
            }
        }
        editor.apply()
    }

    private fun restoreJsonFile(root: JSONObject, key: String, fileName: String) {
        val value = root.opt(key) ?: return
        if (value !is JSONObject && value !is JSONArray) {
            Log.w(TAG, "Skipping invalid backup value for $key")
            return
        }
        writeTextAtomically(File(context.filesDir, fileName), value.toString())
    }

    private fun restoreRomsJson(root: JSONObject) {
        val roms = root.optJSONArray("roms") ?: return
        for (i in 0 until roms.length()) {
            val rom = roms.optJSONObject(i) ?: return
            if (!rom.has("uri")) {
                writeTextAtomically(File(context.filesDir, ROM_METADATA_MIRROR_FILE), roms.toString())
                return
            }
        }
        writeTextAtomically(File(context.filesDir, ROM_DATA_FILE), roms.toString())
    }

    /**
     * The cheats only this install has: every game where the user enabled a cheat or wrote one,
     * whole (all folders and cheats, from every database), so restoring it brings the state back.
     *
     * Imported databases are left out; they can be imported again, and the bundled one comes back
     * by itself. Copying them made every settings change serialise the whole cheat database into
     * one JSON string - a full DeadSkullzJr import is ~600k cheats, enough to run out of memory.
     */
    private fun createCheatsJson(): JSONObject {
        val db = database.openHelper.readableDatabase

        val databaseNames = mutableMapOf<Long, String>()
        db.query("SELECT id, name FROM cheat_database").use {
            while (it.moveToNext())
                databaseNames[it.getLong(0)] = it.getString(1)
        }

        val gameIds = mutableListOf<Long>()
        db.query(
            "SELECT DISTINCT cheat_folder.game_id FROM cheat JOIN cheat_folder ON cheat.cheat_folder_id = cheat_folder.id " +
                "WHERE cheat.enabled = 1 OR cheat.cheat_database_id = ? ORDER BY cheat_folder.game_id",
            arrayOf<Any?>(CheatDatabaseEntity.CUSTOM_CHEATS_DATABASE_ID),
        ).use {
            while (it.moveToNext())
                gameIds.add(it.getLong(0))
        }

        val games = JSONArray()
        for (gameId in gameIds) {
            val game = db.query("SELECT name, game_code, game_checksum FROM game WHERE id = ?", arrayOf<Any?>(gameId)).use {
                if (!it.moveToFirst()) return@use null
                JSONObject()
                    .put("name", it.getString(0))
                    .put("gameCode", it.getString(1))
                    .put("gameChecksum", it.getString(2))
            } ?: continue

            val folders = JSONArray()
            db.query("SELECT id, name FROM cheat_folder WHERE game_id = ? ORDER BY id", arrayOf<Any?>(gameId)).use { folderCursor ->
                while (folderCursor.moveToNext()) {
                    val cheats = JSONArray()
                    db.query(
                        "SELECT cheat_database_id, name, description, code, enabled FROM cheat WHERE cheat_folder_id = ? ORDER BY id",
                        arrayOf<Any?>(folderCursor.getLong(0)),
                    ).use { cheatCursor ->
                        while (cheatCursor.moveToNext()) {
                            cheats.put(JSONObject()
                                .put("database", databaseNames[cheatCursor.getLong(0)] ?: CheatDatabaseEntity.CUSTOM_CHEATS_DATABASE_NAME)
                                .put("name", cheatCursor.getString(1))
                                .put("description", if (cheatCursor.isNull(2)) JSONObject.NULL else cheatCursor.getString(2))
                                .put("code", cheatCursor.getString(3))
                                .put("enabled", cheatCursor.getInt(4) != 0))
                        }
                    }
                    folders.put(JSONObject().put("name", folderCursor.getString(1)).put("cheats", cheats))
                }
            }
            games.put(game.put("folders", folders))
        }

        return JSONObject()
            .put("format", CHEATS_BACKUP_FORMAT)
            .put("games", games)
    }

    private fun restoreCheatsJson(json: JSONObject) {
        if (json.optInt("format", 1) >= CHEATS_BACKUP_FORMAT) {
            restoreCheatGames(json.optJSONArray("games") ?: JSONArray())
            requestMirrorWrite()
            return
        }

        // Format 1 (before 2026-09-29): every cheat table, restored over the current database
        val writableDb = database.openHelper.writableDatabase
        writableDb.beginTransaction()
        try {
            for (tableName in CHEAT_RESTORE_DELETE_ORDER) {
                writableDb.delete(tableName, null, emptyArray())
            }

            for ((tableName, columns) in CHEAT_TABLES) {
                val rows = json.optJSONArray(tableName) ?: continue
                for (i in 0 until rows.length()) {
                    val row = rows.optJSONObject(i) ?: continue
                    val values = ContentValues()
                    for (column in columns) {
                        if (!row.has(column) || row.isNull(column)) {
                            values.putNull(column)
                            continue
                        }

                        when (val value = row.get(column)) {
                            is Boolean -> values.put(column, if (value) 1 else 0)
                            is Int -> values.put(column, value)
                            is Long -> values.put(column, value)
                            is Double -> values.put(column, value)
                            is String -> values.put(column, value)
                            is Number -> values.put(column, value.toLong())
                            else -> values.put(column, value.toString())
                        }
                    }
                    writableDb.insert(tableName, SQLiteDatabase.CONFLICT_REPLACE, values)
                }
            }
            writableDb.setTransactionSuccessful()
        } finally {
            writableDb.endTransaction()
        }
        requestMirrorWrite()
    }

    /**
     * Puts each backed-up game back as it was: its current cheats are replaced, other games and
     * databases are left alone.
     */
    private fun restoreCheatGames(games: JSONArray) {
        val db = database.openHelper.writableDatabase
        db.beginTransaction()
        try {
            val databaseIds = mutableMapOf<String, Long>()
            fun databaseId(name: String): Long = databaseIds.getOrPut(name) {
                db.query("SELECT id FROM cheat_database WHERE name = ?", arrayOf<Any?>(name)).use {
                    if (it.moveToFirst()) return@getOrPut it.getLong(0)
                }
                db.insert("cheat_database", SQLiteDatabase.CONFLICT_ABORT, ContentValues().apply { put("name", name) })
            }

            for (i in 0 until games.length()) {
                val game = games.optJSONObject(i) ?: continue
                val gameCode = game.optString("gameCode").takeIf { it.isNotEmpty() } ?: continue
                val gameChecksum = game.optString("gameChecksum").takeIf { it.isNotEmpty() } ?: continue

                val existingId = db.query(
                    "SELECT id FROM game WHERE game_code = ? AND game_checksum = ?",
                    arrayOf<Any?>(gameCode, gameChecksum),
                ).use { if (it.moveToFirst()) it.getLong(0) else null }
                val gameId = if (existingId != null) {
                    db.execSQL("DELETE FROM cheat WHERE cheat_folder_id IN (SELECT id FROM cheat_folder WHERE game_id = ?)", arrayOf<Any?>(existingId))
                    db.execSQL("DELETE FROM cheat_folder WHERE game_id = ?", arrayOf<Any?>(existingId))
                    existingId
                } else {
                    db.insert("game", SQLiteDatabase.CONFLICT_ABORT, ContentValues().apply {
                        put("name", game.optString("name", gameCode))
                        put("game_code", gameCode)
                        put("game_checksum", gameChecksum)
                    })
                }

                val folders = game.optJSONArray("folders") ?: continue
                for (f in 0 until folders.length()) {
                    val folder = folders.optJSONObject(f) ?: continue
                    val folderId = db.insert("cheat_folder", SQLiteDatabase.CONFLICT_ABORT, ContentValues().apply {
                        put("game_id", gameId)
                        put("name", folder.optString("name"))
                    })
                    val cheats = folder.optJSONArray("cheats") ?: continue
                    for (c in 0 until cheats.length()) {
                        val cheat = cheats.optJSONObject(c) ?: continue
                        db.insert("cheat", SQLiteDatabase.CONFLICT_ABORT, ContentValues().apply {
                            put("cheat_folder_id", folderId)
                            put("cheat_database_id", databaseId(cheat.optString("database", CheatDatabaseEntity.CUSTOM_CHEATS_DATABASE_NAME)))
                            put("name", cheat.optString("name"))
                            if (cheat.isNull("description")) putNull("description") else put("description", cheat.optString("description"))
                            put("code", cheat.optString("code"))
                            put("enabled", if (cheat.optBoolean("enabled")) 1 else 0)
                        })
                    }
                }
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    override fun onSharedPreferenceChanged(sharedPreferences: SharedPreferences, key: String?) {
        if (key == SETTINGS_MIRROR_ENABLED && isMirrorEnabled()) {
            return
        }
        requestMirrorWrite()
    }

    fun backupInternalLayout(treeUri: Uri) {
        backupUnifiedLayout(treeUri, INTERNAL_LAYOUT_FILE)
    }

    fun backupExternalLayout(treeUri: Uri) {
        backupUnifiedLayout(treeUri, EXTERNAL_LAYOUT_FILE)
    }

    fun restoreInternalLayout(treeUri: Uri) {
        restoreUnifiedLayout(treeUri, INTERNAL_LAYOUT_FILE)
    }

    fun restoreExternalLayout(treeUri: Uri) {
        restoreUnifiedLayout(treeUri, EXTERNAL_LAYOUT_FILE)
    }

    private fun backupUnifiedLayout(treeUri: Uri, fileName: String) {
        val root = DocumentFile.fromTreeUri(context, treeUri) ?: return

        val layoutsText = File(context.filesDir, LAYOUTS_FILE)
            .takeIf { it.exists() }
            ?.let { runCatching { it.readText() }.getOrNull() }
            ?: "[]"
        val layouts = runCatching { JSONArray(layoutsText) }.getOrNull() ?: return

        val dest = root.findFile(fileName) ?: root.createFile("application/json", fileName) ?: return
        context.contentResolver.openOutputStream(dest.uri)?.use { out ->
            out.writer().use { it.write(layouts.toString()) }
        }
    }

    private fun restoreUnifiedLayout(treeUri: Uri, fileName: String) {
        val root = DocumentFile.fromTreeUri(context, treeUri) ?: return
        val src = root.findFile(fileName) ?: return

        val layouts = context.contentResolver.openInputStream(src.uri)?.use { input ->
            runCatching { JSONArray(input.reader().readText()) }.getOrNull()
        } ?: return
        writeTextAtomically(File(context.filesDir, LAYOUTS_FILE), layouts.toString())
    }

    private fun writeTextAtomically(file: File, text: String) {
        writeBytesAtomically(file, text.toByteArray())
    }

    private fun writeBytesAtomically(file: File, bytes: ByteArray) {
        val tempFile = File(file.parentFile, "${file.name}.tmp")
        FileOutputStream(tempFile).use { stream ->
            stream.write(bytes)
            stream.flush()
            runCatching { stream.fd.sync() }
        }

        if (!tempFile.renameTo(file)) {
            if (file.exists() && !file.delete()) {
                throw IllegalStateException("Could not replace ${file.absolutePath}")
            }
            if (!tempFile.renameTo(file)) {
                throw IllegalStateException("Could not move ${tempFile.absolutePath} to ${file.absolutePath}")
            }
        }
    }
}
