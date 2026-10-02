package me.magnum.melonds.impl

import android.content.Context

/**
 * Our fixes to broken codes in the bundled cheat database (assets/cheats/code_fixes.txt). A fix
 * replaces a cheat's code only while it is exactly the broken one, so a code the user edited is
 * left alone. Applied when a game is read in from the bundled database, and to games read in
 * before the fix existed.
 */
class CheatCodeFixes(private val fixes: List<Fix>) {

    data class Fix(val gameCode: String, val cheatName: String, val brokenCode: String, val fixedCode: String)

    companion object {
        private const val ASSET_PATH = "cheats/code_fixes.txt"

        fun load(context: Context): CheatCodeFixes {
            val fixes = runCatching {
                context.assets.open(ASSET_PATH).bufferedReader().useLines { parse(it) }
            }.getOrDefault(emptyList())
            return CheatCodeFixes(fixes)
        }

        fun parse(lines: Sequence<String>): List<Fix> = lines
            .map { it.trim() }
            .filter { it.isNotEmpty() && !it.startsWith("#") }
            .mapNotNull { line ->
                val fields = line.split('|')
                if (fields.size < 4) return@mapNotNull null
                Fix(fields[0].trim().uppercase(), fields[1].trim(), normalize(fields[2]), normalize(fields[3]))
            }
            .toList()

        /** Words separated by single spaces, upper case: the stored form, and the one compared. */
        fun normalize(code: String): String = code.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ").uppercase()
    }

    fun isEmpty(): Boolean = fixes.isEmpty()

    fun fixesFor(gameCode: String): List<Fix> = fixes.filter { it.gameCode == gameCode.uppercase() }

    /** The fixed code for this cheat, or null when it needs none. */
    fun fixedCode(gameCode: String, cheatName: String, code: String): String? {
        val normalized = normalize(code)
        return fixesFor(gameCode).firstOrNull { it.cheatName == cheatName && it.brokenCode == normalized }?.fixedCode
    }
}
