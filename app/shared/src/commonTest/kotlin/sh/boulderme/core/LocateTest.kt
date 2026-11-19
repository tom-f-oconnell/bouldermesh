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
    }
}
