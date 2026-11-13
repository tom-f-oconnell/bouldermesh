package sh.boulderme.core

import kotlin.math.cos
import kotlin.math.sin
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import sh.boulderme.core.TestWorld.near

class CalibrationTest {
    @Test fun accelerometer() {
        val bias = Vec3(0.21, -0.14, 0.3)
        // a table sloping 4 degrees: gravity as the phone reads it lying on it, after half a turn, and flipped over
        val g = Sensors.G; val t = 4 * TestWorld.rad
        val c = AccelCalibration()
        assertNull(c.bias())
        c.flat = Vec3(g * sin(t), 0.0, g * cos(t)) + bias
        c.turned = Vec3(-g * sin(t), 0.0, g * cos(t)) + bias
        c.flipped = Vec3(g * sin(t), 0.0, -g * cos(t)) + bias
        assertTrue(near(assertNotNull(c.bias()), bias, 1e-9), "the slope cancels")
        assertTrue(assertNotNull(c.gainDeg()) in 1.5..2.5)
        c.flipped = c.flat
        assertNull(c.bias(), "not flipped")
        val still = StillMean()
        repeat(20) { still.add(Vec3(0.01 * (it % 3), 0.0, g)) }
        assertTrue(still.still() && near(assertNotNull(still.mean()).z, g, 1e-9))
        still.add(Vec3(2.0, 0.0, g))
        assertTrue(!still.still(), "moved")
    }

    @Test fun magnetometer() {
        val rnd = Random(5)
        val offset = Vec3(12.0, -30.0, 7.0)
        val m = MagCalibration()
        assertNull(m.solve())
        repeat(200) {
            val d = Vec3(rnd.nextDouble(-1.0, 1.0), rnd.nextDouble(-1.0, 1.0), rnd.nextDouble(-1.0, 1.0)).unit()
            m.add(offset + d * 47.0 + Vec3(rnd.nextDouble(-0.3, 0.3), rnd.nextDouble(-0.3, 0.3), rnd.nextDouble(-0.3, 0.3)))
        }
        val r = assertNotNull(m.solve())
        assertTrue(near(r.offset, offset, 0.5) && near(r.fieldUt, 47.0, 0.5) && r.rmsUt < 0.5 && r.covered > 0.9, "$r")
        val half = MagCalibration()
        repeat(100) { half.add(offset + Vec3(cos(it * 0.03), sin(it * 0.03), 0.0) * 47.0) }
        assertTrue(half.covered() < 0.4, "turned about one axis only: ${half.covered()}")
    }

    @Test fun gps() {
        val a = GpsAverager()
        assertNull(a.result())
        val rnd = Random(2)
        repeat(60) { a.add(GpsFix(34.01 + rnd.nextDouble(-2e-5, 2e-5), -116.16 + rnd.nextDouble(-2e-5, 2e-5), 1200.0, 4.0, it.toDouble())) }
        a.add(GpsFix(34.012, -116.16, 1200.0, 4.0, 60.0))      // 220 m off
        a.add(GpsFix(34.5, -116.16, null, 500.0, 61.0))        // and one that says it's no good
        val r = assertNotNull(a.result())
        assertEquals(60, r.n)
        assertTrue(GpsAverager.metresApart(r.lat, r.lng, 34.01, -116.16) < 1.0 && r.accM < 2.0 && r.seconds >= 59.0, "$r")
        assertTrue(near(GpsAverager.metresApart(34.0, -116.0, 34.001, -116.0), 111.3, 0.3))
    }

    @Test fun sensorsGraded() {
        val good = SensorSample(Vec3(0.0, 9.8, 0.3), Vec3(0.01, 0.0, 0.0), Vec3(20.0, 5.0, -40.0), 3, 120.0, 8.0, GpsFix(34.0, -116.0, null, 3.0, 9.5), 10.0)
        val g = Sensors.grade(good, 45.0)
        assertTrue(g.gravityOk && g.headingOk && g.gpsOk && g.notes.isEmpty(), "$g")
        val moving = Sensors.grade(good.copy(accel = Vec3(0.0, 11.5, 0.0)))
        assertTrue(!moving.gravityOk && moving.notes.any { "moving" in it })
        assertTrue(!Sensors.grade(good.copy(mag = Vec3(150.0, 0.0, 0.0))).headingOk, "a magnet")
        assertTrue(!Sensors.grade(good, usualFieldUt = 60.0).headingOk, "not this capture's usual field")
        assertTrue(!Sensors.grade(good.copy(magAccuracy = 1)).headingOk)
        val stale = Sensors.grade(good.copy(gps = GpsFix(34.0, -116.0, null, 3.0, 2.0)))
        assertTrue(!stale.gpsOk && stale.notes.any { "old" in it })
        assertTrue(!Sensors.grade(good.copy(gps = null)).gpsOk)
    }
}
