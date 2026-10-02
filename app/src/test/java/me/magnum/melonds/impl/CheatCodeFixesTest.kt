package me.magnum.melonds.impl

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.util.zip.ZipFile

class CheatCodeFixesTest {

    private fun assetFile(name: String): File =
        listOf(File("src/main/assets/cheats/$name"), File("app/src/main/assets/cheats/$name")).first { it.isFile }

    private fun shippedFixes(): CheatCodeFixes =
        CheatCodeFixes(assetFile("code_fixes.txt").useLines { CheatCodeFixes.parse(it) })

    @Test
    fun everyFixIsWellFormedAndMatchesTheBundledCode() {
        val fixes = CheatCodeFixes.parse(assetFile("code_fixes.txt").readLines().asSequence())
        assertTrue(fixes.isNotEmpty())
        val bundled = ZipFile(assetFile("bundled_cheats.zip"))
        for (fix in fixes) {
            for (code in listOf(fix.brokenCode, fix.fixedCode)) {
                val words = code.split(' ')
                // the native parser reads 8-digit hex words, two per line of a code
                assertEquals("${fix.gameCode} ${fix.cheatName}: even word count", 0, words.size % 2)
                assertTrue("${fix.gameCode} ${fix.cheatName}: $code", words.all { it.matches(Regex("[0-9A-F]{8}")) })
            }
            // the broken code must be one the bundled database really ships, or the fix never applies
            val xml = bundled.getInputStream(bundled.getEntry("${fix.gameCode}.xml")).bufferedReader().readText()
            val shipped = Regex("<codes>(.*?)</codes>", RegexOption.DOT_MATCHES_ALL).findAll(xml).map { CheatCodeFixes.normalize(it.groupValues[1]) }
            assertTrue("${fix.gameCode} ${fix.cheatName}: broken code not in the bundled database", fix.brokenCode in shipped.toList())
        }
    }

    @Test
    fun starFoxWidescreenIsReplacedOnlyWhileUnchanged() {
        val fixes = shippedFixes()
        val broken = "52324998 00001555 02324998 00001C72 D2000000 00000000"
        val fixed = fixes.fixedCode("ASFE", "Widescreen", broken)
        assertNotNull(fixed)
        assertTrue(fixed!!.startsWith("420CF1F0 02000000"))
        // stored with other spacing or case: still the same code
        assertEquals(fixed, fixes.fixedCode("asfe", "Widescreen", broken.lowercase().replace(" ", "  ")))
        // a user's edit, another cheat, another game: untouched
        assertNull(fixes.fixedCode("ASFE", "Widescreen", "02324998 00001C72"))
        assertNull(fixes.fixedCode("ASFE", "Infinite Health", broken))
        assertNull(fixes.fixedCode("AMCE", "Widescreen", broken))
        // already fixed: nothing more to do
        assertNull(fixes.fixedCode("ASFE", "Widescreen", fixed))
    }
}
