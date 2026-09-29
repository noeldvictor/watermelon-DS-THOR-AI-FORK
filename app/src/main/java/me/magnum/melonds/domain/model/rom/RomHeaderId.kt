package me.magnum.melonds.domain.model.rom

/**
 * A ROM's game code and header checksum (inverted CRC32 of the 0x200-byte header, upper-case hex),
 * the pair cheat databases identify a cartridge by.
 */
data class RomHeaderId(
    val gameCode: String,
    val headerChecksum: String,
)
