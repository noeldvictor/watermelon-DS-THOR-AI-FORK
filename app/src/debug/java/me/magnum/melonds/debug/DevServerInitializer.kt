package me.magnum.melonds.debug

import android.content.Context
import androidx.startup.Initializer

/**
 * Hooks [DevServer] into the app at startup (debug manifest only): follows the settings toggle,
 * starts the server when it was left on, and watches activities for `--ez devserver true`.
 */
internal class DevServerInitializer : Initializer<Unit> {
    override fun create(context: Context) {
        DevServer.install(context)
    }

    override fun dependencies(): List<Class<out Initializer<*>>> = emptyList()
}
