package sh.boulderme.core

import kotlin.math.hypot
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** A gym climb's holds found by their colour: on a made-up wall with holds of a few colours, photographed from in front. */
class GymTest {
    private class Hold(val x: Double, val y: Double, val r: Double, val rgb: Int)
    private val wall = 0xB4B4AF
    private val yellow = 0xF0D020; private val blue = 0x2050C0; private val black = 0x202020
    private val orange = 0xF0A020      // amber: 19 degrees of hue from the yellow
    // the wall is the plane z = 0, facing +z; holds stand 4 cm out from it
    private val holds = listOf(Hold(-0.6, 1.0, 0.06, yellow), Hold(0.0, 1.6, 0.07, yellow), Hold(0.5, 2.3, 0.06, yellow), Hold(-0.2, 2.9, 0.08, yellow),
        Hold(0.3, 1.1, 0.06, blue), Hold(-0.5, 2.0, 0.07, blue), Hold(0.75, 1.7, 0.06, orange), Hold(-1.0, 1.5, 0.07, black))
    private val yellows = holds.filter { it.rgb == yellow }
    private val lens = Lens(500.0, 500.0, 320.0, 240.0, 640, 480)
    private val sharp = ImageStats(1.0, 8.0, 0.0, 0.0, 120.0)

    /** The wall as a camera sees it, at half its picture's size, with a tint over it all (a camera's white balance). */
    private fun picture(pose: CameraPose, tint: Int = 0): Picture {
        val w = lens.w / 2; val h = lens.h / 2
        val rgb = IntArray(w * h)
        for (y in 0 until h) for (x in 0 until w) {
            val d = Gym.rayOf(pose, x * 2.0 + 1, y * 2.0 + 1)
            val t = -pose.centre.z / d.z
            val p = pose.centre + d * t
            val c = holds.firstOrNull { hypot(p.x - it.x, p.y - it.y) <= it.r }?.rgb ?: wall
            rgb[y * w + x] = if (tint == 0) c else (((c shr 16 and 0xff) + tint).coerceIn(0, 255) shl 16) or (c and 0xff00) or ((c and 0xff) - tint).coerceIn(0, 255)
        }
        return Picture.ofRgb(rgb, w, h, step = 2.0)
    }

    /** What the phone's tracking finds: a point on each hold, and the wall every 10 cm but where the holds are. */
    private val cloud: List<Vec3> = holds.map { Vec3(it.x, it.y, 0.04) } +
        (0 until 30).flatMap { i -> (0 until 40).map { j -> Vec3(-1.5 + 0.1 * i, 0.1 * j, 0.0) } }.filter { p -> holds.none { hypot(p.x - it.x, p.y - it.y) < 0.12 } }

    private fun at(x: Double) = TestWorld.look(Vec3(x, 1.5, 2.5), Vec3(x * 0.3, 1.9, 0.0), lens)

    @Test fun patchesOfOneColourAreFoundAndMatched() {
        val pose = at(0.0)
        val blobs = Blobs.find(picture(pose))
        // each hold in view is a patch, where it projects to; the wall isn't one
        for (hd in holds) {
            val p = pose.project(Vec3(hd.x, hd.y, 0.0)) ?: continue
            if (p.u !in 0.0..640.0 || p.v !in 0.0..480.0) continue
            assertTrue(blobs.any { hypot(it.u - p.u, it.v - p.v) < 8 }, "a patch at the hold at ${hd.x}, ${hd.y}: $blobs")
        }
        val first = blobs.minByOrNull { b -> pose.project(Vec3(-0.6, 1.0, 0.0))!!.let { hypot(b.u - it.u, b.v - it.v) } }!!
        val colour = HoldColour(first)
        assertEquals("yellow", colour.name)
        val matched = blobs.filter { colour.matches(it) }
        assertEquals(yellows.size, matched.size, "the yellow holds, not the orange, blue or black: $matched")
        // a warm tint over the whole picture (another white balance) changes nothing: the wall is grey again
        val tinted = Blobs.find(picture(pose, tint = 18)).filter { colour.matches(it) }
        assertEquals(yellows.size, tinted.size)
        // a black hold is found by how dark it is against the wall; grey wall alone never is
        val dark = blobs.minByOrNull { b -> pose.project(Vec3(-1.0, 1.5, 0.0))!!.let { hypot(b.u - it.u, b.v - it.v) } }!!
        assertEquals("black", HoldColour(dark).name)
        assertEquals(1, blobs.count { HoldColour(dark).matches(it) })
    }

    @Test fun aPictureFromTheCamerasYuvIsTurnedAsThePhotoIs() {
        // a picture 64 wide and 48 high, read out sideways, with a yellow square near its top left
        val w = 64; val h = 48
        val y = ByteArray(w * h); val uv = ByteArray(w * h / 2)
        fun yuv(rgb: Int): Triple<Int, Int, Int> {
            val r = rgb shr 16 and 0xff; val g = rgb shr 8 and 0xff; val b = rgb and 0xff
            val yy = 0.299 * r + 0.587 * g + 0.114 * b
            return Triple(yy.toInt(), ((b - yy) / 1.772 + 128).toInt(), ((r - yy) / 1.402 + 128).toInt())
        }
        for (row in 0 until h) for (col in 0 until w) {
            val c = if (col in 4..11 && row in 4..11) yellow else wall
            val (yy, u, v) = yuv(c)
            y[row * w + col] = yy.toByte()
            if (row % 2 == 0 && col % 2 == 0) { uv[(row / 2) * w + col] = v.toByte(); uv[(row / 2) * w + col + 1] = u.toByte() }      // NV21: V first
        }
        val p = Picture.ofYuv(y, w, uv, 0, w, w, h, vFirst = true, turn = 90, side = 64)
        assertEquals(48, p.w); assertEquals(64, p.h)
        val blob = assertNotNull(Blobs.find(p).singleOrNull(), "the square, alone")
        assertEquals("yellow", HoldColour(blob).name)
        // turned a quarter clockwise, the top left goes to the top right
        assertTrue(blob.u > 36 && blob.v < 12, "$blob")
    }

    @Test fun holdsOfTheColourArePlacedFromThePhotos() {
        val gym = Gym()
        val first = at(0.0)
        val tap = first.project(Vec3(-0.6, 1.0, 0.0))!!
        assertNotNull(gym.pick(picture(first), first, tap.u, tap.v, Vec3(-0.6, 1.0, 0.04), 1, add = false))
        assertEquals(1, gym.holds.size, "the one tapped, at once")
        // a tap on the wall itself picks nothing
        assertNull(Gym().pick(picture(first), first, 600.0, 470.0, null, 1, add = false))
        for ((i, x) in listOf(-0.8, -0.3, 0.3, 0.8).withIndex()) { val pose = at(x); gym.addPhoto("p$i", pose, 1, Blobs.find(picture(pose)), null, cloud) }
        assertEquals(yellows.size, gym.holds.size, "${gym.holds.map { it.p }}")
        for (hd in yellows) assertTrue(gym.holds.any { it.p.distance(Vec3(hd.x, hd.y, 0.04)) < 0.05 }, "a hold at ${hd.x}, ${hd.y}")
        assertTrue(gym.holds.all { it.rM in 0.04..0.12 }, "about their size: ${gym.holds.map { it.rM }}")
        // ids stay with their holds as more photos come
        val ids = gym.holds.associate { it.id to it.p }
        gym.addPhoto("p9", at(0.1), 1, Blobs.find(picture(at(0.1))), null, cloud)
        assertTrue(gym.holds.all { h -> ids[h.id]?.let { it.distance(h.p) < 0.03 } == true }, "the same ids")
        // narrowed, the colour still has its holds; widened a lot, orange counts too
        gym.narrow(); assertEquals(yellows.size, gym.holds.size)
        repeat(4) { gym.widen() }
        assertTrue(gym.holds.size > yellows.size, "the amber too, taken widely")
        repeat(2) { gym.narrow() }
        assertEquals(yellows.size, gym.holds.size)
        // one left out stays out
        val top = gym.holds.maxByOrNull { it.p.y }!!
        val eye = Vec3(0.0, 1.5, 2.5)
        assertEquals(top.id, gym.drop(eye, (top.p - eye).unit()))
        assertEquals(yellows.size - 1, gym.holds.size)
        assertNull(gym.drop(eye, (Vec3(1.4, 0.2, 0.0) - eye).unit()), "nothing near")
        // the record has its colour and holds
        val r = assertNotNull(gym.record())
        assertTrue(r.name == "yellow" && r.holds.size == yellows.size - 1 && r.rgb.startsWith("#") && r.colourBlobs > 0, "$r")
    }

    @Test fun holdsWithNoTrackedPointsAreFoundWhereThreePhotosAgree() {
        val gym = Gym()
        val first = at(0.0)
        val tap = first.project(Vec3(0.0, 1.6, 0.0))!!
        gym.pick(picture(first), first, tap.u, tap.v, null, 1, add = false)
        for ((i, x) in listOf(-0.9, -0.3, 0.3, 0.9).withIndex()) { val pose = at(x); gym.addPhoto("p$i", pose, 1, Blobs.find(picture(pose)), null, emptyList()) }
        for (hd in yellows) assertTrue(gym.holds.any { it.p.distance(Vec3(hd.x, hd.y, 0.0)) < 0.06 }, "a hold at ${hd.x}, ${hd.y}: ${gym.holds.map { it.p }}")
        assertEquals(yellows.size, gym.holds.size)
    }

    @Test fun aModelFindsHoldsTheWallsOwnColour() {
        // a blue hold on a blue wall: one patch with the wall to the colour finder, too big to be a hold
        val w = 80; val h = 60
        fun inHold(x: Int, y: Int) = hypot(x - 40.0, y - 30.0) <= 6
        val coarse = Picture.ofRgb(IntArray(w * h) { blue }, w, h, step = 4.0)
        assertEquals(0, Blobs.find(coarse).size)
        // the model says where the hold is: a patch of it alone, a hold whatever its colour
        assertEquals(3 * w * h, HoldModel.input(coarse).size)
        val seg = HoldSegmenter { _, fw, fh -> FloatArray(3 * fw * fh) { i -> val k = i / (fw * fh); val px = i % (fw * fh)
            if (k == HoldModel.HOLD) (if (inHold(px % fw, px / fw)) 2f else -2f) else 0f } }
        val found = assertNotNull(HoldModel.blobs(coarse, seg)).single()
        assertTrue(found.src == "model" && hypot(found.u - 40.5 * 4, found.v - 30.5 * 4) < 8 && found.areaPx in 80.0 * 16..140.0 * 16, "$found")
        assertNull(HoldModel.blobs(coarse) { _, _, _ -> null }, "a model that couldn't run")
    }

    @Test fun aHoldOutOfReachAsksForWhatAPhoneHeldUpCanDo() {
        val gym = Gym()
        gym.addPhoto("p", at(0.0), 1, emptyList(), null, emptyList())      // held at 1.5 m: the floor is about at 0
        assertEquals(Orbit.SPOT_GSD_M, gym.bestGsdM(Vec3(0.0, 1.0, 0.0), 1500.0))
        val high = gym.bestGsdM(Vec3(0.0, 4.2, 0.0), 1500.0)
        assertTrue(high > 3 * Orbit.SPOT_GSD_M && gym.high(Vec3(0.0, 4.2, 0.0)) && !gym.high(Vec3(0.0, 1.8, 0.0)), "$high")
    }

    @Test fun aWallIsWalkedAlongNotRound() {
        val o = Orbit(Vec3(0.0, 1.0, 0.0))
        o.blockBehind(Vec3(0.0, 1.5, 3.0))
        assertEquals(o.sectors, o.ring.size)
        assertTrue(!o.ring[o.sectorOf(Vec3(0.0, 1.0, 3.0))].blocked && !o.ring[o.sectorOf(Vec3(2.0, 1.0, 2.0))].blocked, "in front: open")
        assertTrue(o.ring[o.sectorOf(Vec3(0.0, 1.0, -3.0))].blocked && o.ring[o.sectorOf(Vec3(3.0, 1.0, -0.5))].blocked, "behind and beside: out of reach")
    }

    @Test fun aGymCaptureFindsItsHoldsAndAsksForThem() {
        val flow = CaptureFlow(CaptureSession(null, null, "g1", "test", "a phone", 0.0), gym = true)
        flow.skipLevel()
        var t = 0.0
        val first = at(0.0)
        assertTrue("Tap a hold" in flow.frame(first, true, 0.0, 0.004, t, 0).ui.text)
        for (p in cloud) flow.addPoint(p.x, p.y, p.z)
        // a tap on the wall: no colour there
        val wallSpot = Vec3(1.2, 0.3, 0.0)
        flow.tap(first.centre, (wallSpot - first.centre).unit(), { picture(first) to first }) { wallSpot }
        assertEquals(Phase.CENTRE, flow.phase)
        val spot = Vec3(-0.6, 1.0, 0.04)
        flow.tap(first.centre, (spot - first.centre).unit(), { picture(first) to first }) { spot }
        assertEquals(Phase.CAPTURE, flow.phase)
        var ui = flow.frame(first, true, 0.0, 0.004, t, 0).ui
        assertTrue(ui.gym?.name == "yellow" && ui.gym?.holds == 1, "${ui.gym}")
        fun take(pose: CameraPose) {
            flow.shutter()
            assertTrue((0 until 3).any { t += 0.5; flow.frame(pose, true, 0.0, 0.004, t, 0).shoot })
            flow.shot(ShotIn(pose, SensorSample(Vec3(0.0, 9.81, 0.0), Vec3.ZERO, magAccuracy = 0, t = t), sharp, 0.004, 80.0, 1.3, true, "a", t, 1.7e9 + t, blobs = Blobs.find(picture(pose))))
        }
        for (x in listOf(-0.8, -0.3, 0.3, 0.8)) take(at(x))
        ui = flow.frame(at(0.0), true, 0.0, 0.004, t + 0.1, 0).ui
        assertEquals(yellows.size, ui.gym?.holds)
        assertTrue("compass" !in ui.sensors, "a gym capture doesn't ask for the compass to be calibrated: ${ui.sensors}")
        val spots = flow.session.orbit!!.spots.filter { it.gym }
        assertEquals(yellows.size, spots.size)
        val markers = flow.session.markers().filter { it.rgb != null }
        assertTrue(markers.size == yellows.size && markers.all { it.rgb == flow.session.gym!!.colour!!.rgb }, "drawn in the climb's colour")
        // it asks for them close up, and one can be skipped
        val close = TestWorld.look(Vec3(0.0, 1.6, 0.6), Vec3(0.0, 1.6, 0.0), lens)
        ui = flow.frame(close, true, 0.0, 0.004, t + 0.2, 0).ui
        assertTrue("yellow hold" in ui.text && ui.canSkip, ui.text)
        flow.skip()
        assertTrue(flow.session.orbit!!.spots.count { it.skipped } == 1)
        // and they're sent as holds marked on the phone, with the climb's colour
        val record = flow.session.record(99.0)
        assertTrue(record.gym?.name == "yellow" && record.marked.count { it.id.startsWith("g") } == yellows.size, "${record.marked}")
        assertTrue("\"e\":\"gym_photo\"" in flow.logText() && "\"e\":\"gym_pick\"" in flow.logText())
    }
}
