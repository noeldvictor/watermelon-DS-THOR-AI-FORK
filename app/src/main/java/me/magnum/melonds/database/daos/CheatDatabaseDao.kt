package me.magnum.melonds.database.daos

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.Query
import me.magnum.melonds.database.entities.CheatDatabaseEntity

@Dao
interface CheatDatabaseDao {
    @Insert
    suspend fun insertCheatDatabase(database: CheatDatabaseEntity): Long

    @Query("SELECT * FROM cheat_database WHERE name = :databaseName LIMIT 1")
    suspend fun findCheatDatabase(databaseName: String): CheatDatabaseEntity?

    @Query("DELETE FROM cheat_database WHERE name = :databaseName")
    suspend fun deleteCheatDatabase(databaseName: String)
}