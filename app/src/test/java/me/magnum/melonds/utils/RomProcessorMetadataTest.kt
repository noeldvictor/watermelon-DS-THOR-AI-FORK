package me.magnum.melonds.utils

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.ByteArrayInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

class RomProcessorMetadataTest {

    // header (0x200), ARM9 at 0x200, ARM7 at 0x400, optional banner at 0x600
    private fun rom(bannerOffset: Int, bannerTitle: String? = null): ByteArray {
        val size = if (bannerTitle != null) 0x600 + 0xA00 else 0x600
        val data = ByteBuffer.allocate(size).order(ByteOrder.LITTLE_ENDIAN)
        "HOMEBREW".toByteArray().copyInto(data.array(), 0)
        "####".toByteArray().copyInto(data.array(), 0x0C)
        data.putInt(0x20, 0x200)
        data.putInt(0x2C, 0x100)
        data.putInt(0x30, 0x400)
        data.putInt(0x3C, 0x100)
        data.putInt(0x68, bannerOffset)
        if (bannerTitle != null) {
            data.putShort(0x600, 1)
            bannerTitle.toByteArray(Charsets.UTF_16LE).copyInto(data.array(), 0x600 + 0x340)
        }
        return data.array()
    }

    @Test
    fun romWithoutBannerIsStillListed() {
        val metadata = RomProcessor.getRomMetadata(ByteArrayInputStream(rom(bannerOffset = 0)))

        assertNotNull(metadata)
        assertEquals("", metadata!!.romTitle)
        assertEquals("", metadata.developerName)
        assertEquals(32, metadata.retroAchievementsHash.length)
    }

    @Test
    fun bannerTitleAndDeveloperAreRead() {
        val metadata = RomProcessor.getRomMetadata(
            ByteArrayInputStream(rom(bannerOffset = 0x600, bannerTitle = "Meteora\nSomeone"))
        )

        assertNotNull(metadata)
        assertEquals("Meteora", metadata!!.romTitle)
        assertEquals("Someone", metadata.developerName)
    }

    @Test
    fun bannerPastTheEndOfTheFileIsIgnored() {
        val metadata = RomProcessor.getRomMetadata(ByteArrayInputStream(rom(bannerOffset = 0x10000)))

        assertNotNull(metadata)
        assertEquals("", metadata!!.romTitle)
    }

    @Test
    fun missingArm9CodeStillRejectsTheFile() {
        val truncated = rom(bannerOffset = 0).copyOf(0x300)

        assertNull(RomProcessor.getRomMetadata(ByteArrayInputStream(truncated)))
    }
}
