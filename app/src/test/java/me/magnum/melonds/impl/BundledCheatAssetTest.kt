package me.magnum.melonds.impl

import me.magnum.melonds.common.cheats.CheatDatabaseParserListener
import me.magnum.melonds.common.cheats.ProgressTrackerInputStream
import me.magnum.melonds.common.cheats.XmlCheatDatabaseParser
import me.magnum.melonds.domain.model.CheatDatabase
import me.magnum.melonds.domain.model.Game
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File

class BundledCheatAssetTest {

    private class Result(var databaseName: String? = null, val games: MutableList<Game> = mutableListOf())

    private fun parse(bytes: ByteArray): Result {
        val result = Result()
        XmlCheatDatabaseParser().parseCheatDatabase(
            ProgressTrackerInputStream(ByteArrayInputStream(bytes)),
            object : CheatDatabaseParserListener {
                override fun onDatabaseParseStart(databaseName: String): CheatDatabase {
                    result.databaseName = databaseName
                    return CheatDatabase(-1L, databaseName)
                }

                override fun onGameParseStart(gameName: String) = Unit

                override fun onGameParsed(game: Game) {
                    result.games.add(game)
                }

                override fun onParseComplete() = Unit
            },
        )
        return result
    }

    @Test
    fun shippedAssetIsWellFormed() {
        // A nested "<!--" in its header comment made every launch fail to parse it
        val asset = listOf(File("src/main/assets/usrcheat.xml"), File("app/src/main/assets/usrcheat.xml"))
            .first { it.isFile }

        val result = parse(asset.readBytes())

        assertEquals("Bundled cheats", result.databaseName)
    }

    @Test
    fun cheatsInFoldersAndDirectlyUnderTheGameAreParsed() {
        val xml = """
            <?xml version="1.0" encoding="UTF-8"?>
            <codelist>
                <name>Test (v1)</name>
                <game>
                    <name>Game</name>
                    <gameid>ABCD 12345678</gameid>
                    <cheat>
                        <name>Master code</name>
                        <codes>02000000 00000001</codes>
                    </cheat>
                    <folder>
                        <name>Folder</name>
                        <cheat>
                            <name>Max HP</name>
                            <note>All party members</note>
                            <codes>02000004 000003E7</codes>
                        </cheat>
                    </folder>
                    <cheat>
                        <name>Max gold</name>
                        <codes>02000008 0098967F</codes>
                    </cheat>
                </game>
            </codelist>
        """.trimIndent()

        val result = parse(xml.toByteArray())

        assertEquals("Test", result.databaseName)
        assertEquals(1, result.games.size)
        val game = result.games[0]
        assertEquals("ABCD", game.gameCode)
        assertEquals("12345678", game.gameChecksum)
        assertEquals(listOf("Game", "Folder"), game.cheats.map { it.name })
        assertEquals(listOf("Master code", "Max gold"), game.cheats[0].cheats.map { it.name })
        assertEquals(listOf("Max HP"), game.cheats[1].cheats.map { it.name })
        assertEquals("All party members", game.cheats[1].cheats[0].description)
        assertEquals("02000004 000003E7", game.cheats[1].cheats[0].code)
        assertTrue(game.cheats.flatMap { it.cheats }.all { it.cheatDatabaseId == -1L })
    }
}
