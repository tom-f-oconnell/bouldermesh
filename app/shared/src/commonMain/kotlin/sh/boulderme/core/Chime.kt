package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.exp
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * The capture's two sounds, made here so that both apps play the same: a soft bell when a photo is kept, and a short
 * low tick, sounded over and over while a photo from where the phone is would add nothing (FlowOut.tick). Each as 16-bit
 * mono samples at RATE, or as a WAV file of them.
 */
object Chime {
    const val RATE = 44100

    /** Two partials a fifth apart, dying away over about a third of a second, at half loudness. */
    val ding: ShortArray by lazy { tone(listOf(1318.5 to 1.0, 1975.5 to 0.45), 0.4, 0.09, 0.5) }
    /** A woody knock, a twentieth of a second long, quieter than the bell. */
    val tick: ShortArray by lazy { tone(listOf(660.0 to 1.0, 1320.0 to 0.3), 0.05, 0.012, 0.3) }

    private fun tone(partials: List<Pair<Double, Double>>, seconds: Double, decayS: Double, loud: Double): ShortArray {
        val n = (seconds * RATE).roundToInt()
        val sum = partials.sumOf { it.second }
        return ShortArray(n) { i ->
            val t = i.toDouble() / RATE
            val attack = minOf(1.0, t / 0.003)       // no click at the start
            val v = partials.sumOf { (f, a) -> a * sin(2 * PI * f * t) } / sum * attack * exp(-t / decayS) * loud
            (v * Short.MAX_VALUE).roundToInt().coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt()).toShort()
        }
    }

    /** The samples as a WAV file, for a player that takes one. */
    fun wav(samples: ShortArray): ByteArray {
        val data = samples.size * 2
        val out = ByteArray(44 + data)
        var o = 0
        fun str(s: String) { for (c in s) out[o++] = c.code.toByte() }
        fun u32(v: Int) { for (k in 0 until 4) out[o++] = (v shr (8 * k)).toByte() }
        fun u16(v: Int) { for (k in 0 until 2) out[o++] = (v shr (8 * k)).toByte() }
        str("RIFF"); u32(36 + data); str("WAVE")
        str("fmt "); u32(16); u16(1); u16(1); u32(RATE); u32(RATE * 2); u16(2); u16(16)
        str("data"); u32(data)
        for (s in samples) u16(s.toInt())
        return out
    }
}
