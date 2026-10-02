package me.magnum.melonds.impl

import android.content.Context

/**
 * Our changes to the bundled cheat database (assets/cheats/code_fixes.txt), found with tools/re:
 * - fixes: a broken code is replaced only while it is exactly the broken one, so a code the user
 *   edited is left alone;
 * - additions: codes the database lacks, added once per game: never a second cheat of that name,
 *   and never again after the user deletes it (RoomCheatsRepository remembers what it added).
 * Applied when a game is read in from the bundled database, and to games read in before.
 */
class CheatCodeFixes(private val fixes: List<Fix>, private val additions: List<Addition> = emptyList()) {

    data class Fix(val gameCode: String, val cheatName: String, val brokenCode: String, val fixedCode: String)

    data class Addition(val gameCode: String, val folderName: String, val cheatName: String, val code: String, val description: String)

    companion object {
        private const val ASSET_PATH = "cheats/code_fixes.txt"

        fun load(context: Context): CheatCodeFixes {
            return runCatching {
                context.assets.open(ASSET_PATH).bufferedReader().useLines { parseAll(it.toList()) }
            }.getOrDefault(CheatCodeFixes(emptyList()))
        }

        fun parseAll(lines: List<String>): CheatCodeFixes = CheatCodeFixes(parse(lines.asSequence()), parseAdditions(lines.asSequence()))

        private fun records(lines: Sequence<String>): Sequence<String> = lines
            .map { it.trim() }
            .filter { it.isNotEmpty() && !it.startsWith("#") }

        /** Fix lines: game code | cheat name | broken code | fixed code | why */
        fun parse(lines: Sequence<String>): List<Fix> = records(lines)
            .filter { !it.startsWith("+") }
            .mapNotNull { line ->
                val fields = line.split('|')
                if (fields.size < 4) return@mapNotNull null
                Fix(fields[0].trim().uppercase(), fields[1].trim(), normalize(fields[2]), normalize(fields[3]))
            }
            .toList()

        /** Addition lines: +game code | folder | cheat name | code | why (shown as the description) */
        fun parseAdditions(lines: Sequence<String>): List<Addition> = records(lines)
            .filter { it.startsWith("+") }
            .mapNotNull { line ->
                val fields = line.removePrefix("+").split('|')
                if (fields.size < 4) return@mapNotNull null
                Addition(fields[0].trim().uppercase(), fields[1].trim(), fields[2].trim(), normalize(fields[3]), fields.getOrNull(4)?.trim().orEmpty())
            }
            .toList()

        /** Words separated by single spaces, upper case: the stored form, and the one compared. */
        fun normalize(code: String): String = code.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ").uppercase()
    }

    fun isEmpty(): Boolean = fixes.isEmpty() && additions.isEmpty()

    fun fixesFor(gameCode: String): List<Fix> = fixes.filter { it.gameCode == gameCode.uppercase() }

    fun additionsFor(gameCode: String): List<Addition> = additions.filter { it.gameCode == gameCode.uppercase() }

    /** The fixed code for this cheat, or null when it needs none. */
    fun fixedCode(gameCode: String, cheatName: String, code: String): String? {
        val normalized = normalize(code)
        return fixesFor(gameCode).firstOrNull { it.cheatName == cheatName && it.brokenCode == normalized }?.fixedCode
    }
}
