package sh.boulderme.core

import kotlin.math.floor
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Placing a frame on the phone (PhoneLocate): a made-up textured wall, a pack made from two views, a third placed. */
class PhoneLocateTest {
    private val w = 640; private val h = 480
    private val lens = Lens(520.0, 520.0, w / 2 - 0.5, h / 2 - 0.5, w, h)
    private val tex = 256
    private val texture: FloatArray = run {      // blobs of brightness, smoothly interpolated
        val r = Random(3); val n = 40
        val coarse = FloatArray(n * n) { r.nextFloat() * 255 }
        FloatArray(tex * tex) { i ->
            val x = (i % tex) * (n - 1.0) / tex; val y = (i / tex) * (n - 1.0) / tex
            val x0 = floor(x).toInt(); val y0 = floor(y).toInt(); val fx = (x - x0).toFloat(); val fy = (y - y0).toFloat()
            fun c(a: Int, b: Int) = coarse[b * n + a]
            (c(x0, y0) * (1 - fx) + c(x0 + 1, y0) * fx) * (1 - fy) + (c(x0, y0 + 1) * (1 - fx) + c(x0 + 1, y0 + 1) * fx) * fy
        }
    }

    /** A camera 3 units in front of the wall (z = 0, from -1 to 1), at x, turned by yaw: CameraPose's axes. */
    private fun camera(yaw: Double, x: Double): CameraPose = CameraPose(Vec3(x, 0.0, -3.0), Quat.yaw(-yaw), lens)

    private fun onWall(c: CameraPose, u: Double, v: Double): Vec3? {
        val d = c.rot.rotate(Vec3((u - lens.cx) / lens.fx, (v - lens.cy) / lens.fy, 1.0))
        if (d.z <= 1e-9) return null
        return c.centre + d * (-c.centre.z / d.z)
    }

    private fun render(c: CameraPose): ByteArray = ByteArray(w * h) { i ->
        val p = onWall(c, (i % w).toDouble(), (i / w).toDouble())
        if (p == null || p.x < -1 || p.x >= 1 || p.y < -1 || p.y >= 1) 40.toByte()
        else {
            val tx = (p.x + 1) / 2 * (tex - 1); val ty = (p.y + 1) / 2 * (tex - 1)
            val x0 = floor(tx).toInt().coerceAtMost(tex - 2); val y0 = floor(ty).toInt().coerceAtMost(tex - 2)
            val fx = (tx - x0).toFloat(); val fy = (ty - y0).toFloat()
            fun t(a: Int, b: Int) = texture[b * tex + a]
            ((t(x0, y0) * (1 - fx) + t(x0 + 1, y0) * fx) * (1 - fy) + (t(x0, y0 + 1) * (1 - fx) + t(x0 + 1, y0 + 1) * fx) * fy).toInt().toByte()
        }
    }

    /** A pack as build_pack() makes one: each view's features, each a point of its own on the wall. */
    private fun pack(views: List<CameraPose>): LocatePack {
        val xyz = ArrayList<Float>(); val point = ArrayList<Int>(); val photo = ArrayList<Int>(); val desc = ArrayList<Byte>()
        for ((i, c) in views.withIndex()) {
            val f = Sift.of(render(c), w, h)
            for (k in 0 until f.size) {
                val p = onWall(c, f.xs[k].toDouble(), f.ys[k].toDouble()) ?: continue
                point.add(xyz.size / 3); photo.add(i); xyz.addAll(listOf(p.x.toFloat(), p.y.toFloat(), p.z.toFloat()))
                for (t in 0 until 128) desc.add(f.desc[k * 128 + t])
            }
        }
        return LocatePack(xyz.toFloatArray(), point.toIntArray(), photo.toIntArray(), desc.toByteArray(), views.size)
    }

    @Test fun placesANewView() {
        val pack = pack(listOf(camera(0.14, -0.4), camera(-0.14, 0.4)))
        assertTrue(pack.points > 200, "${pack.points} points")
        val truth = camera(0.05, 0.1)
        val got = assertNotNull(PhoneLocate.place(pack, render(truth), w, h, lens))
        assertTrue(got.inliers >= Locate.MIN_INLIERS)
        val pose = got.pose(lens)
        assertTrue(pose.centre.distance(truth.centre) < 0.03, "off by ${pose.centre.distance(truth.centre)}")
        assertTrue(angleDeg(pose.forward, truth.forward) < 0.5)
        val noise = Random(9).let { r -> ByteArray(w * h) { r.nextInt(256).toByte() } }
        assertNull(PhoneLocate.place(pack, noise, w, h, lens), "noise places nothing")
    }

    @Test fun threePoints() {
        val cam = Similarity(1.0, (Quat.yaw(0.3) * Quat.axisAngle(Vec3(1.0, 0.0, 0.0), 0.2)).unit(), Vec3(0.1, -0.2, 4.0))
        val p = listOf(Vec3(-1.0, 0.2, 0.0), Vec3(0.8, -0.5, 0.3), Vec3(0.1, 0.9, -0.4))
        val got = PhoneLocate.p3p(p, p.map { cam.apply(it).unit() })
        assertTrue(got.isNotEmpty())
        assertTrue(got.any { s -> s.shift.distance(cam.shift) < 1e-6 && angleDeg(s.rot.rotate(Vec3(1.0, 2.0, 3.0)), cam.rot.rotate(Vec3(1.0, 2.0, 3.0))) < 1e-4 },
            "the true camera among ${got.size}")
        assertEquals(0, PhoneLocate.p3p(listOf(p[0], p[0], p[1]), p.map { cam.apply(it).unit() }).size, "two points the same")
    }

    @Test fun badPacks() {
        assertFailsWith<IllegalArgumentException> { LocatePack.parse(ByteArray(10)) }
        assertFailsWith<IllegalArgumentException> { LocatePack.parse("BMLP".encodeToByteArray() + ByteArray(16) { if (it == 0) 1 else if (it == 4) 1 else 0 }) }
    }
}
