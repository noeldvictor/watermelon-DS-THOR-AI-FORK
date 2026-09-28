package me.magnum.melonds.impl

import android.content.Context
import me.magnum.melonds.common.cheats.CheatDatabaseParserListener
import me.magnum.melonds.common.cheats.ProgressTrackerInputStream
import me.magnum.melonds.common.cheats.XmlCheatDatabaseParser
import me.magnum.melonds.domain.model.CheatDatabase
import me.magnum.melonds.domain.model.Game
import java.io.File
import java.io.InputStream
import java.util.zip.ZipFile

/**
 * The cheat database shipped with the app: DeadSkullzJr's NDS(i) Cheat Database, as
 * `assets/cheats/bundled_cheats.zip` with one `<GAMECODE>.xml` codelist per game code (built by
 * tools/cheats/build_bundled_cheats.py).
 *
 * A game's cheats are read the first time they're asked for instead of importing ~600k cheats up
 * front: the app database, and the settings mirror that copies every cheat in it, only ever hold
 * games the user opened.
 */
class BundledCheatDatabase(private val context: Context) {

    data class BundledGame(val databaseName: String, val game: Game)

    companion object {
        private const val ASSET_PATH = "cheats/bundled_cheats.zip"
        const val PENDING_DATABASE_ID = -1L
    }

    private var archive: ZipFile? = null

    @Synchronized
    fun findGame(gameCode: String, gameChecksum: String): BundledGame? {
        val zip = openArchive()
        val entry = zip.getEntry("${gameCode.uppercase()}.xml") ?: return null
        return zip.getInputStream(entry).use { findGameInCodelist(it, gameCode, gameChecksum) }
    }

    // ZipFile needs a real file; the asset is stored uncompressed in the APK (.zip is on aapt's
    // no-compress list), so its length is known without reading it
    private fun openArchive(): ZipFile {
        archive?.let { return it }

        val assetLength = context.assets.openFd(ASSET_PATH).use { it.length }
        val file = File(context.noBackupFilesDir, ASSET_PATH)
        if (!file.isFile || file.length() != assetLength) {
            file.parentFile?.mkdirs()
            val staging = File(file.path + ".tmp")
            context.assets.open(ASSET_PATH).use { input ->
                staging.outputStream().use { output -> input.copyTo(output) }
            }
            if (!staging.renameTo(file)) {
                staging.delete()
                throw IllegalStateException("Could not store the bundled cheat database at $file")
            }
        }

        return ZipFile(file).also { archive = it }
    }
}

/**
 * Parses one codelist and returns the game whose code and header checksum match, with its cheats
 * still pointing at [BundledCheatDatabase.PENDING_DATABASE_ID].
 */
internal fun findGameInCodelist(stream: InputStream, gameCode: String, gameChecksum: String): BundledCheatDatabase.BundledGame? {
    var parsedDatabaseName: String? = null
    var match: Game? = null

    XmlCheatDatabaseParser().parseCheatDatabase(
        ProgressTrackerInputStream(stream),
        object : CheatDatabaseParserListener {
            override fun onDatabaseParseStart(databaseName: String): CheatDatabase {
                parsedDatabaseName = databaseName
                return CheatDatabase(BundledCheatDatabase.PENDING_DATABASE_ID, databaseName)
            }

            override fun onGameParseStart(gameName: String) = Unit

            override fun onGameParsed(game: Game) {
                if (match == null && game.gameCode.equals(gameCode, ignoreCase = true) && game.gameChecksum.equals(gameChecksum, ignoreCase = true)) {
                    match = game
                }
            }

            override fun onParseComplete() = Unit
        },
    )

    val game = match ?: return null
    return BundledCheatDatabase.BundledGame(parsedDatabaseName ?: "Bundled cheats", game)
}
