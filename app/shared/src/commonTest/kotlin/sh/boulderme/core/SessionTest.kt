package sh.boulderme.core

import kotlin.math.cos
import kotlin.math.sin
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import sh.boulderme.core.TestWorld.near

class SessionTest {
    private val truth = Similarity(1.25, Quat.yaw(0.6), Vec3(3.0, 0.2, -4.0))
    private val stats = ImageStats(1.0, 8.0, 0.0, 0.0, 120.0)

    private fun session(): CaptureSession {
        val s = CaptureSession(TestWorld.plan(), TestWorld.guide(), "t1", "test 0.1", "a phone", 100.0)
        val a = Vec3(-1.0, 0.5, 0.0); val b = Vec3(1.2, 2.0, 0.0)
        assertNull(s.tap(a, truth.apply(a)))
        assertNotNull(s.tap(b, truth.apply(b)))
        return s
    }

    private fun sensors(t: Double) = SensorSample(Vec3(0.0, 9.81, 0.0), Vec3.ZERO, Vec3(20.0, 5.0, -40.0), 3, 120.0, 5.0, GpsFix(34.01, -116.16, 1200.0, 3.0, t - 0.5), t)

    private fun take(s: CaptureSession, pose: CameraPose, t: Double, stats: ImageStats? = this.stats, gyro: Double = 0.0) =
        s.shot(ShotIn(pose, sensors(t), stats, 0.004, 80.0, 2.0, true, "ar1", t, 1.7e9 + t, gyroRadS = gyro))

    @Test fun coverageOfAPlan() {
        val cov = Coverage(TestWorld.plan(), TestWorld.guide())
        val hold = assertNotNull(cov.state("h0"))
        assertTrue(near(assertNotNull(cov.needGsdM(hold)), 0.0005, 1e-9) && !cov.done(hold) && cov.goodViews(hold) == 0, "2.5 mm a pixel so far: none of its views count")
        // from 4 units (5 m) off: seen, at 5 m / 3000 = 1.7 mm a pixel: too coarse to count for a hold, fine for the rock
        val far = TestWorld.look(Vec3(-0.5, 1.0, 4.0), Vec3(-0.5, 1.0, 0.0))
        assertTrue(near(assertNotNull(cov.see(far, hold)).gsdM, 5.0 / 3000, 1e-6))
        assertNull(cov.see(TestWorld.look(Vec3(-0.5, 1.0, -4.0), Vec3(-0.5, 1.0, 0.0)), hold), "from behind")
        assertNull(cov.see(TestWorld.look(Vec3(-0.5, 1.0, 4.0), Vec3(9.0, 1.0, 0.0)), hold), "out of frame")
        assertEquals(Hint.Kind.CLOSER, cov.hint(far, hold).kind)
        assertTrue(cov.addShot(far).any { it.startsWith("p") } && "h0" !in cov.addShot(far).filter { cov.goodViews(hold) > 0 })
        // close: 1.1 units (1.4 m) off is 0.46 mm a pixel
        val close = TestWorld.look(Vec3(-0.5, 1.0, 1.1), Vec3(-0.5, 1.0, 0.0))
        assertTrue(cov.gain(close) > 0 && "h0" in cov.addShot(close) && cov.goodViews(hold) == 1)
        assertEquals(Hint.Kind.ANGLE, cov.hint(close, hold).kind, "the same direction again adds nothing")
        assertEquals(0, cov.addShot(close).count { it == "h0" })
        for (k in listOf(-2, -1, 1, 2)) {
            val a = k * 20 * TestWorld.rad
            cov.addShot(TestWorld.look(Vec3(-0.5 + 1.1 * sin(a), 1.0, 1.1 * cos(a)), Vec3(-0.5, 1.0, 0.0)))
        }
        assertTrue(cov.done(hold) && cov.level(hold) == Coverage.LEVEL_DONE, "four directions, finely enough")
        val p = cov.progress()
        assertTrue(p.total == 9 && p.holds == 2 && p.holdsDone == 1 && !p.finished, "$p")      // 6 patches on the climb, 2 holds, the edge
        cov.scope = Scope(minPrio = 1)
        assertEquals(15, cov.progress().total)
        cov.scope = Scope(climbs = setOf("nope"))
        assertEquals(0, cov.progress().total)
        // a hold 6 m up can't be had at half a millimetre from the ground: what a phone held up can do is asked instead
        val high = Coverage(TestWorld.plan().let { it.copy(targets = it.targets + Target("h9", "hold", listOf(0.0, 4.8, 0.0), listOf(0.0, 0.0, 1.0), 0.1, prio = 3)) }, null)
        assertTrue(assertNotNull(high.needGsdM(assertNotNull(high.state("h9")))) > 0.001)
        assertEquals(12, cov.patchLevels().size)
    }

    /** Someone doing what the hints say, until it's done. */
    @Test fun followingTheHints() {
        val s = session()
        val cov = assertNotNull(s.coverage)
        var at = truth.apply(Vec3(0.0, 1.5, 4.0))
        var aim = truth.apply(Vec3(0.0, 1.5, 0.0))
        var t = 0.0
        var steps = 0
        val kinds = HashSet<Hint.Kind>()
        var out = s.frame(FrameIn(TestWorld.look(at, aim), 0.0, 0.004, true, t))
        while (!out.progress.finished && steps < 400) {
            steps++; t += 1.0
            val pose = TestWorld.look(at, aim)
            out = s.frame(FrameIn(pose, 0.0, 0.004, true, t))
            kinds.add(out.hint.kind)
            if (out.shoot) { assertTrue(take(s, pose, t).kept); continue }
            val target = out.hint.targetId?.let { cov.state(it) } ?: break
            val p = truth.apply(target.target.point); val n = truth.dir(target.target.normal)
            val side = ((aim - at).unit() cross Vec3.UP).unit()
            when (out.hint.kind) {
                Hint.Kind.TURN -> aim = p
                Hint.Kind.CLOSER -> { aim = p; at += (p - at).unit() * (out.hint.moveM + 0.05) }
                Hint.Kind.BACK -> at -= (p - at).unit() * 0.2
                Hint.Kind.FACE -> { at = p + n * at.distance(p); aim = p }
                Hint.Kind.ANGLE, Hint.Kind.EDGE -> { at += side * (if (out.hint.kind == Hint.Kind.EDGE) 0.6 else out.hint.moveM); aim = p }
                Hint.Kind.SHOOT -> {}       // standing still, until the phone has settled
                Hint.Kind.NONE -> { at += side * 0.3; aim = p }
                Hint.Kind.DONE -> break
            }
        }
        val p = s.coverage!!.progress()
        assertTrue(p.finished && p.holdsDone == 2, "done in $steps steps: $p; hints $kinds")
        assertTrue(s.shotCount in 8..60, "${s.shotCount} photos")
        assertTrue(Hint.Kind.CLOSER in kinds && Hint.Kind.ANGLE in kinds, "it asked to come closer and for other angles: $kinds")
        assertEquals(Hint.Kind.DONE, s.frame(FrameIn(TestWorld.look(at, aim), 0.0, 0.004, true, t + 1)).hint.kind)
        val rec = s.record(900.0)
        assertTrue(rec.mode == "plan" && rec.mesh == "abc" && rec.shots.size == s.shotCount && rec.progress!!.done == p.done)
        assertTrue(near(rec.fit!!.scale, 1.25, 1e-6) && rec.fit!!.taps == 2 && rec.json().contains("\"tilt_deg\""))
        assertTrue(s.markers().count { it.kind == "hold" && it.level == Coverage.LEVEL_DONE } == 2)
    }

    @Test fun photosJudged() {
        val s = session()
        val pose = TestWorld.look(truth.apply(Vec3(-0.5, 1.0, 1.1)), truth.apply(Vec3(-0.5, 1.0, 0.0)))
        val unlined = CaptureSession(TestWorld.plan(), TestWorld.guide(), "t2", "test", "phone", 0.0)
        assertTrue("Line the mesh up" in unlined.frame(FrameIn(pose, 0.0, 0.004, true, 0.0)).hint.text)
        assertTrue(!s.frame(FrameIn(pose, 0.0, 0.004, false, 0.0)).shoot, "not while it's lost")
        assertTrue(s.frame(FrameIn(pose, 0.0, 0.004, true, 1.0)).shoot)
        assertTrue(!s.frame(FrameIn(pose, 0.5, 0.004, true, 2.0)).shoot, "nor while it's turning: 6 pixels of blur")
        val moved = TestWorld.look(pose.centre + Vec3(0.5, 0.0, 0.0), truth.apply(Vec3(-0.5, 1.0, 0.0)))
        assertTrue(!s.frame(FrameIn(moved, 0.0, 0.004, true, 2.1)).shoot, "nor while it's moving: half a metre in a tenth of a second")
        s.frame(FrameIn(pose, 0.0, 0.004, true, 2.5)); s.frame(FrameIn(pose, 0.0, 0.004, true, 2.9))      // back, and still
        val good = take(s, pose, 3.0)
        assertTrue(good.kept && "h0" in good.helped && good.userComment.startsWith("BMSH1:{") && good.record.name == "cap_t1_001.jpg")
        for (key in listOf("\"down\":[", "\"down_src\":\"ar\"", "\"ar\":{\"s\":\"ar1\",\"p\":[", "\"gps\":{\"lat\":34.01,\"lng\":-116.16", "\"acc\":3.0", "\"k\":{\"fx\":3000.0", "\"heading\":120.0", "\"q\":{"))
            assertTrue(key in good.userComment, "$key in ${good.userComment}")
        val blob = good.record.blob
        assertTrue(near(Vec3.of(blob.down!!), Vec3(0.0, 1.0, 0.0), 1e-6), "held level, down is down the picture")
        assertTrue(near(Vec3.of(blob.downAcc!!), Vec3(1.0, 0.0, 0.0), 1e-6), "(the made-up accelerometer says the phone was upright)")
        assertTrue(!s.frame(FrameIn(pose, 0.0, 0.004, true, 3.2)).shoot, "not again at once")
        assertTrue(!s.frame(FrameIn(pose, 0.0, 0.004, true, 9.0)).shoot, "nor from the same place: nothing to gain")
        s.frame(FrameIn(moved, 0.0, 0.004, true, 9.5)); s.frame(FrameIn(moved, 0.0, 0.004, true, 9.9))
        val soft = take(s, moved, 10.0, ImageStats(0.05, 8.0, 0.0, 0.0, 120.0))
        assertTrue(!soft.kept && soft.helped.isEmpty() && soft.why.single().contains("soft") && soft.record.name == "cap_t1_002.jpg")
        val turning = take(s, moved, 11.0, gyro = 0.6)
        assertTrue(!turning.kept && turning.why.any { "moving" in it })
        assertEquals(1, s.shotCount)
        assertEquals(3, s.record(20.0).shots.size)
        s.skipCurrent("out of reach")
        assertTrue(s.record(20.0).skipped.isEmpty() || s.record(20.0).skipped.values.all { it == "out of reach" })
    }

    @Test fun tightenedInASession() {
        val s = session()
        val rnd = kotlin.random.Random(4)
        val pts = List(600) { truth.apply(Vec3(rnd.nextDouble(-2.0, 2.0), rnd.nextDouble(0.0, 3.0), rnd.nextDouble(-0.01, 0.01))) }
        val r = assertNotNull(s.refine(pts))
        assertTrue(r.rmsM < 0.03 && near(s.fit!!.scale, 1.25, 0.02) && near(s.coverage!!.metresPerUnit, s.fit!!.scale, 1e-12))
        assertTrue(near(assertNotNull(s.scaleAgainstServer()), 1.0, 0.02))
        assertNull(s.refine(pts.map { it + Vec3(5.0, 0.0, 0.0) }), "points nowhere near it leave the fit alone")
    }

    @Test fun aRockWithNoMesh() {
        val s = CaptureSession(null, null, "t3", "test", "phone", 0.0)
        val mid = Vec3(1.0, 1.0, -3.0)
        assertTrue("Tap the middle" in s.frame(FrameIn(TestWorld.look(Vec3.ZERO, mid), 0.0, 0.004, true, 0.0)).hint.text)
        s.startOrbit(mid)
        val orbit = assertNotNull(s.orbit)
        for (k in 3..5) orbit.block(k)       // a quarter of it backs onto another rock
        var t = 1.0
        var a = 0.0
        var guard = 0
        while (!orbit.progress().finished && guard++ < 300) {
            val pose = TestWorld.look(mid + Vec3(3 * sin(a), 0.3, 3 * cos(a)), mid)
            s.frame(FrameIn(pose, 0.0, 0.004, true, t)); t += 1.0       // stepping there, then standing
            val out = s.frame(FrameIn(pose, 0.0, 0.004, true, t)); t += 1.0
            if (out.shoot) { take(s, pose, t); continue }
            a += when (out.hint.kind) { Hint.Kind.FACE -> 0.2 * out.hint.turnX; else -> 0.11 }
        }
        val p = orbit.progress()
        assertTrue(p.finished && p.total == 9 && s.shotCount >= 18, "$p after ${s.shotCount} photos")
        assertTrue(orbit.ring.filterIndexed { i, _ -> i in 3..5 }.all { it.shots == 0 }, "nothing asked of the side marked out of reach")
        // a hold marked on it: wanted close, from four directions
        val spot = orbit.addSpot(mid + Vec3(0.0, 0.2, 1.0))
        assertTrue(!orbit.progress().finished)
        val far = TestWorld.look(mid + Vec3(0.0, 0.2, 4.0), spot.p)
        assertEquals(Hint.Kind.CLOSER, s.frame(FrameIn(far, 0.0, 0.004, true, t)).hint.kind)
        for (k in -2..2) {
            val pose = TestWorld.look(spot.p + Vec3(1.2 * sin(k * 0.35), 0.0, 1.2 * cos(k * 0.35)), spot.p)
            t += 1.0; s.frame(FrameIn(pose, 0.0, 0.004, true, t)); t += 1.0; s.frame(FrameIn(pose, 0.0, 0.004, true, t)); t += 0.1
            take(s, pose, t)
        }
        assertTrue(spot.done() && orbit.progress().finished && s.record(t).mode == "orbit")
        assertTrue(s.markers().single().level == Coverage.LEVEL_DONE)
    }
}
