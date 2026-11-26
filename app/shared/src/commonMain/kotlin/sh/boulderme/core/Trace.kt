package sh.boulderme.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlin.time.TimeSource

/**
 * The site owner's timing trace of a capture (Settings' debug tools, on there unless turned off): how long each part
 * took and how it came out, on the phone and the server. Each phase, each target asked for (a hold close up, a side),
 * each frame placed against the mesh by the phone and by the server (how long each step took, and whether it placed
 * it), each photo's work, a gym climb's holds found in each photo.
 *
 * Unlike EventLog, which goes to everyone's server with the capture, it's the owner's alone, and kept so as to change the
 * timings it measures as little as can be: each line is held as given (nothing turned into text, written or sent while
 * the capture runs), written beside the capture only when it pauses or ends, and sent afterwards (the apps'
 * DebugSender, to the server's debug/capture/<id>/).
 *
 * Not safe to use from two threads at once: the apps add to it under the lock they keep round CaptureFlow.
 */
class Trace(private val clock: () -> Double = monotonic()) {
    private class Line(val t: Double, val e: String, val fields: Array<out Pair<String, Any?>>)

    private val lines = ArrayList<Line>(1024)
    private var dropped = 0

    /** Seconds since the trace began, on its own clock (not the frames'). */
    fun now(): Double = clock()

    /** Adds a line now: what happened, and what else is worth knowing of it (durations in ms, as `*_ms`). */
    fun add(e: String, vararg fields: Pair<String, Any?>) {
        if (lines.size >= MOST) { dropped++; return }
        lines.add(Line(clock(), e, fields))
    }

    val size: Int get() = lines.size

    /** All of it, a JSON line each ({t: seconds since it began, e: what, ...}), with a last line saying how many were left out if it filled up. */
    fun text(): String = buildString {
        for (l in lines) append(buildJsonObject {
            put("t", JsonPrimitive(kotlin.math.round(l.t * 1000) / 1000))
            put("e", JsonPrimitive(l.e))
            for ((k, v) in l.fields) if (v != null) put(k, value(v))
        }.toString()).append('\n')
        if (dropped > 0) append("""{"t":${kotlin.math.round(clock() * 1000) / 1000},"e":"dropped","n":$dropped}""").append('\n')
    }

    companion object {
        const val MOST = 50000      // lines kept
        /** The file it's written to, in the owner's debug folder for the capture. */
        const val FILE = "trace.jsonl"

        /** A clock in seconds from when it's made, that only goes forward. */
        fun monotonic(): () -> Double { val start = TimeSource.Monotonic.markNow(); return { start.elapsedNow().inWholeMicroseconds / 1e6 } }

        /** Milliseconds from a mark of TimeSource.Monotonic, to a tenth: for timing a step in common code. */
        fun ms(from: TimeSource.Monotonic.ValueTimeMark): Double = kotlin.math.round(from.elapsedNow().inWholeMicroseconds / 100.0) / 10

        private fun value(v: Any?): JsonElement = when (v) {
            null -> JsonNull
            is JsonElement -> v
            is Double -> if (v.isFinite()) JsonPrimitive(kotlin.math.round(v * 1000) / 1000) else JsonNull
            is Float -> value(v.toDouble())
            is Number -> JsonPrimitive(v)
            is Boolean -> JsonPrimitive(v)
            is Collection<*> -> JsonArray(v.map { value(it) })
            is Map<*, *> -> buildJsonObject { for ((k, x) in v) put(k.toString(), value(x)) }
            else -> JsonPrimitive(v.toString().take(400))
        }
    }
}
