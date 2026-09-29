package me.magnum.melonds.impl

import me.magnum.melonds.common.cheats.CheatDatabaseParserListener
import me.magnum.melonds.common.cheats.ProgressTrackerInputStream
import me.magnum.melonds.common.cheats.XmlCheatDatabaseParser
import me.magnum.melonds.domain.model.CheatDatabase
import me.magnum.melonds.domain.model.EnhancementCheats
import me.magnum.melonds.domain.model.Game
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File
import java.io.InputStream
import java.util.zip.ZipFile

class BundledCheatAssetTest {

    private class Result(var databaseName: String? = null, val games: MutableList<Game> = mutableListOf())

    private fun parse(stream: InputStream): Result {
        val result = Result()
        XmlCheatDatabaseParser().parseCheatDatabase(
            ProgressTrackerInputStream(stream),
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

    private fun bundledZip(): ZipFile {
        val asset = listOf(File("src/main/assets/cheats/bundled_cheats.zip"), File("app/src/main/assets/cheats/bundled_cheats.zip"))
            .first { it.isFile }
        return ZipFile(asset)
    }

    @Test
    fun bundledDatabaseHasTheGameMatchingCodeAndChecksum() {
        bundledZip().use { zip ->
            val entry = zip.getEntry("BSDE.xml")
            val bundled = zip.getInputStream(entry).use { findGameInCodelist(it, "BSDE", "d2d1805e") }

            assertNotNull(bundled)
            assertEquals("DeadSkullzJr's NDS Cheat Database", bundled!!.databaseName)
            assertEquals("BSDE", bundled.game.gameCode)
            assertEquals("D2D1805E", bundled.game.gameChecksum)
            val cheats = bundled.game.cheats.flatMap { it.cheats }
            assertEquals(37, cheats.size)
            assertTrue(cheats.all { it.cheatDatabaseId == BundledCheatDatabase.PENDING_DATABASE_ID })
            // the anti-piracy code sits directly under <game>
            assertTrue(cheats.any { it.name == "Anti-Piracy Bypass Code" })
        }
    }

    @Test
    fun bundledDatabaseIgnoresOtherChecksums() {
        bundledZip().use { zip ->
            val bundled = zip.getInputStream(zip.getEntry("BSDE.xml")).use { findGameInCodelist(it, "BSDE", "00000000") }
            assertNull(bundled)
        }
    }

    @Test
    fun everyBundledEntryParses() {
        var games = 0
        var cheats = 0
        bundledZip().use { zip ->
            for (entry in zip.entries()) {
                val result = zip.getInputStream(entry).use { parse(it) }
                assertTrue(entry.name, result.games.isNotEmpty())
                assertTrue(entry.name, result.games.all { "${it.gameCode}.xml" == entry.name })
                games += result.games.size
                cheats += result.games.sumOf { game -> game.cheats.sumOf { it.cheats.size } }
            }
        }

        // tools/cheats/build_bundled_cheats.py reports these for the 20211225 database
        assertEquals(4079, games)
        assertEquals(597353, cheats)
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

        val result = parse(ByteArrayInputStream(xml.toByteArray()))

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

    // build_bundled_cheats.py writes the index with its own copy of the enhancement rule; it must
    // agree with EnhancementCheats for every game, or the ENH badge and the pause-menu switch differ
    @Test
    fun bundledIndexMatchesTheDatabase() {
        val indexFile = listOf(File("src/main/assets/cheats/bundled_cheats_index.txt"), File("app/src/main/assets/cheats/bundled_cheats_index.txt"))
            .first { it.isFile }
        val index = indexFile.useLines { parseBundledCheatIndex(it) }

        val expected = HashMap<String, BundledCheatDatabase.IndexEntry>()
        bundledZip().use { zip ->
            for (entry in zip.entries()) {
                for (game in zip.getInputStream(entry).use { parse(it) }.games) {
                    val key = BundledCheatDatabase.indexKey(game.gameCode, game.gameChecksum.orEmpty())
                    val names = game.cheats.flatMap { folder -> folder.cheats.map { it.name } }
                    val previous = expected[key]
                    expected[key] = BundledCheatDatabase.IndexEntry(
                        cheatCount = (previous?.cheatCount ?: 0) + names.size,
                        enhanced = previous?.enhanced == true || names.any { EnhancementCheats.isEnhancement(it) },
                    )
                }
            }
        }

        assertEquals(expected, index)
        assertEquals(309, index.values.count { it.enhanced })
        // Diddy Kong Racing DS (USA): widescreen; Lufia (USA): cheats, no enhancement code
        assertEquals(BundledCheatDatabase.IndexEntry(cheatCount = index.getValue("AWDE 21C9768B").cheatCount, enhanced = true), index["AWDE 21C9768B"])
        assertEquals(BundledCheatDatabase.IndexEntry(cheatCount = 37, enhanced = false), index["BSDE D2D1805E"])
    }

    @Test
    fun enhancementNames() {
        listOf(
            "Widescreen",
            "Widescreen (16:10)",
            "Disable 3D Edge Marking + Enable 3D Anti-Aliasing",
            "Disable 3D Edge Marking/Enable 3D Anti-Aliasing",
            "Max/Infinite Draw Distance",
        ).forEach { assertTrue(it, EnhancementCheats.isEnhancement(it)) }
        listOf("Widescreen TV", "Max HP", "Infinite Health FPS Mode").forEach { assertFalse(it, EnhancementCheats.isEnhancement(it)) }
        assertEquals(EnhancementCheats.Kind.WIDESCREEN, EnhancementCheats.kind("Widescreen v2.0"))
        assertEquals(EnhancementCheats.Kind.ANTI_ALIASING, EnhancementCheats.kind("Disable 3D Edge Marking + Enable 3D Anti-Aliasing"))
        assertEquals(EnhancementCheats.Kind.DRAW_DISTANCE, EnhancementCheats.kind("Max/Infinite Draw Distance"))
    }
}
