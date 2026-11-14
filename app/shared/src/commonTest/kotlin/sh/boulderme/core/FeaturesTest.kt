package sh.boulderme.core

import kotlin.math.floor
import kotlin.test.Test
import kotlin.test.assertTrue

class FeaturesTest {
    private val lens = Lens(600.0, 600.0, 320.0, 240.0, 640, 480)

    /** A wall at z = 0 with blotches on it (or plain grey), as a camera at `pose` sees it: its brightness, a byte a pixel. */
    private fun picture(pose: CameraPose, textured: Boolean = true): ByteArray {
        val out = ByteArray(lens.w * lens.h)
        for (v in 0 until lens.h) for (u in 0 until lens.w) {
            val d = pose.rot.rotate(Vec3((u - lens.cx) / lens.fx, (v - lens.cy) / lens.fy, 1.0))
            val t = -pose.centre.z / d.z
            val x = pose.centre.x + d.x * t; val y = pose.centre.y + d.y * t
            var b = 128
            if (textured) {     // squares of three sizes, each a brightness of its own
                for ((size, weight) in listOf(0.11 to 50, 0.037 to 35, 0.013 to 20)) {
                    val i = floor(x / size).toLong(); val j = floor(y / size).toLong()
                    val h = ((i * 73856093L) xor (j * 19349663L) xor (size * 1000).toLong()) and 0xffff
                    b += ((h % 200) - 100).toInt() * weight / 100
                }
            }
            out[v * lens.w + u] = b.coerceIn(0, 255).toByte()
        }
        return out
    }

    @Test fun texturedRockMatchesAndPlainRockDoesnt() {
        val a = TestWorld.look(Vec3(0.0, 0.0, 1.0), Vec3(0.0, 0.0, 0.0), lens)
        val b = TestWorld.look(Vec3(0.2, 0.05, 1.0), Vec3(0.05, 0.0, 0.0), lens)
        val far = TestWorld.look(Vec3(5.0, 0.0, 1.0), Vec3(5.0, 0.0, 0.0), lens)        // the same wall, somewhere else on it
        val fa = Features.of(picture(a), lens.w, lens.h)
        val fb = Features.of(picture(b), lens.w, lens.h)
        assertTrue(fa.size > 300, "corners on textured rock: ${fa.size}")
        val links = Features.links(fa, a, fb, b)
        assertTrue(links > 40, "two views of the same rock share matches: $links")
        val elsewhere = Features.links(fa, a, Features.of(picture(far), lens.w, lens.h), far)
        assertTrue(elsewhere < links / 4, "and views of different rock don't: $elsewhere against $links")
        // matches have to agree with where the tracking says the cameras were: put b somewhere else and few do
        val wrong = TestWorld.look(Vec3(0.0, 0.2, 1.0), Vec3(0.0, 0.05, 0.0), lens)       // moved up, where it moved across
        val disagree = Features.links(fa, a, fb, wrong)
        assertTrue(disagree < links * 0.6, "the tracking disagreeing: $disagree of $links")
        assertTrue(Features.of(picture(a, textured = false), lens.w, lens.h).size < 10, "plain rock has no corners")
        // a picture stored turned: corners placed in the stored picture's axes
        val turned = Features.of(picture(a), lens.w, lens.h, turn = 90)
        assertTrue(turned.xs.zip(fa.ys.toList()).all { (x, y) -> kotlin.math.abs(x - (lens.h - y)) < 1e-3 } && turned.ys.contentEquals(fa.xs))
    }

    @Test fun aSessionWarnsButKeeps() {
        val s = CaptureSession(null, null, "f", "test", "a phone", 0.0)
        s.startOrbit(Vec3(0.0, 0.0, 0.0))
        val lensSmall = lens
        fun shot(pose: CameraPose, textured: Boolean, t: Double) = s.shot(ShotIn(pose, SensorSample(Vec3(0.0, 9.81, 0.0), Vec3.ZERO, t = t),
            ImageStats(1.0, 8.0, 0.0, 0.0, 120.0), 0.002, 80.0, 1.0, true, "a", t, 1.7e9 + t, features = Features.of(picture(pose, textured), lensSmall.w, lensSmall.h), arPoints = 4))
        val first = shot(TestWorld.look(Vec3(0.0, 0.0, 1.0), Vec3(0.0, 0.0, 0.0), lens), true, 1.0)
        assertTrue(first.kept && first.record.blob.feat!!.tried == 0 && first.record.notes.any { "tracking sees few points" in it })
        val second = shot(TestWorld.look(Vec3(0.15, 0.0, 1.0), Vec3(0.05, 0.0, 0.0), lens), true, 2.0)
        val f = second.record.blob.feat!!
        assertTrue(second.kept && f.tried == 1 && f.best!! > 40 && f.to == first.record.name && f.arPts == 4, "$f")
        val plain = shot(TestWorld.look(Vec3(0.3, 0.0, 1.0), Vec3(0.1, 0.0, 0.0), lens), false, 3.0)
        assertTrue(plain.kept && plain.record.notes.any { "little texture" in it }, "${plain.record.notes}: said, and kept all the same")
    }
}
