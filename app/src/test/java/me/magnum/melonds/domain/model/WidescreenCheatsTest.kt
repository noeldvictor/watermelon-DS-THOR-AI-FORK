package me.magnum.melonds.domain.model

import me.magnum.melonds.impl.findGameInCodelist
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.File
import java.util.zip.ZipFile

class WidescreenCheatsTest {

    private fun cheat(name: String, enabled: Boolean = false) = Cheat(null, 0, name, null, "02000000 00000000", enabled)

    private fun bundledCheats(gameCode: String, checksum: String): List<Cheat> {
        val asset = listOf(File("src/main/assets/cheats/bundled_cheats.zip"), File("app/src/main/assets/cheats/bundled_cheats.zip"))
            .first { it.isFile }
        return ZipFile(asset).use { zip ->
            val bundled = zip.getInputStream(zip.getEntry("$gameCode.xml")).use { findGameInCodelist(it, gameCode, checksum) }
            assertNotNull("$gameCode $checksum", bundled)
            bundled!!.game.cheats.flatMap { it.cheats }
        }
    }

    @Test
    fun namesInTheBundledDatabaseAreRecognised() {
        assertEquals(16f / 9f, WidescreenCheats.aspectRatio(cheat("Widescreen"))!!, 0.001f)
        assertEquals(16f / 9f, WidescreenCheats.aspectRatio(cheat("Widescreen (16:9)"))!!, 0.001f)
        assertEquals(16f / 10f, WidescreenCheats.aspectRatio(cheat("Widescreen (16:10)"))!!, 0.001f)
        assertEquals(16f / 9f, WidescreenCheats.aspectRatio(cheat("Widescreen v2.0"))!!, 0.001f)
        assertNull(WidescreenCheats.aspectRatio(cheat("Widescreen TV")))
        assertNull(WidescreenCheats.aspectRatio(cheat("Max HP")))
    }

    @Test
    fun activeAspectRatioFollowsTheEnabledCode() {
        val cheats = listOf(cheat("Max HP", enabled = true), cheat("Widescreen (16:9)"), cheat("Widescreen (16:10)", enabled = true))
        assertEquals(16f / 10f, WidescreenCheats.activeAspectRatio(cheats)!!, 0.001f)
        assertNull(WidescreenCheats.activeAspectRatio(cheats.map { it.copy(enabled = false) }))
    }

    @Test
    fun theCodeClosestToTheDisplayIsPicked() {
        // Metroid Prime Hunters (USA) carries both shapes
        val hunters = bundledCheats("AMHE", "E18D1857").filter { WidescreenCheats.isWidescreen(it) }
        assertEquals(listOf("Widescreen (16:9)", "Widescreen (16:10)"), hunters.map { it.name })
        assertEquals("Widescreen (16:9)", WidescreenCheats.pickFor(hunters, 1920f / 1080f)!!.name)
        assertEquals("Widescreen (16:10)", WidescreenCheats.pickFor(hunters, 2560f / 1600f)!!.name)

        // Final Fantasy III (Europe) only has 16:10
        val ff3 = bundledCheats("AFFP", "AC31DBEB")
        assertEquals(16f / 10f, WidescreenCheats.aspectRatio(WidescreenCheats.pickFor(ff3, 1920f / 1080f)!!)!!, 0.001f)

        // Star Fox Command (USA)
        assertEquals("Widescreen", WidescreenCheats.pickFor(bundledCheats("ASFE", "F22E6A7D"), 1920f / 1080f)!!.name)
    }
}
