package me.magnum.melonds.domain.model

data class InputConfig(
    val input: Input,
    val assignment: Assignment = Assignment.None,
    val altAssignment: Assignment = Assignment.None,
) {

    sealed class Assignment(open val deviceId: Int?) {
        data object None : Assignment(null)
        /**
         * [modifierKeyCode] turns the binding into a combo: the input only
         * fires when that key is already held. Null keeps the plain single
         * key behaviour, so existing configurations are unaffected.
         */
        data class Key(
            override val deviceId: Int?,
            val keyCode: Int,
            val modifierKeyCode: Int? = null,
        ) : Assignment(deviceId)
        data class Axis(override val deviceId: Int?, val axisCode: Int, val direction: Direction) : Assignment(deviceId) {
            enum class Direction {
                POSITIVE,
                NEGATIVE,
            }
        }
    }

    fun hasKeyAssigned(): Boolean {
        return assignment != Assignment.None || altAssignment != Assignment.None
    }
}