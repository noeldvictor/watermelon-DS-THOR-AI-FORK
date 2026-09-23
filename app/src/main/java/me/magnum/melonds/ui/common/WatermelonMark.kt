package me.magnum.melonds.ui.common

import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import me.magnum.melonds.R

/**
 * The Watermelon Thor mark: the launcher icon's watermelon slice and bolt, without its
 * background.
 */
@Composable
fun WatermelonMark(
    modifier: Modifier = Modifier,
    height: Dp = 24.dp,
) {
    Image(
        painter = painterResource(R.drawable.ic_brand_mark),
        contentDescription = null,
        modifier = modifier.size(height),
    )
}
