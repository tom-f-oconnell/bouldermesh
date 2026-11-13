package sh.boulderme.core

import kotlin.math.cos
import kotlin.math.sin
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import sh.boulderme.core.TestWorld.near

class CalibrationTest {
    @Test fun uprightFromDown() {
        // a phone held upright reads its picture out on its side: down runs along the rows, and a quarter turn shows it right
        assertEquals(6, Axes.exifOrientation(Axes.downFromAccel(Vec3(0.0, 9.81, 0.0)), 0))
        assertEquals(1, Axes.exifOrientation(Vec3(0.0, 1.0, 0.0), 90), "held on its side, the picture is upright as stored")
        assertEquals(3, Axes.exifOrientation(Vec3(0.1, -1.0, 0.2), 90))
        assertEquals(8, Axes.exifOrientation(Vec3(-1.0, 0.0, 0.0), 90))
        assertEquals(6, Axes.exifOrientation(Vec3(0.1, 0.1, 1.0), 90), "looking straight down: as the screen showed it")
        assertEquals(1, Axes.exifOrientation(null, 0))
    }

    /**
     * What a phone's accelerometer reads facing `yaw` round the circle and looking `pitch` up, where the tracking's world
     * has down as `down`: with the sensor's own zero error, sitting a little askew of the camera, and noise.
     */
    private fun reading(yaw: Double, pitch: Double, down: Vec3, bias: Vec3, askew: Quat, rnd: Random, extra: Vec3 = Vec3.ZERO): Pair<CameraPose, Vec3> {
        val pose = TestWorld.look(Vec3.ZERO, Vec3(sin(yaw) * cos(pitch), sin(pitch), cos(yaw) * cos(pitch)))
        val force = pose.rot.conj().rotate(-down * Sensors.G + extra)      // in the photo's axes
        val noise = Vec3(rnd.nextDouble(-0.05, 0.05), rnd.nextDouble(-0.05, 0.05), rnd.nextDouble(-0.05, 0.05))
        return pose to (askew.rotate(Axes.forceInImage(force)) + bias + noise)      // forceInImage() is its own inverse
    }

    @Test fun downByTurningOnTheSpot() {
        val rnd = Random(11)
        // the tracking's world is 0.8 degrees off level; the accelerometer reads 0.3 m/s2 off (1.8 degrees' worth) and
        // sits half a degree askew of the camera
        val down = Quat.axisAngle(Vec3(1.0, 0.0, 0.5), 0.8 * TestWorld.rad).rotate(Vec3(0.0, -1.0, 0.0))
        val bias = Vec3(0.15, -0.1, 0.25); val askew = Quat.axisAngle(Vec3(0.3, 1.0, 0.2), 0.5 * TestWorld.rad)
        val turn = LevelTurn()
        assertNull(turn.solve())
        var facingOneWay = Vec3.ZERO
        for (i in 0 until 360) {
            val yaw = i * TestWorld.rad
            // held at arm's length, the phone goes round a circle: pulled inwards a little, the more the faster it turns
            val inwards = Vec3(-sin(yaw), 0.0, -cos(yaw)) * 0.04
            val (pose, accel) = reading(yaw, (10 + 5 * sin(3 * yaw)) * TestWorld.rad, down, bias, askew, rnd, inwards)
            assertTrue(turn.add(pose, accel, gyroRadS = 0.3))
            if (i < 30) facingOneWay += pose.rot.rotate(Axes.forceInImage(accel))
        }
        assertEquals(12, turn.filled)
        val level = assertNotNull(turn.solve())
        assertTrue(angleDeg(level.down, down) < 0.1, "down to ${angleDeg(level.down, down)} degrees")
        assertTrue(near(level.tiltDeg, 0.8, 0.1) && level.spreadDeg < 0.2 && level.offsetDeg in 1.0..2.5, "$level")
        assertTrue(angleDeg(-facingOneWay.unit(), down) > 1.0, "where readings facing one way alone are ${angleDeg(-facingOneWay.unit(), down)} degrees off")
        // a photo's down, by the turn; and its accelerometer's alone, with what the turn found taken off, agrees with it
        val (pose, accel) = reading(2.0, 0.3, down, bias, askew, rnd)
        val truth = pose.rot.conj().rotate(down)
        assertTrue(angleDeg(Axes.downInImage(pose, level.down), truth) < 0.1 && angleDeg(Axes.downInImage(pose), truth) > 0.7)
        assertTrue(angleDeg(-(Axes.forceInImage(accel) - level.offset).unit(), truth) < 0.6 && angleDeg(Axes.downFromAccel(accel), truth) > 1.0)

        // three quarters of the way round is enough, if asked; a third isn't
        val part = LevelTurn()
        for (i in 0 until 270) { val (p, a) = reading(i * TestWorld.rad, 0.15, down, bias, askew, rnd); part.add(p, a) }
        assertTrue(part.filled == 9 && part.solve() == null)
        assertTrue(angleDeg(assertNotNull(part.solve(least = 9)).down, down) < 0.25)
        val little = LevelTurn()
        for (i in 0 until 120) { val (p, a) = reading(i * TestWorld.rad, 0.15, down, bias, askew, rnd); little.add(p, a) }
        assertNull(little.solve(least = 9))
        // not read: looking steeply up, turning fast, or jolted
        val (p, a) = reading(0.0, 1.2, down, bias, askew, rnd)
        assertFalse(little.add(p, a), "looking steeply up")
        val (q, c) = reading(0.0, 0.1, down, bias, askew, rnd)
        assertFalse(little.add(q, c, gyroRadS = 2.0), "turning fast")
        assertFalse(little.add(q, c * 1.2), "jolted")
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
