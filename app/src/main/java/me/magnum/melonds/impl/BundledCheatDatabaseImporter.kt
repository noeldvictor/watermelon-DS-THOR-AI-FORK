package me.magnum.melonds.impl

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.runBlocking
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

        val imported = runCatching { parseBundledDatabase() }.getOrElse {
            Log.w(TAG, "Could not import the bundled cheat database", it)
            return
        }

        markSeeded()
        Log.i(TAG, "Imported $imported game(s) from the bundled cheat database")
    }

    private fun parseBundledDatabase(): Int {
        var gameCount = 0

        context.assets.open(ASSET_NAME).use { assetStream ->
            XmlCheatDatabaseParser().parseCheatDatabase(
                ProgressTrackerInputStream(assetStream),
                object : CheatDatabaseParserListener {
                    override fun onDatabaseParseStart(databaseName: String): CheatDatabase = runBlocking {
                        cheatsRepository.deleteCheatDatabaseIfExists(databaseName)
                        cheatsRepository.addCheatDatabase(databaseName)
                    }

                    override fun onGameParseStart(gameName: String) = Unit

                    override fun onGameParsed(game: Game) {
                        runBlocking { cheatsRepository.addGameCheats(game) }
                        gameCount++
                    }

                    override fun onParseComplete() = Unit
                },
            )
        }

        if (gameCount == 0) {
            // A seed with no <game> entries is the shipped placeholder. Leave
            // the version unset so a later real database still gets imported.
            throw IOException("bundled cheat database contains no games")
        }

        return gameCount
    }

    private fun markSeeded() {
        preferences.edit().putInt(KEY_SEEDED_VERSION, SEED_VERSION).apply()
    }
}
