package me.magnum.melonds.domain.model

/**
 * What the ROM list badges a game for: an installed HD texture pack, enhancement codes
 * ([EnhancementCheats]) and cheats of any kind, from the bundled database or the user's own.
 */
data class RomFeatures(
    val hdTextures: Boolean,
    val enhanced: Boolean,
    val cheats: Boolean,
) {
    companion object {
        val NONE = RomFeatures(hdTextures = false, enhanced = false, cheats = false)
    }
}
