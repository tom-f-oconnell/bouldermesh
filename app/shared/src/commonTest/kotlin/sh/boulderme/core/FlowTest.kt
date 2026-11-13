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
        val points = assertNotNull(flow.refineDue(5.0))
        assertTrue(points.size > 300 && flow.refineDue(6.0) == null, "${points.size} points, and not again at once")
        flow.refine(points)
        assertTrue("Fitted to" in flow.frame(pose, true, 0.0, 0.004, 6.5, 90).ui.note && near(flow.session.fit!!.scale, 1.25, 0.02))
        flow.addSpot()
        assertEquals(Phase.GHOST, flow.phase)
        flow.restartAlign()
        assertTrue(flow.session.fit == null && flow.frame(pose, true, 0.0, 0.004, 7.0, 90).ui.pairs == 0)
        val again = assertNotNull(flow.frame(pose, true, 0.0, 0.004, 7.1, 90).guide)
        for (spot in listOf(a, b)) { flow.tap(eye, (again.apply(spot) - eye).unit()) { null }; flow.tap(eye, pose.forward) { truth.apply(spot) } }
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

    @Test fun aRockWithNoMesh() {
        val flow = CaptureFlow(CaptureSession(null, null, "f2", "test", "a phone", 0.0))
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
