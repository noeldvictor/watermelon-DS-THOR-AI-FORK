package me.magnum.melonds.ui.romlist.composables

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import me.magnum.melonds.domain.model.RomFeatures
import me.magnum.melonds.ui.theme.WatermelonColors
import me.magnum.melonds.ui.theme.WatermelonMono
import me.magnum.melonds.ui.theme.watermelon

/**
 * HD / ENH / CHT chips: an installed HD pack, enhancement codes (widescreen, anti-aliasing, draw
 * distance) and cheats. [overArt] draws them for the grid card's cover art instead of a list row.
 */
@Composable
fun RomFeatureBadges(
    features: RomFeatures?,
    overArt: Boolean,
    modifier: Modifier = Modifier,
) {
    if (features == null || features == RomFeatures.NONE) {
        return
    }

    val colors = watermelon
    val hdColor = when {
        overArt || colors.isDark -> WatermelonColors.thorGold
        else -> WatermelonColors.thorGoldOnLight
    }
    val enhancedColor = if (overArt) Color(0xFF8BD66A) else colors.green
    val cheatsColor = if (overArt) Color.White.copy(alpha = 0.85f) else colors.text2

    Row(
        modifier = modifier,
        horizontalArrangement = Arrangement.spacedBy(4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (features.hdTextures) {
            FeatureChip("HD", hdColor, overArt)
        }
        if (features.enhanced) {
            FeatureChip("ENH", enhancedColor, overArt)
        }
        if (features.cheats) {
            FeatureChip("CHT", cheatsColor, overArt)
        }
    }
}

@Composable
private fun FeatureChip(text: String, textColor: Color, overArt: Boolean) {
    val colors = watermelon
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(4.dp))
            .background(if (overArt) Color.Black.copy(alpha = 0.45f) else colors.surface2)
            .padding(horizontal = 5.dp, vertical = 2.dp),
    ) {
        Text(
            text = text,
            color = textColor,
            fontFamily = WatermelonMono,
            fontSize = 8.sp,
            lineHeight = 9.sp,
            fontWeight = FontWeight.SemiBold,
            letterSpacing = 0.5.sp,
        )
    }
}
