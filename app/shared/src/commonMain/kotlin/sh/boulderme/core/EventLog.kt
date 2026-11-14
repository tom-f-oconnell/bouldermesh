package sh.boulderme.core

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject

/**
 * How a capture went, as it went: a line for each thing that changed (where it is, what it asks for, tracking lost and
 * found, the fit, each photo, errors the apps caught), and now and then how fast frames are coming. Not a line a
 * frame. Kept beside the capture as JSON lines (`capture-log.jsonl` on the server), each {t: seconds since the
 * capture began, e: what, ...}, and sent with it, for working out afterwards what happened at the rock.
 *
 * Not safe to use from two threads at once: it's used through CaptureFlow, which the apps keep a lock round.
 */
class EventLog {
    private var started: Double? = null     // the first line's time
    private val lines = ArrayList<String>()
    private var dropped = 0
    private var lastT = 0.0

    /** Adds a line at time t (seconds, the flow's clock): what happened, and what else is worth knowing of it. */
    fun add(t: Double, e: String, vararg fields: Pair<String, Any?>) {
        lastT = t
        val from = started ?: if (t > 0) t.also { started = it } else t     // the first frame's time: what came before it is at 0
        if (lines.size >= MOST) { dropped++; return }
        lines.add(buildJsonObject {
            put("t", JsonPrimitive(kotlin.math.round((t - from) * 100) / 100))
            put("e", JsonPrimitive(e))
            for ((k, v) in fields) if (v != null) put(k, value(v))
        }.toString())
    }

    private fun value(v: Any): JsonElement = when (v) {
        is Double -> JsonPrimitive(if (v.isFinite()) kotlin.math.round(v * 1000) / 1000 else null)
        is Number -> JsonPrimitive(v)
        is Boolean -> JsonPrimitive(v)
        else -> JsonPrimitive(v.toString().take(400))
    }

    val size: Int get() = lines.size

    /** All of it, a line each, with a last line saying how many were left out if it filled up. */
    fun text(): String = buildString {
        for (l in lines) append(l).append('\n')
        if (dropped > 0) append("""{"t":${kotlin.math.round((lastT - (started ?: lastT)) * 100) / 100},"e":"dropped","n":$dropped}""").append('\n')
    }

    companion object {
        const val MOST = 20000      // lines kept: a long capture's worth several times over
    }
}
