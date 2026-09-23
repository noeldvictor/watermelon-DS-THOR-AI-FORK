package me.magnum.melondualds

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.preference.PreferenceManager
import me.magnum.melonds.common.retroachievements.RetroAchievementsEndpointStorage

class RetroAchievementsHostOverrideReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val preferences = PreferenceManager.getDefaultSharedPreferences(context.applicationContext)
        when (intent.action) {
            actionSet(context), LEGACY_ACTION_SET -> {
                val result = RetroAchievementsEndpointStorage.activateExternal(
                    preferences,
                    intent.getStringExtra(EXTRA_HOST),
                )
                result.onSuccess {
                    RetroAchievementsEndpointStorage.logSnapshot(it, "external_set")
                }.onFailure {
                    Log.w(TAG, "Rejected RAOfflineProxy host: ${it.message}")
                }
            }
            actionClear(context), LEGACY_ACTION_CLEAR -> {
                val snapshot = RetroAchievementsEndpointStorage.clearExternal(preferences)
                RetroAchievementsEndpointStorage.logSnapshot(snapshot, "external_clear")
            }
            else -> Log.w(TAG, "Ignored unsupported action")
        }
    }

    companion object {
        private const val ACTION_SET_SUFFIX = ".action.SET_RETROACHIEVEMENTS_HOST_OVERRIDE"
        private const val ACTION_CLEAR_SUFFIX = ".action.CLEAR_RETROACHIEVEMENTS_HOST_OVERRIDE"

        // RAOfflineProxy was written against WatermelonDS and sends these fixed names (built on
        // its application ID, me.magnum.melondualds). Still accepted next to the ones built on
        // this app's own ID.
        const val LEGACY_ACTION_SET = "me.magnum.melondualds$ACTION_SET_SUFFIX"
        const val LEGACY_ACTION_CLEAR = "me.magnum.melondualds$ACTION_CLEAR_SUFFIX"

        fun actionSet(context: Context) = "${context.packageName}$ACTION_SET_SUFFIX"
        fun actionClear(context: Context) = "${context.packageName}$ACTION_CLEAR_SUFFIX"

        const val EXTRA_HOST = "host"
        private const val TAG = "RAHostOverrideReceiver"
    }
}
