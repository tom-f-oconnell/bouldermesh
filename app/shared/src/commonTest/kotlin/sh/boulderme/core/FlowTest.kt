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
        assertTrue(out.shoot && out.ui.total == 15 && out.ui.holds == 2 && out.ui.guided && out.ui.wide && out.ui.reach == "whole rock", "${out.ui}")
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
        assertTrue(flow.frame(close, true, 0.0, 0.004, t + 5.0, 90).ui.let { it.total == 9 && it.reach == "its climbs" && !it.wide })
        flow.toggleWide(); assertTrue(flow.wide, "with no set of beta chosen: the whole rock, or its climbs")
        assertTrue(ui.canSkip)
        flow.frame(close, true, 0.0, 0.004, t + 6.0, 90)
        flow.skip()
        assertTrue(flow.session.record(99.0).skipped.isNotEmpty())
        // and how it went, a line for each change: JSON with its time from the first frame
        flow.event("camera", "image" to "1920x1080", "fps" to 30)
        val lines = flow.logText().trim().lines()
        val kinds = lines.map { Regex("\"e\":\"(\\w+)\"").find(it)!!.groupValues[1] }
        assertTrue(lines.all { it.startsWith("{\"t\":") && it.endsWith("}") } && lines.first().startsWith("{\"t\":0.0,"), lines.take(3).joinToString("\n"))
        for (k in listOf("start", "phase", "tracking", "tap_model", "tap_rock", "fit_confirmed", "hint", "shoot", "shot", "shutter", "no_shot", "reach", "skip", "camera"))
            assertTrue(k in kinds, "$k: $kinds")
        assertTrue(lines.any { "\"kept\":false" in it && "soft" in it }, "a photo not kept, and why")
        assertTrue(kinds.count { it == "phase" } < 10 && lines.size < 80, "a line a change, not a line a frame: ${lines.size}")
    }

    @Test fun anUnsteadyDirectionIsFacedAgain() {
        val flow = CaptureFlow(CaptureSession(null, null, "f4", "test", "a phone", 0.0))
        val down = Quat.axisAngle(Vec3(0.0, 0.0, 1.0), 1.0 * TestWorld.rad).rotate(Vec3(0.0, -1.0, 0.0))
        // facing 60 to 90 degrees round, the phone is swung sideways the whole time: 0.3 m/s2, nearly 2 degrees' worth,
        // which would leave the whole turn agreeing to only half a degree
        fun at(deg: Int, swung: Boolean): Pair<CameraPose, Vec3> {
            val yaw = deg * TestWorld.rad
            val pose = TestWorld.look(Vec3.ZERO, Vec3(kotlin.math.sin(yaw), 0.1, kotlin.math.cos(yaw)))
            val push = if (swung) Vec3(0.3, 0.0, 0.0) else Vec3.ZERO
            return pose to (Axes.forceInImage(pose.rot.conj().rotate(-down * Sensors.G + push)) + Vec3(0.2, 0.0, 0.1))
        }
        var t = 0.0
        var out = flow.frame(at(0, false).first, true, 0.0, 0.004, t, 90)
        var clearedAt = -1
        for (i in 0 until 240) {
            val (pose, accel) = at(i, i in 60 until 90)
            t += 0.05; out = flow.frame(pose, true, 0.3, 0.004, t, 90, accel)
            val lvl = assertNotNull(out.ui.level)
            if (clearedAt < 0 && lvl.faced[2] == 3) clearedAt = i
        }
        // found out early, well before the turn is done; and only that direction is to face again, the rest kept
        assertTrue(clearedAt in 0 until 160, "cleared at $clearedAt degrees")
        val lvl = assertNotNull(out.ui.level)
        assertTrue(lvl.faced[2] == 3 && lvl.faced.count { it == 2 } == 7 && out.ui.done == 7, "${lvl.faced}")
        assertTrue(lvl.agreeDeg!! < 0.1 && lvl.wobbleNow < 0.2 && "unsteadily" in out.ui.note, "$lvl ${out.ui.note}")
        assertTrue(flow.log.text().contains("\"e\":\"level_redo\""))
        // the rest of the way round, and back to face that direction again, steadily: done
        for (i in 240 until 360) { val (pose, accel) = at(i, false); t += 0.05; out = flow.frame(pose, true, 0.3, 0.004, t, 90, accel) }
        assertEquals(Phase.LEVEL, flow.phase)
        for (i in 359 downTo 50) {
            val (pose, accel) = at(i, false); t += 0.05; out = flow.frame(pose, true, 0.3, 0.004, t, 90, accel)
            if (flow.phase != Phase.LEVEL) break
        }
        val level = assertNotNull(flow.session.level)
        assertTrue(flow.phase == Phase.CENTRE && angleDeg(level.down, down) < 0.05, "$level")
    }

    @Test fun howSteadyTheTurnIs() {
        val flow = CaptureFlow(CaptureSession(null, null, "f5", "test", "a phone", 0.0))
        val down = Vec3(0.0, -1.0, 0.0)
        var t = 0.0
        fun frame(deg: Int, shake: Double, gyro: Double = 0.3): LevelUi {
            val yaw = deg * TestWorld.rad
            val pose = TestWorld.look(Vec3.ZERO, Vec3(kotlin.math.sin(yaw), 0.1, kotlin.math.cos(yaw)))
            val wobble = Vec3(shake * kotlin.math.sin(t * 40), 0.0, shake * kotlin.math.cos(t * 31))
            t += 0.033
            return assertNotNull(flow.frame(pose, true, gyro, 0.004, t, 90, Axes.forceInImage(pose.rot.conj().rotate(-down * Sensors.G + wobble))).ui.level)
        }
        var lvl = frame(0, 0.0)
        for (i in 0 until 30) lvl = frame(i, 0.0)
        assertTrue(lvl.wobbleNow < 0.1 && lvl.wobbleNow < lvl.wobbleOk && !lvl.tooFast && lvl.facing == 0, "$lvl")
        for (i in 30 until 60) lvl = frame(i, 0.5)       // shaken: half a metre a second squared, about 3 degrees
        assertTrue(lvl.wobbleNow > lvl.wobbleOk && lvl.wobbleTurn > 0.5 && lvl.facing == 1, "$lvl")
        assertTrue(frame(60, 0.0, gyro = 2.0).tooFast)
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
        flow.frame(pose, true, 0.0, 0.004, t + 1.5, 90)
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

    @Test fun takeWaitsForTheButtonsPhotoToBeSteady() {
        val flow = CaptureFlow(CaptureSession(null, null, "f5", "test", "a phone", 0.0))
        flow.skipLevel()
        val mid = Vec3(0.0, 1.0, 3.0)
        val pose = TestWorld.look(Vec3(0.0, 1.5, 0.0), mid)
        flow.frame(pose, true, 0.0, 0.004, 0.0, 90)
        flow.tap(pose.centre, pose.forward) { mid }
        flow.setAuto(false)
        // pressed with the phone turning: no photo yet, and it says to hold still; pressed again, it still waits for the one
        flow.shutter()
        var out = flow.frame(pose, true, 1.0, 0.004, 1.0, 90)
        assertTrue(!out.shoot && out.ui.waiting && "Hold still" in out.ui.text, "${out.ui}")
        flow.shutter()
        assertFalse(flow.frame(pose, true, 1.0, 0.004, 1.5, 90).shoot)
        out = flow.frame(pose, true, 0.0, 0.004, 1.7, 90)
        assertTrue(!out.shoot && out.ui.waiting, "not the moment it's still: it waits to see it stay so")
        out = flow.frame(pose, true, 0.0, 0.004, 2.0, 90)
        assertTrue(out.shoot && !out.ui.waiting, "taken once still for long enough")
        assertTrue(flow.shot(shot(pose, 2.0)).kept)
        assertFalse(flow.frame(pose, true, 0.0, 0.004, 2.5, 90).shoot, "one press, one photo")
        // never still: the press lapses, and says why
        flow.shutter()
        flow.frame(pose, true, 1.0, 0.004, 3.0, 90)
        out = flow.frame(pose, true, 1.0, 0.004, 3.0 + CaptureFlow.WAIT_S + 0.1, 90)
        assertTrue(!out.ui.waiting && "didn't hold still" in out.ui.note, "${out.ui}")
        assertFalse(flow.frame(pose, true, 0.0, 0.004, 20.0, 90).shoot)
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
        fun take(t: Double): ShotOut {
            flow.shutter()
            val at = (0..4).map { t - 0.4 + it * 0.1 }.firstOrNull { flow.frame(pose, true, 0.0, 0.004, it, 90).shoot }
            return flow.shot(shot(pose, assertNotNull(at, "held still, the photo is taken")))
        }
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
        assertTrue(record.v == 4 && record.plan == RecordPlan("abc123def456", 1.7e9, "0123abcd") && "\"plan\":{\"key\":\"abc123def456\"" in record.json(), record.json().take(300))
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
        flow.frame(pose, true, 0.0, 0.004, 0.6, 90)
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
