package me.magnum.melonds.database.entities

import androidx.room.ColumnInfo

/** A game that has at least one cheat (ROM list badges). */
data class GameKey(
    @ColumnInfo(name = "game_code") val gameCode: String,
    @ColumnInfo(name = "game_checksum") val gameChecksum: String?,
)

/** A cheat's name with the game it belongs to (ROM list badges). */
data class GameCheatName(
    @ColumnInfo(name = "game_code") val gameCode: String,
    @ColumnInfo(name = "game_checksum") val gameChecksum: String?,
    @ColumnInfo(name = "name") val name: String,
)
