package me.magnum.melonds.impl

import android.content.Context
import android.net.Uri
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.workDataOf
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.mapNotNull
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import me.magnum.melonds.common.workers.CheatImportWorker
import me.magnum.melonds.database.MelonDatabase
import me.magnum.melonds.database.entities.CheatDatabaseEntity
import me.magnum.melonds.database.entities.CheatEntity
import me.magnum.melonds.database.entities.CheatFolderEntity
import me.magnum.melonds.database.entities.CheatStatusUpdate
import me.magnum.melonds.database.entities.GameEntity
import me.magnum.melonds.domain.model.Cheat
import me.magnum.melonds.domain.model.CheatDatabase
import me.magnum.melonds.domain.model.CheatFolder
import me.magnum.melonds.domain.model.CheatImportProgress
import me.magnum.melonds.domain.model.Game
import me.magnum.melonds.domain.model.RomInfo
import me.magnum.melonds.domain.model.EnhancementCheats
import me.magnum.melonds.domain.repositories.CheatsRepository
import me.magnum.melonds.ui.cheats.model.CheatSubmissionForm

class RoomCheatsRepository(
    private val context: Context,
    private val database: MelonDatabase,
    private val settingsBackupManager: SettingsBackupManager,
) : CheatsRepository {
    companion object {
        private const val TAG = "RoomCheatsRepository"
        private const val IMPORT_WORKER_NAME = "cheat_import_worker"
    }

    private val bundledCheatDatabase = BundledCheatDatabase(context)
    private val bundledImportMutex = Mutex()
    private val codeFixes by lazy { CheatCodeFixes.load(context) }
    // games whose stored cheats were checked against the fixes in this process
    private val fixedGames = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()
    // additions already made once ("<code> <checksum> <cheat>"): one the user deleted stays deleted
    private val additionsMade by lazy { context.getSharedPreferences("cheat_code_additions", Context.MODE_PRIVATE) }

    private fun additionKey(gameCode: String, gameChecksum: String, cheatName: String) =
        "${BundledCheatDatabase.indexKey(gameCode, gameChecksum)} $cheatName"

    override suspend fun getGames(): List<Game> {
        return database.gameDao().getGames().map { game ->
            Game(
                game.id,
                game.name,
                game.gameCode,
                game.gameChecksum,
                emptyList(),
            )
        }
    }

    override suspend fun findGameForRom(romInfo: RomInfo): Game? {
        val gameChecksum = romInfo.headerChecksumString()
        val gameEntity = database.gameDao().findGame(romInfo.gameCode, gameChecksum)
            ?.also { applyCodeFixes(romInfo.gameCode, gameChecksum) }
            ?: importBundledGame(romInfo.gameCode, gameChecksum)

        return gameEntity?.let {
            Game(
                it.id,
                it.name,
                it.gameCode,
                it.gameChecksum,
                emptyList(),
            )
        }
    }

    /**
     * Copies a game's cheats from the bundled database the first time they are asked for. Only
     * games the user opens end up in the app database (and in the settings mirror).
     */
    private suspend fun importBundledGame(gameCode: String, gameChecksum: String): GameEntity? = bundledImportMutex.withLock {
        database.gameDao().findGame(gameCode, gameChecksum)?.let { return@withLock it }

        val bundled = withContext(Dispatchers.IO) {
            runCatching { bundledCheatDatabase.findGame(gameCode, gameChecksum) }
                .onFailure { Log.w(TAG, "Could not read the bundled cheat database", it) }
                .getOrNull()
        } ?: return@withLock null

        val databaseId = database.cheatDatabaseDao().findCheatDatabase(bundled.databaseName)?.id
            ?: addCheatDatabase(bundled.databaseName).id
            ?: return@withLock null
        val fixedFolders = bundled.game.cheats.map { folder ->
            folder.copy(cheats = folder.cheats.map {
                val fixedCode = codeFixes.fixedCode(gameCode, it.name, it.code)
                it.copy(cheatDatabaseId = databaseId, code = fixedCode ?: it.code)
            })
        }
        val existingNames = fixedFolders.flatMap { folder -> folder.cheats.map { it.name } }.toSet()
        val additions = codeFixes.additionsFor(gameCode).filter { it.cheatName !in existingNames }
        val addedFolders = additions.groupBy { it.folderName }.map { (folderName, entries) ->
            CheatFolder(null, folderName, entries.map { Cheat(null, databaseId, it.cheatName, it.description, it.code, false) })
        }
        val cheats = fixedFolders + addedFolders
        addGameCheats(bundled.game.copy(cheats = cheats))
        Log.i(TAG, "Added ${cheats.sumOf { it.cheats.size }} bundled cheats for $gameCode $gameChecksum")

        fixedGames.add(BundledCheatDatabase.indexKey(gameCode, gameChecksum))
        additionsMade.edit().apply {
            additions.forEach { putBoolean(additionKey(gameCode, gameChecksum, it.cheatName), true) }
        }.apply()
        database.gameDao().findGame(gameCode, gameChecksum)
    }

    /**
     * Swaps known-broken codes (assets/cheats/code_fixes.txt) in a game read in from the bundled
     * database before the fix existed. Only a code still exactly the broken one is replaced; the
     * cheat keeps its enabled state.
     */
    private suspend fun applyCodeFixes(gameCode: String, gameChecksum: String) {
        if (!fixedGames.add(BundledCheatDatabase.indexKey(gameCode, gameChecksum))) return
        val fixes = codeFixes.fixesFor(gameCode)
        val additions = codeFixes.additionsFor(gameCode)
        if (fixes.isEmpty() && additions.isEmpty()) return

        var changed = false
        for (name in fixes.map { it.cheatName }.distinct()) {
            for (cheat in database.cheatDao().getRomCheatsNamed(gameCode, gameChecksum, name)) {
                val fixedCode = codeFixes.fixedCode(gameCode, cheat.name, cheat.code) ?: continue
                database.cheatDao().insertCheat(cheat.copy(code = fixedCode))
                Log.i(TAG, "Fixed the code of '${cheat.name}' for $gameCode $gameChecksum")
                changed = true
            }
        }

        val gameId = database.gameDao().findGame(gameCode, gameChecksum)?.id
        val databaseId = gameId?.let { database.cheatDao().getGameCheatDatabaseId(it) }
        if (gameId != null && databaseId != null) {
            for ((folderName, entries) in additions.groupBy { it.folderName }) {
                val missing = entries.filter {
                    !additionsMade.getBoolean(additionKey(gameCode, gameChecksum, it.cheatName), false)
                        && database.cheatDao().getRomCheatsNamed(gameCode, gameChecksum, it.cheatName).isEmpty()
                }
                if (missing.isEmpty()) continue
                val folderId = database.cheatFolderDao().insertCheatFolder(CheatFolderEntity(null, gameId, folderName))
                database.cheatDao().insertCheats(missing.map { CheatEntity(null, folderId, databaseId, it.cheatName, it.description, it.code, false) })
                additionsMade.edit().apply {
                    missing.forEach { putBoolean(additionKey(gameCode, gameChecksum, it.cheatName), true) }
                }.apply()
                Log.i(TAG, "Added ${missing.size} code(s) to $gameCode $gameChecksum: ${missing.joinToString { it.cheatName }}")
                changed = true
            }
        }
        if (changed) settingsBackupManager.requestMirrorWrite()
    }

    override fun getAllGameCheats(game: Game): Flow<List<CheatFolder>> {
        val gameId = game.id ?: return emptyFlow()

        return database.gameDao().getGameCheats(gameId).map { foldersWithCheats ->
            foldersWithCheats.map {
                CheatFolder(
                    it.cheatFolder.id,
                    it.cheatFolder.name,
                    it.cheats.map { cheat ->
                        Cheat(
                            cheat.id,
                            cheat.cheatDatabaseId,
                            cheat.name,
                            cheat.description,
                            cheat.code,
                            cheat.enabled
                        )
                    }
                )
            }
        }
    }

    override fun getFolderCheats(folder: CheatFolder): Flow<List<Cheat>> {
        return database.cheatDao().getFolderCheats(folder.id!!).map {
            it.map { cheat ->
                Cheat(
                    cheat.id,
                    cheat.cheatDatabaseId,
                    cheat.name,
                    cheat.description,
                    cheat.code,
                    cheat.enabled
                )
            }
        }
    }

    override suspend fun getRomEnabledCheats(romInfo: RomInfo): List<Cheat> {
        // a broken code enabled before its fix shipped runs fixed from the next launch
        applyCodeFixes(romInfo.gameCode, romInfo.headerChecksumString())
        return database.cheatDao().getEnabledRomCheats(romInfo.gameCode, romInfo.headerChecksumString()).map { cheat ->
            Cheat(
                cheat.id,
                cheat.cheatDatabaseId,
                cheat.name,
                cheat.description,
                cheat.code,
                cheat.enabled
            )
        }
    }

    /** The game's enhancement codes, from any database (the bundled one is read in on first use). */
    override suspend fun getRomEnhancementCheats(romInfo: RomInfo): List<Cheat> {
        val gameId = findGameForRom(romInfo)?.id ?: return emptyList()
        return database.cheatDao().getGameEnhancementCheatCandidates(gameId).map { cheat ->
            Cheat(
                cheat.id,
                cheat.cheatDatabaseId,
                cheat.name,
                cheat.description,
                cheat.code,
                cheat.enabled
            )
        }.filter { EnhancementCheats.isEnhancement(it.name) }
    }

    override suspend fun updateCheatsStatus(cheats: List<Cheat>) {
        val cheatEntities = cheats.map {
            CheatStatusUpdate(it.id!!, it.enabled)
        }

        database.cheatDao().updateCheatsStatus(cheatEntities)
        settingsBackupManager.requestMirrorWrite()
    }

    override suspend fun addCheatFolder(folderName: String, game: Game) {
        val gameId = if (game.id == null) {
            // It's a new game. Insert it first
            val gameEntity = GameEntity(
                id = null,
                name = game.name,
                gameCode = game.gameCode,
                gameChecksum = game.gameChecksum
            )
            database.gameDao().insertGame(gameEntity)
        } else {
            game.id
        }

        val cheatFolderEntity = CheatFolderEntity(null, gameId, folderName)
        database.cheatFolderDao().insertCheatFolder(cheatFolderEntity)
        settingsBackupManager.requestMirrorWrite()
    }

    override suspend fun deleteCheatDatabaseIfExists(databaseName: String) {
        if (databaseName == CheatDatabaseEntity.CUSTOM_CHEATS_DATABASE_NAME) {
            // Don't allow the custom cheat database to be deleted
            return
        }

        database.cheatDatabaseDao().deleteCheatDatabase(databaseName)
        database.cheatFolderDao().deleteEmptyFolders()
        database.gameDao().deleteEmptyGames()
        settingsBackupManager.requestMirrorWrite()
    }

    override suspend fun addCheatDatabase(databaseName: String): CheatDatabase {
        val cheatDatabaseEntity = CheatDatabaseEntity(
            null,
            databaseName,
        )

        val databaseId = database.cheatDatabaseDao().insertCheatDatabase(cheatDatabaseEntity)
        settingsBackupManager.requestMirrorWrite()
        return CheatDatabase(databaseId, databaseName)
    }

    override suspend fun addGameCheats(game: Game): Game {
        val gameEntity = GameEntity(
            null,
            game.name,
            game.gameCode,
            game.gameChecksum
        )

        // Insertion may do nothing if the game already exists
        database.gameDao().insertGame(gameEntity)
        val insertedGame = database.gameDao().findGame(game.gameCode, game.gameChecksum)!!
        val gameId = insertedGame.id!!

        val categoryEntities = game.cheats.map { category ->
            CheatFolderEntity(
                null,
                gameId,
                category.name
            )
        }
        val categoryIds = database.cheatFolderDao().insertCheatFolders(categoryEntities)

        val cheatEntities = game.cheats.zip(categoryIds).flatMap { pair ->
            pair.first.cheats.map {
                CheatEntity(
                    id = null,
                    cheatFolderId = pair.second,
                    cheatDatabaseId = it.cheatDatabaseId,
                    name = it.name,
                    description = it.description,
                    code = it.code,
                    enabled = false
                )
            }
        }
        database.cheatDao().insertCheats(cheatEntities)

        settingsBackupManager.requestMirrorWrite()
        return Game(
            id = insertedGame.id,
            name = insertedGame.name,
            gameCode = insertedGame.gameCode,
            gameChecksum = insertedGame.gameChecksum,
            cheats = emptyList(),
        )
    }

    override suspend fun addCheat(folder: CheatFolder, cheat: Cheat) {
        val cheatEntity = CheatEntity(
            id = null,
            cheatFolderId = folder.id!!,
            cheatDatabaseId = cheat.cheatDatabaseId,
            name = cheat.name,
            description = cheat.description,
            code = cheat.code,
            enabled = cheat.enabled,
        )

        database.cheatDao().insertCheat(cheatEntity)
        settingsBackupManager.requestMirrorWrite()
    }

    override suspend fun addCustomCheat(folder: CheatFolder, cheatForm: CheatSubmissionForm) {
        val cheatEntity = CheatEntity(
            id = null,
            cheatFolderId = folder.id!!,
            cheatDatabaseId = CheatDatabaseEntity.CUSTOM_CHEATS_DATABASE_ID,
            name = cheatForm.name,
            description = cheatForm.description,
            code = cheatForm.code,
            enabled = false,
        )

        database.cheatDao().insertCheat(cheatEntity)
        settingsBackupManager.requestMirrorWrite()
    }

    override suspend fun updateCheat(cheat: Cheat) {
        val originalEntity = database.cheatDao().getCheat(cheat.id!!) ?: return
        val updatedCheatEntity = CheatEntity(
            id = cheat.id,
            cheatFolderId = originalEntity.cheatFolderId,
            cheatDatabaseId = originalEntity.cheatDatabaseId,
            name = cheat.name,
            description = cheat.description,
            code = cheat.code,
            enabled = cheat.enabled,
        )

        database.cheatDao().insertCheat(updatedCheatEntity)
        settingsBackupManager.requestMirrorWrite()
    }

    override suspend fun deleteCheat(cheat: Cheat) {
        val cheatId = cheat.id ?: return
        database.cheatDao().deleteCheat(cheatId)
        settingsBackupManager.requestMirrorWrite()
    }

    override fun importCheats(uri: Uri) {
        val workRequest = OneTimeWorkRequestBuilder<CheatImportWorker>()
                .setInputData(workDataOf(CheatImportWorker.KEY_URI to uri.toString()))
                .build()

        WorkManager.getInstance(context).enqueueUniqueWork(IMPORT_WORKER_NAME, ExistingWorkPolicy.KEEP, workRequest)
    }

    override fun isCheatImportOngoing(): Boolean {
        val workManager = WorkManager.getInstance(context)
        val statuses = workManager.getWorkInfosForUniqueWork(IMPORT_WORKER_NAME)
        val infos = statuses.get()

        return infos.any { !it.state.isFinished }
    }

    override fun getCheatImportProgress(): Flow<CheatImportProgress> {
        val workManager = WorkManager.getInstance(context)
        return workManager.getWorkInfosForUniqueWorkFlow(IMPORT_WORKER_NAME)
            .mapNotNull { workInfos ->
                val allWorkFinished = workInfos.all { it.state.isFinished }
                if (allWorkFinished) {
                    CheatImportProgress(CheatImportProgress.CheatImportStatus.NOT_IMPORTING, 0f, null)
                } else {
                    val workInfo = workInfos.first()
                    when (workInfo.state) {
                        WorkInfo.State.ENQUEUED -> CheatImportProgress(CheatImportProgress.CheatImportStatus.STARTING, 0f, null)
                        WorkInfo.State.RUNNING -> {
                            val relativeProgress = workInfo.progress.getFloat(CheatImportWorker.KEY_PROGRESS_RELATIVE, 0f)
                            val itemName = workInfo.progress.getString(CheatImportWorker.KEY_PROGRESS_ITEM)
                            CheatImportProgress(CheatImportProgress.CheatImportStatus.ONGOING, relativeProgress, itemName)
                        }
                        WorkInfo.State.SUCCEEDED -> CheatImportProgress(CheatImportProgress.CheatImportStatus.FINISHED, 1f, null)
                        WorkInfo.State.CANCELLED,
                        WorkInfo.State.FAILED -> CheatImportProgress(CheatImportProgress.CheatImportStatus.FAILED, 0f, null)
                        WorkInfo.State.BLOCKED -> null
                    }
                }
            }
    }
}
