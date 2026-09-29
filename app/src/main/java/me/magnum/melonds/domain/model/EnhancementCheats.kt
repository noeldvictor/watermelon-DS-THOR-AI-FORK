package me.magnum.melonds.domain.model

/**
 * Cheat codes that change how a game looks rather than how it plays: widescreen, anti-aliasing
 * (DeadSkullzJr: "Disable 3D Edge Marking + Enable 3D Anti-Aliasing") and draw distance. They give
 * a game the ROM list's ENH badge and are the switches in the pause menu's Enhancements panel.
 * tools/cheats/build_bundled_cheats.py applies the same rule when it writes the bundled database's
 * index.
 */
object EnhancementCheats {
    enum class Kind {
        WIDESCREEN,
        ANTI_ALIASING,
        DRAW_DISTANCE,
    }

    private val ANTI_ALIASING = Regex("""anti.?alias""", RegexOption.IGNORE_CASE)
    private val DRAW_DISTANCE = Regex("""draw\s*distance""", RegexOption.IGNORE_CASE)

    fun kind(cheatName: String): Kind? = when {
        WidescreenCheats.isWidescreenName(cheatName) -> Kind.WIDESCREEN
        ANTI_ALIASING.containsMatchIn(cheatName) -> Kind.ANTI_ALIASING
        DRAW_DISTANCE.containsMatchIn(cheatName) -> Kind.DRAW_DISTANCE
        else -> null
    }

    fun isEnhancement(cheatName: String): Boolean = kind(cheatName) != null
}
