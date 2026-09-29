package me.magnum.melonds.ui.emulator.ui

import androidx.activity.compose.BackHandler
import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import me.magnum.melonds.R
import me.magnum.melonds.domain.model.Cheat
import me.magnum.melonds.domain.model.EnhancementCheats
import me.magnum.melonds.domain.model.WidescreenCheats

/**
 * The pause menu's Enhancements panel: one switch per enhancement code the game has (widescreen,
 * smooth 3D edges, draw distance). [recommendedWidescreenCheatId] is the widescreen shape that fits
 * the top screen's display.
 */
@Composable
fun EnhancementsOverlay(
    cheats: List<Cheat>,
    recommendedWidescreenCheatId: Long?,
    onToggle: (Cheat, Boolean) -> Unit,
    onBack: () -> Unit,
) {
    BackHandler(onBack = onBack)

    ConsoleScaffold(
        title = stringResource(R.string.enhancements),
        onBack = onBack,
    ) {
        ConsoleSectionLabel(stringResource(R.string.enhancements_hint))
        val labels = cheats.map { enhancementLabel(it) }
        cheats.forEachIndexed { index, cheat ->
            // two codes with the same friendly name ("Widescreen" and "Widescreen v2.0") keep their own
            val label = if (labels.count { it == labels[index] } > 1) cheat.name else labels[index]
            val details = listOfNotNull(
                cheat.name.takeIf { namesMore(it, label) },
                cheat.description?.takeIf { it.isNotBlank() },
                if (cheat.id != null && cheat.id == recommendedWidescreenCheatId && cheats.count { WidescreenCheats.isWidescreen(it) } > 1) {
                    stringResource(R.string.enhancement_best_fit)
                } else {
                    null
                },
            )
            ConsoleToggleRow(
                label = label,
                checked = cheat.enabled,
                onToggle = { enabled -> onToggle(cheat, enabled) },
                firstFocus = index == 0,
                description = details.joinToString(" · ").takeIf { it.isNotEmpty() },
            )
        }
    }
}

// The code's own name is shown under the label unless the label already says it ("Widescreen (16:9)"
// under "Widescreen 16:9")
private fun namesMore(cheatName: String, label: String): Boolean {
    val name = cheatName.lowercase().filter { it.isLetterOrDigit() }
    return !label.lowercase().filter { it.isLetterOrDigit() }.startsWith(name)
}

@Composable
private fun enhancementLabel(cheat: Cheat): String {
    return when (EnhancementCheats.kind(cheat.name)) {
        EnhancementCheats.Kind.WIDESCREEN -> {
            val aspectRatio = WidescreenCheats.aspectRatio(cheat) ?: WidescreenCheats.DEFAULT_ASPECT_RATIO
            stringResource(if (aspectRatio < 1.7f) R.string.enhancement_widescreen_16_10 else R.string.enhancement_widescreen_16_9)
        }
        EnhancementCheats.Kind.ANTI_ALIASING -> stringResource(R.string.enhancement_anti_aliasing)
        EnhancementCheats.Kind.DRAW_DISTANCE -> stringResource(R.string.enhancement_draw_distance)
        null -> cheat.name
    }
}
