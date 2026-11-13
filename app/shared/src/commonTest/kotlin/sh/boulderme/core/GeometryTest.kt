package sh.boulderme.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import sh.boulderme.core.TestWorld.near

class GeometryTest {
    @Test fun quaternions() {
        val q = Quat.yaw(90 * TestWorld.rad)
        assertTrue(near(q.rotate(Vec3(0.0, 0.0, 1.0)), Vec3(1.0, 0.0, 0.0), 1e-9), "a turn about up takes +Z to +X")
        val r = Quat.axisAngle(Vec3(1.0, 2.0, 3.0), 0.7)
        val v = Vec3(0.3, -1.2, 2.0)
        assertTrue(near((r * r.conj()).rotate(v), v, 1e-9))
        assertTrue(near(Quat.fromAxes(r.rotate(Vec3(1.0, 0.0, 0.0)), r.rotate(Vec3(0.0, 1.0, 0.0)), r.rotate(Vec3(0.0, 0.0, 1.0))).rotate(v), r.rotate(v), 1e-9))
    }

    @Test fun similarity() {
        val s = Similarity(1.25, Quat.yaw(0.6), Vec3(3.0, 0.2, -4.0))
        val p = Vec3(0.4, 1.1, -0.7)
        assertTrue(near(s.inverse().apply(s.apply(p)), p, 1e-9))
        val m = s.matrix()
        val w = s.apply(p)
        assertTrue(near(m[0] * p.x + m[4] * p.y + m[8] * p.z + m[12], w.x, 1e-5) && near(m[2] * p.x + m[6] * p.y + m[10] * p.z + m[14], w.z, 1e-5))
        assertTrue(s.tiltDeg < 1e-6)
    }

    @Test fun cameras() {
        val cam = TestWorld.look(Vec3(0.0, 1.5, 5.0), Vec3(0.0, 1.5, 0.0))
        val mid = assertNotNull(cam.project(Vec3(0.0, 1.5, 0.0)))
        assertTrue(near(mid.u, 2000.0, 1e-6) && near(mid.v, 1500.0, 1e-6) && near(mid.depth, 5.0, 1e-9))
        val right = assertNotNull(cam.project(Vec3(-1.0, 1.5, 0.0)))     // looking along -Z, -X is to the right... of nothing: it's to the left
        assertTrue(right.u < 2000.0, "world -X is on the left of a camera looking along -Z")
        assertTrue(assertNotNull(cam.project(Vec3(0.0, 2.5, 0.0))).v < 1500.0, "up in the world is up the picture")
        assertNull(cam.project(Vec3(0.0, 1.5, 9.0)), "behind it")
        // the same camera given the OpenGL way: x right, y up, z back
        val gl = CameraPose.fromGl(cam.centre, Quat.fromAxes(Vec3(1.0, 0.0, 0.0), Vec3(0.0, 1.0, 0.0), Vec3(0.0, 0.0, 1.0)), cam.lens)
        assertTrue(near(gl.forward, cam.forward, 1e-9) && near(assertNotNull(gl.project(Vec3(0.0, 2.5, 0.0))).v, assertNotNull(cam.project(Vec3(0.0, 2.5, 0.0))).v, 1e-6))
        val s = Similarity(1.25, Quat.yaw(0.6), Vec3(3.0, 0.2, -4.0))
        val p = Vec3(0.3, 1.0, 0.0)
        val world = TestWorld.look(s.apply(Vec3(0.0, 1.5, 4.0)), s.apply(p))
        val inMesh = world.into(s)
        assertTrue(near(inMesh.centre, Vec3(0.0, 1.5, 4.0), 1e-9))
        assertTrue(near(assertNotNull(inMesh.project(p)).u, assertNotNull(world.project(s.apply(p))).u, 1e-6), "it lands on the same pixel")
        assertEquals(6000.0, cam.lens.scaledTo(8000, 6000).fx)
    }

    @Test fun downInAPhoto() {
        // a phone held upright, looking along -Z: its picture's rows run down the phone, so down is along +x of the picture
        val upright = CameraPose.fromGl(Vec3.ZERO, Quat.fromAxes(Vec3(0.0, -1.0, 0.0), Vec3(1.0, 0.0, 0.0), Vec3(0.0, 0.0, 1.0)), TestWorld.lens)
        assertTrue(near(Axes.downInImage(upright), Vec3(1.0, 0.0, 0.0), 1e-9))
        assertTrue(near(Axes.downFromAccel(Vec3(0.0, 9.81, 0.0)), Vec3(1.0, 0.0, 0.0), 1e-9), "and so says its accelerometer")
        assertTrue(near(Axes.downFromAccel(Vec3(0.0, 9.81, 0.0), 270), Vec3(-1.0, 0.0, 0.0), 1e-9))
        // on its side, top to the left: its picture is upright, down is down the picture
        assertTrue(near(Axes.downFromAccel(Vec3(9.81, 0.0, 0.0)), Vec3(0.0, 1.0, 0.0), 1e-9))
        // flat on its back, the camera looks down
        assertTrue(near(Axes.downFromAccel(Vec3(0.0, 0.0, 9.81)), Vec3(0.0, 0.0, 1.0), 1e-9))
        assertTrue(near(assertNotNull(Axes.headingOfView(Quat.IDENTITY.let { Quat.axisAngle(Vec3(1.0, 0.0, 0.0), 90 * TestWorld.rad) })), 0.0, 1e-6), "upright, facing north")
        assertNull(Axes.headingOfView(Quat.IDENTITY), "flat on its back: the camera looks down, so no bearing")
        assertTrue(near(assertNotNull(Axes.headingOfView(Quat.axisAngle(Vec3(1.0, 0.0, 0.0), 90 * TestWorld.rad), 12.0)), 12.0, 1e-6))
    }
}
