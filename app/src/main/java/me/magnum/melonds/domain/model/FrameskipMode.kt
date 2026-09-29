package me.magnum.melonds.domain.model

/**
 * Frameskip for the Vulkan renderer: a skipped frame still runs but isn't composed or presented.
 * [nativeValue] is what the emulator thread's frame limiter reads.
 */
enum class FrameskipMode(val preferenceValue: String, val nativeValue: Int) {
    OFF("off", 0),
    MANUAL("manual", 1),
    AUTO("auto", 2);

    companion object {
        const val MANUAL_VALUE_MIN = 1
        const val MANUAL_VALUE_MAX = 4

        fun fromPreferenceValue(value: String?): FrameskipMode {
            return entries.firstOrNull { it.preferenceValue == value } ?: OFF
        }
    }
}
