package me.magnum.melonds.domain.model

/**
 * Cheat codes that change how a game looks rather than how it plays: widescreen, anti-aliasing
 * (DeadSkullzJr: "Disable 3D Edge Marking + Enable 3D Anti-Aliasing") and draw distance. They give
 * a game the ROM list's ENH badge. tools/cheats/build_bundled_cheats.py applies the same rule when it
 * writes the bundled database's index.
 */
object EnhancementCheats {
    private val VISUAL = Regex("""anti.?alias|draw\s*distance""", RegexOption.IGNORE_CASE)

    fun isEnhancement(cheatName: String): Boolean {
        return WidescreenCheats.isWidescreenName(cheatName) || VISUAL.containsMatchIn(cheatName)
    }
}
