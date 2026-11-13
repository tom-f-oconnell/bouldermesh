package sh.boulderme.core

import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertTrue

class QualityTest {
    private val w = 1200; private val h = 900

    /** Rock, more or less: blotches at several sizes, down to a pixel. */
    private fun texture(seed: Int): ByteArray {
        val rnd = Random(seed)
        val out = DoubleArray(w * h)
        for (size in intArrayOf(64, 16, 4, 1)) {
            val gw = w / size + 2; val gh = h / size + 2
            val grid = DoubleArray(gw * gh) { rnd.nextDouble(-1.0, 1.0) }
            for (y in 0 until h) for (x in 0 until w) out[y * w + x] += grid[(y / size) * gw + x / size] * 22
        }
        return ByteArray(w * h) { (128 + out[it]).coerceIn(0.0, 255.0).toInt().toByte() }
    }

    /** The picture with across and down swapped (it's then h wide; only its middle square is looked at for sharpness). */
    private fun turned(src: ByteArray): ByteArray {
        val side = h
        val out = src.copyOf()
        for (y in 0 until side) for (x in 0 until side) out[y * w + x] = src[x * w + y]
        return out
    }

    private fun blurred(src: ByteArray, r: Int): ByteArray {
        val out = ByteArray(w * h)
        for (y in 0 until h) for (x in 0 until w) {
            var sum = 0; var n = 0
            for (dx in -r..r) { val xx = x + dx; if (xx in 0 until w) { sum += src[y * w + xx].toInt() and 0xff; n++ } }
            out[y * w + x] = (sum / n).toByte()
        }
        return out
    }

    @Test fun sharpness() {
        val sharp = Quality.imageStats(texture(1), w, h)
        val other = Quality.imageStats(texture(9), w, h)
        val soft = Quality.imageStats(blurred(texture(1), 2), w, h)       // smeared 5 pixels sideways
        val softer = Quality.imageStats(blurred(texture(1), 5), w, h)
        assertTrue(sharp.sharp > 0.8 && other.sharp > 0.8 && kotlin.math.abs(sharp.sharp / other.sharp - 1) < 0.1, "sharp: ${sharp.sharp}, ${other.sharp}")
        assertTrue(soft.sharp < 0.4 * sharp.sharp && softer.sharp < 0.4 * sharp.sharp, "soft: ${soft.sharp}, ${softer.sharp}, of ${sharp.sharp}")
        val downwards = Quality.imageStats(turned(blurred(turned(texture(1)), 2)), w, h)
        assertTrue(downwards.sharp < 0.4 * sharp.sharp, "smeared down the picture, not across: ${downwards.sharp}")
        assertTrue(sharp.detail > Quality.DETAIL_LEAST && sharp.clipHigh < 0.01 && sharp.clipLow < 0.01)
        assertTrue(Quality.judge(sharp, 0.4).ok)
        assertTrue(Quality.judge(soft, 0.4, usualSharp = sharp.sharp).reasons.single().contains("soft"), "against what's usual for this capture")
        assertTrue(Quality.judge(sharp.copy(sharp = 0.07), 0.4).reasons.single().contains("soft"), "or, before that's known, against a floor")
        val blank = Quality.imageStats(ByteArray(w * h) { 200.toByte() }, w, h)
        assertTrue(blank.detail < Quality.DETAIL_LEAST && Quality.judge(blank, 0.2).ok, "nothing in it to be sharp or not: not called soft")
    }

    @Test fun exposureAndMotion() {
        val bright = Quality.imageStats(ByteArray(w * h) { if (it % 3 == 0) 255.toByte() else 120.toByte() }, w, h)
        val v = Quality.judge(bright, 0.2)
        assertTrue(bright.clipHigh > 0.2 && !v.ok && v.reasons.any { "bright" in it }, "$bright")
        val dark = Quality.imageStats(ByteArray(w * h) { if (it % 2 == 0) 2 else 90 }, w, h)
        assertTrue(Quality.judge(dark, 0.2).reasons.any { "dark" in it })
        // turning 0.2 rad/s through a 1/250 s shutter at 3000 pixels of focal length: 2.4 pixels
        assertTrue(TestWorld.near(Quality.blurPx(0.2, 0.0, 0.004, 3000.0, null), 2.4, 1e-9))
        // walking at 0.5 m/s past rock 2 m off through the same shutter: 3 pixels
        assertTrue(TestWorld.near(Quality.blurPx(0.0, 0.5, 0.004, 3000.0, 2.0), 3.0, 1e-9))
        assertTrue(!Quality.judge(null, 2.4).ok && Quality.judge(null, 0.5).ok && !Quality.judge(null, 0.5, tracking = false).ok)
    }
}
