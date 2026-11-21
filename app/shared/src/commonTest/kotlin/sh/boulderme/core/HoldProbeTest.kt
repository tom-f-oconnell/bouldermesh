package sh.boulderme.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class HoldProbeTest {
    /** A grey wall with two red holds and a blue one. */
    private fun wall(): Picture {
        val w = 120; val h = 80
        val px = IntArray(w * h) { 0x808080 }
        fun square(x0: Int, y0: Int, c: Int) { for (y in y0 until y0 + 10) for (x in x0 until x0 + 10) px[y * w + x] = c }
        square(10, 10, 0xD02020); square(60, 40, 0xD02020); square(90, 10, 0x2040D0)
        return Picture.ofRgb(px, w, h)
    }

    @Test fun aTapFindsItsColour() {
        val r = HoldProbe.run(wall(), null, Gym.FINDER_COLOUR, 15.0, 15.0, 1.0, 0f)
        assertTrue(r.colour.size >= 3, "${r.colour}")
        assertEquals(2, r.matches.size)
        assertEquals("red", r.name)
        assertNull(r.model)
        val blue = HoldProbe.run(wall(), null, Gym.FINDER_COLOUR, 95.0, 15.0, 1.0, 0f)
        assertEquals(1, blue.matches.size)
        assertNull(HoldProbe.run(wall(), null, Gym.FINDER_MODEL, 15.0, 15.0, 1.0, 0f).picked, "no model: nothing to pick from")
    }

    @Test fun theModelsMarginNarrowsItsMask() {
        val logits = FloatArray(3 * 4) { 0f }.also { it[4] = 1f; it[5] = 3f }    // pixels 0 and 1 holds, by 1 and by 3
        assertEquals(listOf(true, true, false, false), HoldModel.mask(logits, 2, 2).toList())
        assertEquals(listOf(false, true, false, false), HoldModel.mask(logits, 2, 2, 2f).toList())
    }
}
