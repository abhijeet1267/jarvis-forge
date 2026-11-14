package com.vantage.companion.voice

import com.vantage.companion.data.api.CommandResponse

/**
 * The whole "should this reply be read aloud?" decision, with no Android
 * types in it — unit-testable on the JVM.
 *
 * A reply is spoken only when all of these hold:
 *  - the user turned AI Voice ON,
 *  - the engine actually reached [VoiceAvailability.READY],
 *  - the response is a normal assistant answer: it is not blocked, is not a
 *    confirmation request (the user must read and approve those, and a
 *    spoken prompt would invite an unsafe, hands-free "yes"), and it carries
 *    a non-blank `reply`.
 *
 * Errors are represented by `null` (the ViewModel only has a
 * [CommandResponse] on a completed `/command` call) and are never spoken.
 */
object VoiceSpeechPolicy {

    fun utteranceFor(
        enabled: Boolean,
        availability: VoiceAvailability,
        response: CommandResponse?,
    ): String? {
        if (!enabled) return null
        if (availability != VoiceAvailability.READY) return null
        val answer = response ?: return null
        if (answer.isBlocked) return null
        if (answer.confirmationRequired) return null
        return answer.reply?.trim()?.takeIf { it.isNotEmpty() }
    }
}