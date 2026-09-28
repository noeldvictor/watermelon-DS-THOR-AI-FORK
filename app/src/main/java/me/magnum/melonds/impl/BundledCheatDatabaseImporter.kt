package me.magnum.melonds.impl

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import dagger.hilt.android.qualifiers.ApplicationContext
import me.magnum.melonds.common.cheats.CheatDatabaseParserListener
import me.magnum.melonds.common.cheats.ProgressTrackerInputStream
import me.magnum.melonds.common.cheats.XmlCheatDatabaseParser
import me.magnum.melonds.domain.model.CheatDatabase
import me.magnum.melonds.domain.model.Game
import me.magnum.melonds.domain.repositories.CheatsRepository
import java.io.IOException
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Imports the cheat database bundled in assets so a fresh install starts with
 * cheats already available.
 *
 * Runs only when the cheat database holds no games, so a user who deletes the
 * imported database (or curates their own) is never fought with. [SEED_VERSION]
 * exists so that replacing the bundled file can re-seed installs that already
 * ran the importer - bump it whenever the asset content changes.
 */
@Singleton
class BundledCheatDatabaseImporter @Inject constructor(
    @ApplicationContext private val context: Context,
    private val cheatsRepository: CheatsRepository,
) {

    companion object {
        private const val TAG = "BundledCheats"
        private const val ASSET_NAME = "usrcheat.xml"
        private const val PREFS_NAME = "bundled_cheats"
        private const val KEY_SEEDED_VERSION = "seeded_version"

        /** Bump when [ASSET_NAME] changes so existing installs re-seed. */
        private const val SEED_VERSION = 1

        private const val PENDING_DATABASE_ID = -1L
    }

    private val preferences: SharedPreferences
        get() = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    suspend fun importIfNeeded() {
        if (preferences.getInt(KEY_SEEDED_VERSION, 0) >= SEED_VERSION) {
            return
        }

        if (cheatsRepository.getGames().isNotEmpty()) {
            // The user already has cheats. Don't touch their database, but
            // record the version so we stop checking on every launch.
            markSeeded()
            return
        }

        val imported = runCatching { importBundledDatabase() }.getOrElse {
            Log.w(TAG, "Could not import the bundled cheat database", it)
            return
        }

        if (imported == 0) {
            // A seed with no <game> entries is the shipped placeholder. Leave
            // the version unset so a later real database still gets imported.
            Log.i(TAG, "The bundled cheat database holds no games; nothing imported")
            return
        }

        markSeeded()
        Log.i(TAG, "Imported $imported game(s) from the bundled cheat database")
    }

    private suspend fun importBundledDatabase(): Int {
        var parsedDatabaseName: String? = null
        val games = mutableListOf<Game>()

        // Parse everything first: the database is created only when the file
        // holds games, so the placeholder never leaves an empty entry behind.
        // Cheats carry a pending database id until then.
        context.assets.open(ASSET_NAME).use { assetStream ->
            XmlCheatDatabaseParser().parseCheatDatabase(
                ProgressTrackerInputStream(assetStream),
                object : CheatDatabaseParserListener {
                    override fun onDatabaseParseStart(databaseName: String): CheatDatabase {
                        parsedDatabaseName = databaseName
                        return CheatDatabase(PENDING_DATABASE_ID, databaseName)
                    }

                    override fun onGameParseStart(gameName: String) = Unit

                    override fun onGameParsed(game: Game) {
                        games.add(game)
                    }

                    override fun onParseComplete() = Unit
                },
            )
        }

        val name = parsedDatabaseName ?: return 0
        if (games.isEmpty()) {
            return 0
        }

        cheatsRepository.deleteCheatDatabaseIfExists(name)
        val databaseId = cheatsRepository.addCheatDatabase(name).id
            ?: throw IOException("cheat database $name was not stored")
        games.forEach { game ->
            val cheats = game.cheats.map { folder ->
                folder.copy(cheats = folder.cheats.map { it.copy(cheatDatabaseId = databaseId) })
            }
            cheatsRepository.addGameCheats(game.copy(cheats = cheats))
        }

        return games.size
    }

    private fun markSeeded() {
        preferences.edit().putInt(KEY_SEEDED_VERSION, SEED_VERSION).apply()
    }
}
