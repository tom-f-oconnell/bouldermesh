package sh.boulderme.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class SceneryTest {
    private val rock = Vec3(0.0, 1.0, 0.0)

    /** A camera at `from`, held level, looking the way given (horizontally). */
    private fun facing(from: Vec3, way: Vec3) = TestWorld.look(from, from + Vec3(way.x, 0.0, way.z).unit())

    @Test fun aWalkRoundTheRock() {
        val w = SceneryWalk(rock, 2.0)
        assertTrue(w.ringM == 5.0 && w.nearestM == 2.4)
        val close = facing(Vec3(0.0, 1.5, 1.5), Vec3(0.0, 0.0, 1.0))
        assertEquals(Hint.Kind.BACK, w.hint(close, 90).kind)
        assertTrue(!w.wanted(close, 1.0, true), "too near the rock for the scenery")
        val out = facing(Vec3(0.0, 1.5, 5.0), Vec3(0.0, 0.0, 1.0))
        assertTrue(w.outward(out) > 0.99 && w.wanted(out, 1.0, true) && !w.wanted(out, 1.0, false))
        w.add(out, 1.0)
        val s = w.sectorOf(out.centre)
        assertTrue(w.outDone[s] && !w.inDone[s] && w.done == 1)
        assertTrue(!w.wanted(out, 1.2, true), "not again without moving or turning")
        assertTrue(w.wanted(facing(Vec3(0.35, 1.5, 5.0), Vec3(0.0, 0.0, 1.0)), 1.5, true), "moved a step")
        // facing out there: now to face the rock from it
        assertTrue("face the rock" in w.hint(out, 90).text)
        val back = facing(Vec3(0.0, 1.5, 5.0), Vec3(0.0, 0.0, -1.0))
        assertTrue(w.outward(back) < -0.99)
        w.add(back, 2.0)
        assertTrue(w.inDone[s])
        // then round to the next sector, by an arrow across the screen
        val next = w.hint(back, 90)
        assertTrue(next.kind == Hint.Kind.GO && "round" in next.text && kotlin.math.hypot(next.turnX, next.turnY) > 0.9, next.toString())
    }

    @Test fun aPanoramaFromTheTurn() {
        val p = PanoTurn()
        val north = facing(Vec3(0.0, 1.5, 0.0), Vec3(0.0, 0.0, 1.0))
        assertTrue(p.wanted(north, true) && !p.wanted(north, false))
        p.add(north)
        assertTrue(!p.wanted(north, true) && p.count == 1)
        assertTrue(p.wanted(facing(Vec3(0.0, 1.5, 0.0), Vec3(1.0, 0.0, 1.0)), true), "a step round")
        assertNull(p.bucketOf(TestWorld.look(Vec3(0.0, 1.5, 0.0), Vec3(0.0, 0.0, 0.3))), "looking down at the ground")
    }

    @Test fun noSceneryWhenTurnedOff() {
        // as for a gym climb, unless Settings has them: no panorama frames during the turn, and no walk round
        val flow = CaptureFlow(CaptureSession(null, null, "f6", "test", "a phone", 0.0), panorama = false, sceneryOn = false)
        assertNull(flow.frame(facing(Vec3(0.0, 1.5, 0.0), Vec3(0.0, 0.0, 1.0)), true, 0.0, 0.004, 0.0, 90, Vec3(0.0, 9.81, 0.0)).grab)
        flow.skipLevel()
        val at = TestWorld.look(Vec3(0.0, 1.5, 4.0), rock)
        flow.frame(at, true, 0.0, 0.004, 1.0, 90)
        flow.tap(at.centre, at.forward) { rock }
        assertEquals(Phase.CAPTURE, flow.phase)
        assertTrue(flow.startScenery() != null && flow.phase == Phase.CAPTURE)
    }

    @Test fun aFlowTakesFramesForTheScenery() {
        val flow = CaptureFlow(CaptureSession(null, null, "f5", "test", "a phone", 0.0))
        // during the turn on the spot: a frame for the panorama, asked for once for each step of the turn
        val first = flow.frame(facing(Vec3(0.0, 1.5, 0.0), Vec3(0.0, 0.0, 1.0)), true, 0.0, 0.004, 0.0, 90, Vec3(0.0, 9.81, 0.0))
        assertEquals("pano", first.grab)
        assertNull(flow.frame(facing(Vec3(0.0, 1.5, 0.0), Vec3(1.0, 0.0, 1.0)), true, 0.0, 0.004, 0.1, 90).grab, "not while one's being taken")
        val pano = flow.scenery("pano", facing(Vec3(0.0, 1.5, 0.0), Vec3(0.0, 0.0, 1.0)), 0.0, "a", 90, null, 0.0, 0.004)
        assertEquals("pano_f5_01.jpg", pano.name)
        assertNull(flow.frame(facing(Vec3(0.0, 1.5, 0.0), Vec3(0.0, 0.0, 1.0)), true, 0.0, 0.004, 0.2, 90).grab, "the same step of the turn")
        assertEquals("pano", flow.frame(facing(Vec3(0.0, 1.5, 0.0), Vec3(1.0, 0.0, 1.0)), true, 0.0, 0.004, 0.3, 90).grab)
        assertNull(flow.frame(facing(Vec3(0.0, 1.5, 0.0), Vec3(-1.0, 0.0, 1.0)), true, 2.0, 0.01, 0.4, 90).grab, "turning too fast to be sharp")
        flow.noGrab()
        flow.skipLevel()
        // a rock with no mesh: its middle tapped, then the walk round it
        val at = TestWorld.look(Vec3(0.0, 1.5, 4.0), rock)
        flow.frame(at, true, 0.0, 0.004, 1.0, 90)
        flow.tap(at.centre, at.forward) { rock }
        assertEquals(Phase.CAPTURE, flow.phase)
        assertNull(flow.startScenery())
        assertEquals(Phase.SCENERY, flow.phase)
        val out = flow.frame(facing(Vec3(0.0, 1.5, 6.0), Vec3(0.0, 0.0, 1.0)), true, 0.0, 0.004, 2.0, 90)
        assertTrue(out.grab == "walk" && !out.shoot && out.ui.total == 2 * SceneryWalk.SECTORS, "${out.ui}")
        val shot = flow.scenery("walk", facing(Vec3(0.0, 1.5, 6.0), Vec3(0.0, 0.0, 1.0)), 2.0, "a", 90, ImageStats(1.0, 5.0, 0.0, 0.0, 100.0), 0.0, 0.004)
        assertEquals("scn_f5_001.jpg", shot.name)
        assertEquals(2, flow.frame(facing(Vec3(0.0, 1.5, 6.0), Vec3(0.0, 0.0, 1.0)), true, 0.0, 0.004, 2.5, 90).ui.scenery, "the panorama's and the walk's")
        flow.endScenery()
        assertEquals(Phase.CAPTURE, flow.phase)
        // kept in the record, apart from the photos, each with where the tracking had it
        val record = flow.session.record(9.0)
        assertTrue(record.v == 7 && record.scenery.map { it.name } == listOf("pano_f5_01.jpg", "scn_f5_001.jpg") && record.shots.isEmpty())
        assertTrue(record.scenery.all { it.downSrc == "ar" && it.down != null }, "with no turn, the tracking's own down")
        assertTrue(record.scenery[1].ar.p == listOf(0.0, 1.5, 6.0) && record.scenery[1].kind == "walk" && record.scenery[1].sharp == 1.0 && "\"scenery\":[" in record.json())
        assertTrue(listOf("scenery_start", "scenery", "scenery_end").all { "\"e\":\"$it\"" in flow.logText() })
    }
}
