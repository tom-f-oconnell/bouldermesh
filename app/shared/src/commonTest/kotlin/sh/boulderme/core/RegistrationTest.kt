package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import sh.boulderme.core.TestWorld.near

class RegistrationTest {
    private val truth = Similarity(1.25, Quat.yaw(0.6), Vec3(3.0, 0.2, -4.0))

    @Test fun guideMesh() {
        val g = TestWorld.guide()
        assertEquals(TestWorld.NX * TestWorld.NY * 2, g.faces)
        assertTrue(near(assertNotNull(g.raycast(Vec3(0.3, 1.0, 5.0), Vec3(0.0, 0.0, -1.0))), 5.0, 1e-9))
        assertNull(g.raycast(Vec3(9.0, 1.0, 5.0), Vec3(0.0, 0.0, -1.0)), "past its side")
        assertTrue(g.clear(Vec3(0.0, 1.0, 5.0), Vec3(0.0, 1.0, 0.0), 0.05), "a spot on it is seen from in front")
        assertTrue(!g.clear(Vec3(0.0, 1.0, 5.0), Vec3(0.0, 1.0, -2.0), 0.05), "but not what's behind it")
        assertTrue(near(g.normal(0), Vec3(0.0, 0.0, 1.0), 1e-9))
        val (f, d) = assertNotNull(g.Nearest(0.3).of(Vec3(0.31, 1.02, 0.1), 0.5))
        assertTrue(d < 0.2 && g.centre(f).distance(Vec3(0.31, 1.02, 0.0)) < 0.2)
        assertNull(g.Nearest(0.3).of(Vec3(0.0, 1.0, 3.0), 0.5))
        assertFailsWith<IllegalArgumentException> { GuideMesh.parse(ByteArray(40)) }
        val whole = GuideMesh.bytes(floatArrayOf(0f, 0f, 0f, 1f, 0f, 0f, 0f, 1f, 0f), intArrayOf(0, 1, 2), intArrayOf(GuideMesh.NO_PATCH), floatArrayOf(0f), intArrayOf(0), 0)
        assertFailsWith<IllegalArgumentException> { GuideMesh.parse(whole.copyOf(whole.size - 3)) }
        assertEquals(GuideMesh.NO_PATCH, GuideMesh.parse(whole).patch[0])
    }

    @Test fun twoTaps() {
        val a = Vec3(-1.0, 0.5, 0.0); val b = Vec3(1.2, 2.0, 0.0)
        val fit = assertNotNull(Registration.fromTaps(listOf(a to truth.apply(a), b to truth.apply(b))))
        val p = Vec3(0.3, 1.7, 0.0)
        assertTrue(near(fit.apply(p), truth.apply(p), 1e-6), "two spots give the turn, the size and the shift")
        assertTrue(near(fit.scale, 1.25, 1e-9))
        assertNull(Registration.fromTaps(listOf(a to truth.apply(a))), "one isn't enough")
        val above = Vec3(-1.0, 2.5, 0.0)
        assertNull(Registration.fromTaps(listOf(a to truth.apply(a), above to truth.apply(above))), "one above the other can't say which way it's turned")
        val kept = assertNotNull(Registration.fromTaps(listOf(a to truth.apply(a), b to truth.apply(b)), scale = 1.0))
        assertEquals(1.0, kept.scale)
    }

    @Test fun fits() {
        val rnd = Random(3)
        val tilted = Similarity(0.8, Quat.axisAngle(Vec3(0.3, 1.0, -0.2), 1.1), Vec3(-2.0, 1.0, 0.5))
        val from = List(40) { Vec3(rnd.nextDouble(-2.0, 2.0), rnd.nextDouble(0.0, 3.0), rnd.nextDouble(-1.0, 1.0)) }
        val fit = assertNotNull(Registration.fit(from, from.map { tilted.apply(it) }))
        assertTrue(near(fit.scale, 0.8, 1e-9) && near(fit.apply(from[7]), tilted.apply(from[7]), 1e-8), "any turn at all")
        val flat = assertNotNull(Registration.fit(from, from.map { truth.apply(it) }, yawOnly = true))
        assertTrue(near(flat.apply(from[3]), truth.apply(from[3]), 1e-8))
    }

    @Test fun tightened() {
        // a lump of rock: a wall alone can slide along itself and fit as well, so says nothing of where it is
        fun lump(a: Double, b: Double): Vec3 {
            val r = 1.5 + 0.35 * sin(3 * a) * cos(2 * b) + 0.2 * cos(2 * a)
            return Vec3(r * sin(b) * cos(a), 1.6 + r * cos(b), r * sin(b) * sin(a))
        }
        val na = 36; val nb = 18
        val pos = ArrayList<Float>(); val idx = ArrayList<Int>()
        for (i in 0 until na) for (j in 0..nb) { val v = lump(2 * PI * i / na, PI * (0.12 + 0.76 * j / nb)); pos.add(v.x.toFloat()); pos.add(v.y.toFloat()); pos.add(v.z.toFloat()) }
        for (i in 0 until na) for (j in 0 until nb) {
            val a = i * (nb + 1) + j; val b = ((i + 1) % na) * (nb + 1) + j
            idx.addAll(listOf(a, b, b + 1)); idx.addAll(listOf(a, b + 1, a + 1))
        }
        val nf = idx.size / 3
        val g = GuideMesh.parse(GuideMesh.bytes(pos.toFloatArray(), idx.toIntArray(), IntArray(nf) { GuideMesh.NO_PATCH }, FloatArray(nf), IntArray(nf), 0))
        val rnd = Random(11)
        // what the phone's tracking finds: points on the side of it that's been walked past, a centimetre or so off, and
        // as many again on the ground about it
        val seen = ArrayList<Vec3>()
        repeat(600) { seen.add(truth.apply(lump(rnd.nextDouble(0.0, 1.2 * PI), PI * rnd.nextDouble(0.15, 0.8)) + Vec3(rnd.nextDouble(-0.01, 0.01), rnd.nextDouble(-0.01, 0.01), rnd.nextDouble(-0.01, 0.01)))) }
        repeat(500) { seen.add(truth.apply(Vec3(rnd.nextDouble(-4.0, 4.0), rnd.nextDouble(-0.05, 0.0), rnd.nextDouble(-4.0, 4.0)))) }
        // taps a hand's width out, and a size 6% off
        val start = Similarity(1.25 * 1.06, Quat.yaw(0.6 + 0.07), truth.shift + Vec3(0.12, -0.05, 0.1))
        val r = assertNotNull(Registration.refine(start, seen, g, g.Nearest(0.25)))
        val corners = listOf(lump(0.3 * PI, 0.5 * PI), lump(0.9 * PI, 0.3 * PI), lump(0.6 * PI, 0.7 * PI))      // on the side that was seen
        val before = corners.maxOf { start.apply(it).distance(truth.apply(it)) }
        val after = corners.maxOf { r.fit.apply(it).distance(truth.apply(it)) }
        assertTrue(before > 0.2 && after < 0.06 && after < before / 4, "from $before m off to $after m")
        assertTrue(near(r.fit.scale, 1.25, 0.02) && r.rmsM < 0.03 && r.matched >= 200, "$r")
        assertNull(Registration.refine(start, seen.take(10), g, g.Nearest(0.25)), "too few points to go by")
    }
}
