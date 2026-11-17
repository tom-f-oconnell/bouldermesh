package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.sin
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class ImuClockTest {
    /** A phone turning slowly on the spot, wobbling as a hand does; `still` without either. */
    private fun turned(t: Double, still: Boolean = false): Quat = if (still) Quat.axisAngle(Vec3(1.0, 0.0, 0.0), 0.1) else
        Quat.yaw(0.5 * t) * Quat.axisAngle(Vec3(1.0, 0.0, 0.0), 0.08 * sin(2 * PI * 1.3 * t)) * Quat.axisAngle(Vec3(0.0, 0.0, 1.0), 0.05 * sin(2 * PI * 2.1 * t + 1))

    private fun rate(t: Double, still: Boolean): Vec3 {
        val h = 1e-4
        val d = (turned(t, still).conj() * turned(t + h, still)).unit()
        val angle = 2 * acos(d.w.coerceIn(-1.0, 1.0))
        return if (angle < 1e-12) Vec3.ZERO else Vec3(d.x, d.y, d.z).unit() * (angle / h)
    }

    /** What's read of gravity, in the phone's axes. */
    private fun gravity(t: Double, still: Boolean = false) = turned(t, still).conj().rotate(Vec3(0.0, Sensors.G, 0.0))

    /**
     * Readings at 200 a second on the sensors' clock, which reads `offset` more than the camera's; frames at 30 a second
     * reaching the app 40 ms after they were taken. Returns the clock, and the worst error of a reading at a frame's
     * moment, in degrees: lined up, and as the newest reading when the frame arrives.
     */
    private fun run(offset: Double, seconds: Double, still: Boolean = false): Triple<ImuClock, Double, Double> {
        val rnd = Random(3)
        val clock = ImuClock()
        var sensorT = 0.0
        var worstLined = 0.0; var worstNewest = 0.0
        var frameT = 0.1
        val estimates = ArrayList<ImuClock.Estimate>()
        while (frameT < seconds) {
            val arrives = frameT + 0.04
            while (sensorT - offset <= arrives) {
                val at = sensorT - offset       // the camera clock's time for this reading
                clock.gyro(sensorT, rate(at, still) + Vec3(rnd.nextDouble(-0.01, 0.01), rnd.nextDouble(-0.01, 0.01), rnd.nextDouble(-0.01, 0.01)))
                clock.accel(sensorT, gravity(at, still))
                sensorT += 0.005
            }
            clock.frame(frameT, turned(frameT, still))?.let { estimates.add(it) }
            if (frameT > seconds - 1) {
                val truth = gravity(frameT, still)
                worstLined = maxOf(worstLined, angleDeg(assertNotNull(clock.accelAt(frameT)), truth))
                worstNewest = maxOf(worstNewest, angleDeg(gravity(arrives, still), truth))
            }
            frameT += 1.0 / 30 + rnd.nextDouble(-0.002, 0.002)
        }
        return Triple(clock, worstLined, worstNewest)
    }

    @Test fun findsHowFarApartTheClocksAre() {
        for (offset in listOf(0.0, 0.042, -0.12)) {
            val (clock, lined, newest) = run(offset, 12.0)
            assertTrue(abs(clock.offsetS - offset) < 0.003, "offset ${clock.offsetS}, truly $offset")
            assertTrue(lined < 0.05 && newest > 0.2, "a reading at a frame's moment ${lined}° off; the newest one ${newest}°")
        }
    }

    @Test fun stillSaysNothing() {
        // held still, the speeds don't vary: no answer is believed, and the clocks are taken as the same
        val (clock, lined, _) = run(0.05, 8.0, still = true)
        assertEquals(0.0, clock.offsetS)
        assertTrue(lined < 0.01)
    }
}
