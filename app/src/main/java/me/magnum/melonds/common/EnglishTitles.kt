package me.magnum.melonds.common

import android.content.res.AssetManager
import android.util.Log
import me.magnum.melonds.domain.model.rom.Rom

/**
 * Our curated English titles for games whose own title is Japanese (assets/titles/english_titles.tsv),
 * by game code, so one entry covers every copy of a game. A custom name set in the app wins.
 */
object EnglishTitles {
    private const val ASSET = "titles/english_titles.tsv"

    @Volatile
    private var titles: Map<String, String> = emptyMap()

    fun load(assets: AssetManager) {
        titles = runCatching {
            assets.open(ASSET).bufferedReader(Charsets.UTF_8).useLines { lines ->
                lines.filter { it.isNotBlank() && !it.startsWith("#") }
                    .mapNotNull { line ->
                        val fields = line.split('\t')
                        val code = fields.getOrNull(0)?.trim().orEmpty()
                        val title = fields.getOrNull(1)?.trim().orEmpty()
                        if (code.length == 4 && title.isNotEmpty()) code to title else null
                    }
                    .toMap()
            }
        }.onFailure {
            Log.w("EnglishTitles", "Could not read $ASSET", it)
        }.getOrDefault(emptyMap())
    }

    fun forGameCode(gameCode: String?): String? = gameCode?.let { titles[it] }

    /** The curated English title of this ROM's game, if there is one. */
    fun forRom(rom: Rom): String? = forGameCode(rom.gameCode)

    /** Whether a title has Japanese characters (kana, kanji, full-width forms). */
    fun isJapanese(title: String): Boolean = title.any { ch ->
        ch in '぀'..'ヿ' || ch in '一'..'鿿' || ch in '＀'..'￯' || ch in '㐀'..'䶿'
    }
}
