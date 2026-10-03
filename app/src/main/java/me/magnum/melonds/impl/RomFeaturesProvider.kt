package me.magnum.melonds.impl

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import me.magnum.melonds.database.MelonDatabase
import me.magnum.melonds.domain.model.EnhancementCheats
import me.magnum.melonds.domain.model.RomFeatures
import me.magnum.melonds.domain.model.rom.Rom
import java.io.File

/**
 * Works out the ROM list badges: HD (a texture pack is installed for the game code), ENH (the game
 * has an enhancement code, see [EnhancementCheats]) and CHT (it has cheats). Cheats come from the
 * bundled database's index, without opening it, and from the app's own cheat database (imports,
 * custom cheats). ROMs whose game code isn't known yet get [RomFeatures.NONE].
 */
class RomFeaturesProvider(
    private val context: Context,
    private val database: MelonDatabase,
) {
    private companion object {
        const val TAG = "RomFeaturesProvider"
    }

    private val bundledCheatDatabase = BundledCheatDatabase(context)
    private val codeFixes by lazy { CheatCodeFixes.load(context) }

    /** Features by ROM URI (as a string). */
    suspend fun getFeatures(roms: List<Rom>): Map<String, RomFeatures> = withContext(Dispatchers.IO) {
        val bundledIndex = runCatching { bundledCheatDatabase.index() }
            .onFailure { Log.w(TAG, "Could not read the bundled cheat index", it) }
            .getOrDefault(emptyMap())

        val gameDao = database.gameDao()
        val gamesWithCheats = gameDao.getGamesWithCheats().mapTo(HashSet()) { userGameKey(it.gameCode, it.gameChecksum) }
        val gamesWithEnhancements = gameDao.getEnhancementCheatCandidates()
            .filter { EnhancementCheats.isEnhancement(it.name) }
            .mapTo(HashSet()) { userGameKey(it.gameCode, it.gameChecksum) }

        // texturepacks/<GAMECODE>.zip (the standard pack) or a texturepacks/<GAMECODE> folder, as
        // HDPackSource looks them up; a folder with nothing in it is no pack
        val packDirectories = File(context.filesDir, "texturepacks").listFiles()
            ?.mapNotNull {
                when {
                    it.isFile && it.name.endsWith(".zip") && it.length() > 0 -> it.name.removeSuffix(".zip")
                    it.isDirectory && !it.list().isNullOrEmpty() -> it.name
                    else -> null
                }
            }
            ?.toHashSet()
            .orEmpty()

        roms.associate { rom ->
            val gameCode = rom.gameCode
            val headerChecksum = rom.headerChecksum
            val features = if (gameCode == null || headerChecksum == null) {
                RomFeatures.NONE
            } else {
                val bundled = bundledIndex[BundledCheatDatabase.indexKey(gameCode, headerChecksum)]
                // our added codes (code_fixes.txt) join a game the bundled database has
                val added = bundled != null && codeFixes.additionsFor(gameCode).any { EnhancementCheats.isEnhancement(it.cheatName) }
                // cheats imported without a checksum apply to every revision of the code
                val userKeys = listOf(userGameKey(gameCode, headerChecksum), userGameKey(gameCode, null))
                RomFeatures(
                    hdTextures = texturePackDirectoryName(gameCode) in packDirectories,
                    enhanced = bundled?.enhanced == true || added || userKeys.any { it in gamesWithEnhancements },
                    cheats = (bundled?.cheatCount ?: 0) > 0 || userKeys.any { it in gamesWithCheats },
                )
            }
            rom.uri.toString() to features
        }
    }

    private fun userGameKey(gameCode: String, gameChecksum: String?): String {
        return "${gameCode.uppercase()} ${gameChecksum.orEmpty().uppercase()}"
    }

    // the native side replaces characters that can't be in a folder name
    private fun texturePackDirectoryName(gameCode: String): String {
        return gameCode.map { if (it.code < 0x21 || it.code > 0x7E || it == '/' || it == '\\' || it == ':') '_' else it }.joinToString("")
    }
}
