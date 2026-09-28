package me.magnum.melonds.impl

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.FileInputStream
import java.io.FileOutputStream

class SaveFileImportTest {

    @get:Rule
    val folder = TemporaryFolder()

    private val save = ByteArray(8192) { (it * 31).toByte() }

    @Test
    fun importingTheTargetSaveItselfKeepsItsContent() {
        val cache = folder.newFolder("cache")
        val target = folder.newFile("game.sav").apply { writeBytes(save) }

        // the picker hands out the same file under another URI; the target open truncates it
        copySaveWithSnapshot(
            cacheDirectory = cache,
            maxBytes = 64L * 1024L * 1024L,
            openSource = { FileInputStream(target) },
            openTarget = { FileOutputStream(target, false) },
        )

        assertArrayEquals(save, target.readBytes())
        assertEquals(0, cache.listFiles()!!.size)
    }

    @Test
    fun emptySourceLeavesTheTargetUntouched() {
        val cache = folder.newFolder("cache")
        val source = folder.newFile("empty.sav")
        val target = folder.newFile("game.sav").apply { writeBytes(save) }

        val result = runCatching {
            copySaveWithSnapshot(
                cacheDirectory = cache,
                maxBytes = 64L * 1024L * 1024L,
                openSource = { FileInputStream(source) },
                openTarget = { FileOutputStream(target, false) },
            )
        }

        assertTrue(result.exceptionOrNull() is IllegalArgumentException)
        assertArrayEquals(save, target.readBytes())
    }
}
