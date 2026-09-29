package me.magnum.melonds.domain.model

import kotlin.math.abs

/**
 * Widescreen codes in cheat databases. DeadSkullzJr's database names them "Widescreen",
 * "Widescreen (16:9)", "Widescreen (16:10)" or "Widescreen v2.0". They widen the game's 3D
 * projection; the frontend then stretches the top screen to the same shape.
 */
object WidescreenCheats {
    // "Widescreen TV" is a furniture item in Animal Crossing's item codes, not a display hack
    private val NAME = Regex("""wide\s*screen(?!\s*tv)|\b16\s*:\s*(9|10)\b""", RegexOption.IGNORE_CASE)
    private val SIXTEEN_BY_TEN = Regex("""\b16\s*:\s*10\b""")

    const val DEFAULT_ASPECT_RATIO = 16f / 9f

    fun isWidescreen(cheat: Cheat): Boolean = NAME.containsMatchIn(cheat.name)

    /** The shape (width / height) [cheat] expects the top screen to have, or null if it isn't a widescreen code. */
    fun aspectRatio(cheat: Cheat): Float? = when {
        !isWidescreen(cheat) -> null
        SIXTEEN_BY_TEN.containsMatchIn(cheat.name) -> 16f / 10f
        else -> DEFAULT_ASPECT_RATIO
    }

    /** The shape of the first enabled widescreen code in [cheats], or null if none is enabled. */
    fun activeAspectRatio(cheats: List<Cheat>): Float? {
        return cheats.firstNotNullOfOrNull { if (it.enabled) aspectRatio(it) else null }
    }

    /** Of a game's widescreen codes, the one whose shape is closest to the display's. */
    fun pickFor(cheats: List<Cheat>, displayAspectRatio: Float): Cheat? {
        return cheats.filter { isWidescreen(it) }.minByOrNull { abs(aspectRatio(it)!! - displayAspectRatio) }
    }
}
