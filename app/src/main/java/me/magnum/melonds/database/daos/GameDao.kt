package me.magnum.melonds.database.daos

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Transaction
import kotlinx.coroutines.flow.Flow
import me.magnum.melonds.database.entities.CheatFolderWithCheats
import me.magnum.melonds.database.entities.GameCheatName
import me.magnum.melonds.database.entities.GameEntity
import me.magnum.melonds.database.entities.GameKey

@Dao
interface GameDao {
    @Query("SELECT * FROM game")
    suspend fun getGames(): List<GameEntity>

    @Query("SELECT * FROM game WHERE id = :gameId")
    suspend fun getGame(gameId: Long): GameEntity?

    @Query("SELECT * FROM game WHERE game_code = :gameCode AND game_checksum = :gameChecksum")
    suspend fun findGame(gameCode: String, gameChecksum: String): GameEntity?

    @Transaction
    @Query("SELECT * FROM cheat_folder WHERE game_id = :gameId")
    fun getGameCheats(gameId: Long): Flow<List<CheatFolderWithCheats>>

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertGame(game: GameEntity): Long

    @Query("SELECT DISTINCT game.game_code, game.game_checksum FROM game JOIN cheat_folder ON cheat_folder.game_id = game.id JOIN cheat ON cheat.cheat_folder_id = cheat_folder.id")
    suspend fun getGamesWithCheats(): List<GameKey>

    // Candidates only (LIKE is case-insensitive for ASCII); EnhancementCheats decides
    @Query("SELECT game.game_code, game.game_checksum, cheat.name FROM game JOIN cheat_folder ON cheat_folder.game_id = game.id JOIN cheat ON cheat.cheat_folder_id = cheat_folder.id WHERE cheat.name LIKE '%wide%' OR cheat.name LIKE '%16:%' OR cheat.name LIKE '%alias%' OR cheat.name LIKE '%distance%'")
    suspend fun getEnhancementCheatCandidates(): List<GameCheatName>

    @Query("DELETE FROM game WHERE id NOT IN (SELECT DISTINCT game_id FROM cheat_folder)")
    suspend fun deleteEmptyGames()
}