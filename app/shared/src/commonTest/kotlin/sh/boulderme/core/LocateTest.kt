package sh.boulderme.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import sh.boulderme.core.TestWorld.near

class LocateTest {
    private val truth = Similarity(1.25, (Quat.yaw(0.6) * Quat.axisAngle(Vec3(1.0, 0.0, 0.0), 0.05)).unit(), Vec3(3.0, 0.2, -4.0))
    private val lens = Lens(500.0, 500.0, 320.0, 240.0, 640, 480)

    /** A frame whose camera is at `c` in the mesh, turned by `yaw`, and where tracking has it: as truth takes it. */
    private fun frame(id: Int, c: Vec3, yaw: Double, wrongBy: Similarity? = null): Pair<Located, LocateFrame> {
        val mesh = CameraPose(c, Quat.yaw(yaw), lens)
        val s = wrongBy ?: truth
        val world = CameraPose(s.apply(c), (s.rot * mesh.rot).unit(), lens)
        return Located(id, mesh, 150, 1.0, "phone") to LocateFrame(id, ByteArray(0), 640, 480, world)
    }

    @Test fun framesGiveTheFit() {
        val fs = listOf(frame(1, Vec3(0.0, 1.5, 3.0), 0.0), frame(2, Vec3(1.0, 1.4, 3.2), 0.2), frame(3, Vec3(-0.6, 1.6, 2.8), -0.3))
        val r = Locate.fromLocated(fs.map { it.first }, fs.map { it.second }, null)
        val fit = assertNotNull(r.fit, r.why)
        assertEquals("cameras", r.scaleFrom)
        val p = Vec3(0.3, 1.7, 0.2)
        assertTrue(near(fit.apply(p), truth.apply(p), 1e-6))
        assertTrue(near(fit.scale, 1.25, 1e-9))
    }

    @Test fun oneFrameNeedsTheScale() {
        val one = listOf(frame(1, Vec3(0.0, 1.5, 3.0), 0.4))
        val none = Locate.fromLocated(one.map { it.first }, one.map { it.second }, null)
        assertNull(none.fit, "no size from one camera")
        val r = Locate.fromLocated(one.map { it.first }, one.map { it.second }, 1.25)
        val fit = assertNotNull(r.fit, r.why)
        assertEquals("plan", r.scaleFrom)
        assertTrue(near(fit.apply(Vec3(1.0, 0.0, 0.0)), truth.apply(Vec3(1.0, 0.0, 0.0)), 1e-6))
    }

    @Test fun disagreeingFramesGiveNoFit() {
        val other = Similarity(1.25, Quat.yaw(0.9), Vec3(3.0, 0.2, -4.0))
        val fs = listOf(frame(1, Vec3(0.0, 1.5, 3.0), 0.0), frame(2, Vec3(1.0, 1.4, 3.2), 0.2, other))
        val r = Locate.fromLocated(fs.map { it.first }, fs.map { it.second }, 1.25)
        assertNull(r.fit)
        assertTrue(r.why.isNotEmpty())
        val weak = listOf(frame(1, Vec3(0.0, 1.5, 3.0), 0.0).let { it.first.copy(inliers = 5) to it.second })
        assertNull(Locate.fromLocated(weak.map { it.first }, weak.map { it.second }, 1.25).fit, "too few matches")
        val alone = listOf(frame(1, Vec3(0.0, 1.5, 3.0), 0.0).let { it.first.copy(inliers = 40) to it.second })
        assertNull(Locate.fromLocated(alone.map { it.first }, alone.map { it.second }, 1.25).fit, "one frame, too weakly placed to go by alone")
        val two = listOf(frame(1, Vec3(0.0, 1.5, 3.0), 0.0), frame(2, Vec3(0.3, 1.5, 3.0), 0.1)).map { it.first.copy(inliers = 40) to it.second }
        assertNotNull(Locate.fromLocated(two.map { it.first }, two.map { it.second }, 1.25).fit, "but two that agree will do")
        val same = listOf(frame(1, Vec3(0.0, 1.5, 3.0), 0.0)).map { it.first.copy(inliers = 40) to it.second }
        assertNull(Locate.fromLocated(same.map { it.first } + same.map { it.first.copy(by = "server") }, same.map { it.second }, 1.25).fit,
            "one frame placed twice (by the phone and the server) is still one")
        val withBad = listOf(frame(1, Vec3(0.0, 1.5, 3.0), 0.0), frame(2, Vec3(1.0, 1.4, 3.2), 0.2, other), frame(3, Vec3(-0.6, 1.6, 2.8), -0.3))
        val r2 = Locate.fromLocated(withBad.map { it.first }, withBad.map { it.second }, 1.25)
        val fit = assertNotNull(r2.fit, r2.why)
        assertEquals(2, r2.frames, "the frame that disagrees is left out")
        assertTrue(near(fit.apply(Vec3(0.3, 1.7, 0.2)), truth.apply(Vec3(0.3, 1.7, 0.2)), 1e-6))
    }

    @Test fun aFlowLinesItselfUp() {
        val t0 = Similarity(1.25, Quat.yaw(0.6), Vec3(3.0, 0.2, -4.0))
        val flow = CaptureFlow(CaptureSession(TestWorld.plan(), TestWorld.guide(), "l1", "test", "a phone", 0.0))
        flow.canLocate = true
        flow.skipLevel()
        assertEquals(Phase.GHOST, flow.phase)
        fun pose(x: Double) = TestWorld.look(t0.apply(Vec3(x, 1.5, 4.0)), t0.apply(Vec3(0.0, 1.5, 0.0)))
        val a = pose(0.0)
        assertEquals("locate", flow.frame(a, true, 0.0, 0.004, 0.1, 90).grab)
        assertTrue("line itself up" in flow.frame(a, true, 0.0, 0.004, 0.2, 90).ui.text)
        val fa = flow.locateFrame(ByteArray(64 * 48), 64, 48, a)
        assertEquals(64, fa.world.lens.w, "its lens is the grey picture's")
        assertNull(flow.frame(a, true, 0.0, 0.004, 1.0, 90).grab, "not again so soon")
        flow.located(listOf(Located(fa.id, a.into(t0), 40, 1.0, "server")), "server")
        assertEquals(Phase.GHOST, flow.phase, "one weakly placed frame isn't enough")
        assertNull(flow.frame(a, true, 0.0, 0.004, 2.0, 90).grab, "nor from where the last was")
        val b = pose(0.6)
        assertEquals("locate", flow.frame(b, true, 0.0, 0.004, 2.1, 90).grab)
        val fb = flow.locateFrame(ByteArray(64 * 48), 64, 48, b)
        flow.located(listOf(Located(fb.id, b.into(t0), 40, 1.0, "phone")), "phone")
        assertEquals(Phase.CONFIRM, flow.phase)
        val fit = assertNotNull(flow.session.fit)
        assertTrue(near(fit.apply(Vec3(1.0, 1.0, 0.0)), t0.apply(Vec3(1.0, 1.0, 0.0)), 1e-6))
        assertTrue("by itself" in flow.frame(b, true, 0.0, 0.004, 2.2, 90).ui.note)
        // started again, answers about frames from before match nothing
        flow.restartAlign()
        flow.located(listOf(Located(fb.id, b.into(t0), 400, 1.0, "phone")), "phone")
        assertEquals(Phase.GHOST, flow.phase)
    }
}
