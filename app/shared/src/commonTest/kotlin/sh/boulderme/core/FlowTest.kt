package sh.boulderme.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import sh.boulderme.core.TestWorld.near

/** A capture as its screen goes through it: the small model tapped, then the rock, the fit checked, and photos. */
class FlowTest {
    private val truth = Similarity(1.25, Quat.yaw(0.6), Vec3(3.0, 0.2, -4.0))
    private val sharp = ImageStats(1.0, 8.0, 0.0, 0.0, 120.0)
    private fun shot(pose: CameraPose, t: Double, stats: ImageStats = sharp) =
        ShotIn(pose, SensorSample(Vec3(0.0, 9.81, 0.0), Vec3.ZERO, t = t), stats, 0.004, 80.0, 1.3, true, "a", t, 1.7e9 + t)

    @Test fun fromTheSmallModelToPhotos() {
        val flow = CaptureFlow(CaptureSession(TestWorld.plan(), TestWorld.guide(), "f1", "test", "a phone", 0.0))
        assertEquals(Phase.LEVEL, flow.phase)
        flow.skipLevel()
        assertEquals(Phase.GHOST, flow.phase)
        // standing 5 m back from the wall, looking at it
        val eye = truth.apply(Vec3(0.0, 1.5, 4.0))
        val pose = TestWorld.look(eye, truth.apply(Vec3(0.0, 1.5, 0.0)))
        assertNull(flow.frame(pose, false, 0.0, 0.004, 0.0, 90).guide, "not tracking yet: nothing is drawn")
        var out = flow.frame(pose, true, 0.0, 0.004, 0.1, 90)
        val ghost = assertNotNull(out.guide)
        assertTrue(out.solid && near(ghost.scale * 4.0, CaptureFlow.GHOST_ACROSS_M, 1e-9) && out.levels != null, "a small model, coloured")
        val middle = ghost.apply(Vec3(0.0, 1.5, 0.0))
        assertTrue(middle.distance(eye) in 1.0..1.3 && ((middle - eye).unit() dot pose.forward) > 0.95, "a metre in front")
        assertTrue(flow.frame(pose, true, 0.0, 0.004, 0.2, 90).guide == ghost, "and it stays where it was put")

        fun tapModel(mesh: Vec3) = flow.tap(eye, (ghost.apply(mesh) - eye).unit()) { error("a tap on the model asks nothing of the world") }
        flow.tap(eye, Vec3.UP) { null }
        assertTrue(flow.phase == Phase.GHOST && "missed" in flow.frame(pose, true, 0.0, 0.004, 0.3, 90).ui.note)
        val a = Vec3(-1.0, 0.5, 0.0); val b = Vec3(1.2, 2.0, 0.0)
        tapModel(a)
        assertEquals(Phase.ROCK, flow.phase)
        flow.tap(eye, pose.forward) { null }
        assertTrue(flow.phase == Phase.ROCK && "made out" in flow.frame(pose, true, 0.0, 0.004, 0.4, 90).ui.note, "nothing found there: asked again")
        flow.tap(eye, pose.forward) { truth.apply(a) }
        out = flow.frame(pose, true, 0.0, 0.004, 0.5, 90)
        assertTrue(flow.phase == Phase.GHOST && out.ui.pairs == 1 && "2 of 2" in out.ui.text && out.markers.size == 2, out.ui.text)
        tapModel(b)
        flow.tap(eye, pose.forward) { truth.apply(b) }
        assertEquals(Phase.CONFIRM, flow.phase)
        val fit = assertNotNull(flow.session.fit)
        assertTrue(near(fit.apply(Vec3(0.5, 1.0, 0.0)), truth.apply(Vec3(0.5, 1.0, 0.0)), 1e-4) && near(fit.scale, 1.25, 1e-4), "the mesh is on the rock: $fit")
        out = flow.frame(pose, true, 0.0, 0.004, 0.6, 90)
        assertTrue(!out.solid && out.guide == fit && out.levels != null && !out.shoot, "shown over the rock to check; no photos yet")

        // the points the phone tracks on the wall tighten it, every few seconds
        assertNull(flow.refineDue(5.0), "nothing tracked yet")
        val rnd = kotlin.random.Random(3)
        repeat(900) { truth.apply(Vec3(rnd.nextDouble(-2.0, 2.0), rnd.nextDouble(0.0, 3.0), rnd.nextDouble(-0.01, 0.01))).let { flow.addPoint(it.x, it.y, it.z) } }
        val work = assertNotNull(flow.refineDue(5.0))
        assertTrue(work.points.size > 300 && flow.refineDue(6.0) == null && flow.refineDue(60.0) == null, "${work.points.size} points; and not again until that's back")
        flow.tightened(work, flow.tighter(work))
        assertTrue("Fitted to" in flow.frame(pose, true, 0.0, 0.004, 6.5, 90).ui.note && near(flow.session.fit!!.scale, 1.25, 0.02))
        // work handed out before the fit was set afresh comes back to nothing
        val stale = assertNotNull(flow.refineDue(60.0))
        val found = assertNotNull(flow.tighter(stale))
        flow.addSpot()
        assertEquals(Phase.GHOST, flow.phase)
        flow.restartAlign()
        assertTrue(flow.session.fit == null && flow.frame(pose, true, 0.0, 0.004, 7.0, 90).ui.pairs == 0)
        val again = assertNotNull(flow.frame(pose, true, 0.0, 0.004, 7.1, 90).guide)
        for (spot in listOf(a, b)) { flow.tap(eye, (again.apply(spot) - eye).unit()) { null }; flow.tap(eye, pose.forward) { truth.apply(spot) } }
        val afresh = assertNotNull(flow.session.fit)
        flow.tightened(stale, found)
        assertTrue(flow.session.fit == afresh, "the fit tapped since stays as it is")
        flow.confirmFit()
        assertEquals(Phase.CAPTURE, flow.phase)

        // close to a hold and still: it asks for a photo, and for no other until that one is back
        val hold = truth.apply(Vec3(-0.5, 1.0, 0.0))
        val close = TestWorld.look(hold + truth.dir(Vec3(0.0, 0.0, 1.0)) * 1.3, hold)
        var t = 20.0
        out = flow.frame(close, true, 0.0, 0.004, t, 90)
        while (!out.shoot && t < 30.0) { t += 0.2; out = flow.frame(close, true, 0.0, 0.004, t, 90) }
        assertTrue(out.shoot && out.ui.total == 9 && out.ui.holds == 2 && out.ui.guided, "${out.ui}")
        assertFalse(flow.frame(close, true, 0.0, 0.004, t + 0.1, 90).shoot, "one at a time")
        flow.shutter()
        assertFalse(flow.frame(close, true, 0.0, 0.004, t + 0.2, 90).shoot, "even by the button")
        assertTrue(flow.shot(shot(close, t + 0.3)).kept)
        out = flow.frame(close, true, 0.0, 0.004, t + 0.4, 90)
        assertTrue(out.shoot && out.ui.kept == 1 && out.levels != null, "the button's photo, now the first is back; and the mesh coloured afresh")
        assertTrue("GPS: no fix" in out.ui.sensors && "compass" !in out.ui.sensors, "kept, but the phone had no fix: ${out.ui.sensors}")
        assertFalse(flow.shot(shot(close, t + 0.5, ImageStats(0.05, 8.0, 0.0, 0.0, 120.0))).kept)
        val ui = flow.frame(close, true, 0.0, 0.004, t + 2.0, 90).ui
        assertTrue(ui.kept == 1 && ui.rejected == 1 && ui.note.startsWith("Not kept") && "soft" in ui.note, ui.note)
        flow.shutter()
        assertTrue(flow.frame(close, true, 0.0, 0.004, t + 3.0, 90).shoot)
        flow.noShot("The camera gave no picture.")
        assertTrue("no picture" in flow.frame(close, true, 0.0, 0.004, t + 4.0, 90).ui.note)
        flow.toggleWide()
        assertEquals(15, flow.frame(close, true, 0.0, 0.004, t + 5.0, 90).ui.total)
        assertTrue(ui.canSkip)
        flow.skip()
        assertTrue(flow.session.record(99.0).skipped.isNotEmpty())
    }

    @Test fun firstWhichWayIsDown() {
        val flow = CaptureFlow(CaptureSession(null, null, "f3", "test", "a phone", 0.0))
        // the tracking's world is a degree off level, and the accelerometer reads 0.2 m/s2 off sideways
        val down = Quat.axisAngle(Vec3(0.0, 0.0, 1.0), 1.0 * TestWorld.rad).rotate(Vec3(0.0, -1.0, 0.0))
        fun at(yaw: Double): Pair<CameraPose, Vec3> {
            val pose = TestWorld.look(Vec3.ZERO, Vec3(kotlin.math.sin(yaw), 0.1, kotlin.math.cos(yaw)))
            return pose to (Axes.forceInImage(pose.rot.conj().rotate(-down * Sensors.G)) + Vec3(0.2, 0.0, 0.1))
        }
        var t = 0.0
        var out = flow.frame(at(0.0).first, true, 0.0, 0.004, t, 90)
        assertTrue(out.ui.phase == Phase.LEVEL && "turn slowly" in out.ui.text && out.ui.total == 12 && out.ui.done == 0 && !out.shoot)
        for (i in 0 until 180) {
            val (pose, accel) = at(i * TestWorld.rad)
            t += 0.05; out = flow.frame(pose, true, 0.3, 0.004, t, 90, accel)
        }
        assertTrue(out.ui.phase == Phase.LEVEL && out.ui.done == 6, "half way round: ${out.ui.done} of 12")
        for (i in 180 until 360) {
            val (pose, accel) = at(i * TestWorld.rad)
            t += 0.05; out = flow.frame(pose, true, 0.3, 0.004, t, 90, accel)
        }
        val level = assertNotNull(flow.session.level)
        assertTrue(flow.phase == Phase.CENTRE && angleDeg(level.down, down) < 0.05 && TestWorld.near(level.tiltDeg, 1.0, 0.05), "$level")
        assertTrue("Down found" in flow.frame(at(0.0).first, true, 0.0, 0.004, t + 1, 90).ui.note)
        // photos then carry down as the turn found it, with the tracking's own beside it; and the record says how it went
        val mid = Vec3(0.0, 1.0, 3.0)
        val pose = TestWorld.look(Vec3(0.0, 1.5, 0.0), mid)
        flow.tap(pose.centre, pose.forward) { mid }
        flow.shutter()
        assertTrue(flow.frame(pose, true, 0.0, 0.004, t + 2, 90).shoot)
        val accel = Axes.forceInImage(pose.rot.conj().rotate(-down * Sensors.G)) + Vec3(0.2, 0.0, 0.1)
        val shot = flow.shot(ShotIn(pose, SensorSample(accel, Vec3.ZERO, t = t + 2), sharp, 0.004, 80.0, 1.3, true, "a", t + 2, 1.7e9))
        val blob = shot.record.blob
        assertTrue(shot.kept && blob.downSrc == "turn" && near(Vec3.of(blob.down!!), pose.rot.conj().rotate(down), 1e-3), "${blob.downSrc} ${blob.down}")
        assertTrue(TestWorld.near(angleDeg(Vec3.of(blob.downTrack!!), Vec3.of(blob.down!!)), 1.0, 0.05), "the tracking's own, a degree off, is kept beside it")
        // the accelerometer's own reading for the photo, less what the turn found the phone adds, agrees to a few tenths of
        // a degree: not better, as the part of the phone's error that lay along gravity during the turn, and so couldn't be
        // told from it, no longer does once the phone looks down from where it looked level
        assertTrue(angleDeg(Vec3.of(blob.downAcc!!), Vec3.of(blob.down!!)) < 0.5, "${angleDeg(Vec3.of(blob.downAcc!!), Vec3.of(blob.down!!))}")
        val record = flow.session.record(99.0)
        assertTrue(TestWorld.near(record.level!!.tiltDeg, 1.0, 0.05) && record.level!!.sectors == 12 && record.downAgreeDeg!! < 0.5 && "\"down_agree_deg\"" in record.json())
    }

    @Test fun trackingLostAndBack() {
        val plan = TestWorld.plan().copy(key = "abc123def456", made = 1.7e9, guide = GuideInfo(patches = 12, sha1 = "0123abcd"))
        val flow = CaptureFlow(CaptureSession(plan, TestWorld.guide(), "f4", "test", "a phone", 0.0))
        flow.skipLevel()
        val eye = truth.apply(Vec3(0.0, 1.5, 4.0))
        val pose = TestWorld.look(eye, truth.apply(Vec3(0.0, 1.5, 0.0)))
        val ghost = assertNotNull(flow.frame(pose, true, 0.0, 0.004, 0.1, 90).guide)
        for (spot in listOf(Vec3(-1.0, 0.5, 0.0), Vec3(1.2, 2.0, 0.0))) { flow.tap(eye, (ghost.apply(spot) - eye).unit()) { null }; flow.tap(eye, pose.forward) { truth.apply(spot) } }
        flow.confirmFit()
        flow.addPoint(1.0, 2.0, 3.0); flow.addPoint(1.01, 2.0, 3.0); flow.addPoint(1.5, 2.0, 3.0)
        assertEquals(2, flow.pointCount, "one to each 5 cm")
        fun take(t: Double): ShotOut { flow.shutter(); assertTrue(flow.frame(pose, true, 0.0, 0.004, t, 90).shoot); return flow.shot(shot(pose, t)) }
        assertEquals("a", take(2.0).record.blob.ar!!.s)
        // a blink of lost tracking keeps the same spell; two seconds of it starts another
        flow.frame(pose, false, 0.0, 0.004, 3.0, 90); flow.frame(pose, true, 0.0, 0.004, 3.3, 90)
        assertEquals("a", take(4.0).record.blob.ar!!.s)
        flow.frame(pose, false, 0.0, 0.004, 5.0, 90); flow.frame(pose, false, 0.0, 0.004, 6.5, 90); flow.frame(pose, true, 0.0, 0.004, 7.0, 90)
        assertEquals("a.2", take(8.0).record.blob.ar!!.s)
        flow.addPoint(-4.0, 0.5, 0.25)
        // the points go with the record, each with the spell that found it
        val bytes = flow.points()
        assertTrue(bytes.size == 8 + 3 * 14 && bytes.decodeToString(0, 4) == "BMP1" && bytes[4].toInt() == 3)
        fun float(at: Int) = Float.fromBits((bytes[at].toInt() and 0xff) or ((bytes[at + 1].toInt() and 0xff) shl 8) or ((bytes[at + 2].toInt() and 0xff) shl 16) or ((bytes[at + 3].toInt() and 0xff) shl 24))
        val last = 8 + 2 * 14
        assertTrue(float(last) == -4.0f && float(last + 4) == 0.5f && float(last + 8) == 0.25f && bytes[last + 12].toInt() == 2 && bytes[8 + 12].toInt() == 1)
        assertNull(flow.refineDue(100.0), "and the fit isn't tightened against points from before, whose world may not be this one")
        // the record names the plan it went by
        val record = flow.session.record(99.0)
        assertTrue(record.v == 2 && record.plan == RecordPlan("abc123def456", 1.7e9, "0123abcd") && "\"plan\":{\"key\":\"abc123def456\"" in record.json(), record.json().take(300))
        assertEquals(listOf("a", "a", "a.2"), record.shots.map { it.blob.ar!!.s })
    }

    @Test fun aRockWithNoMesh() {
        val flow = CaptureFlow(CaptureSession(null, null, "f2", "test", "a phone", 0.0))
        flow.skipLevel()
        assertEquals(Phase.CENTRE, flow.phase)
        val mid = Vec3(0.0, 1.0, 0.0)
        val pose = TestWorld.look(Vec3(0.0, 1.5, 4.0), mid)
        assertTrue("middle" in flow.frame(pose, true, 0.0, 0.004, 0.0, 90).ui.text)
        flow.tap(pose.centre, pose.forward) { null }
        assertEquals(Phase.CENTRE, flow.phase)
        flow.tap(pose.centre, pose.forward) { mid }
        assertEquals(Phase.CAPTURE, flow.phase)
        var out = flow.frame(pose, true, 0.0, 0.004, 1.0, 90)
        assertTrue(!out.ui.guided && out.ui.total == 12 && out.guide == null && !out.ui.canSkip, "${out.ui}")
        assertTrue(out.shoot && flow.shot(shot(pose, 1.1)).kept, "looking at it, still: the first photo takes itself")
        flow.tap(pose.centre, pose.forward) { Vec3(0.3, 1.2, 0.5) }
        assertEquals(12, flow.frame(pose, true, 0.0, 0.004, 1.5, 90).ui.total, "a tap marks nothing unless asked to")
        flow.toggleMarking()
        flow.tap(pose.centre, pose.forward) { Vec3(0.3, 1.2, 0.5) }
        out = flow.frame(pose, true, 0.0, 0.004, 2.0, 90)
        assertTrue(!flow.marking && out.markers.size == 1 && out.ui.total == 13, "a hold marked: ${out.ui}")
        flow.blockSide()
        assertEquals(12, flow.frame(pose, true, 0.0, 0.004, 3.0, 90).ui.total, "a side out of reach isn't asked for")
        assertEquals(1, flow.session.record(9.0).shots.size)
    }
}
