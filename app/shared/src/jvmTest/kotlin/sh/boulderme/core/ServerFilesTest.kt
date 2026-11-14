package sh.boulderme.core

import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

/**
 * The files that pass between the server and the apps, each as the other side really makes it: a plan and guide the
 * server's own test made (tests/test_capture.py --write-fixtures), and a photo's readings and a capture's record as
 * written here, which that test reads. Either side changing its format fails the other's test.
 */
class ServerFilesTest {
    private fun bytes(name: String) = checkNotNull(javaClass.getResourceAsStream("/$name")) { "$name is missing" }.readBytes()

    @Test fun readsWhatTheServerMakes() {
        val plan = Plan.parse(bytes("server/plan.json").decodeToString())
        val guide = GuideMesh.parse(bytes("server/guide.bin"))
        assertEquals(listOf(plan.guide.vertices, plan.guide.faces, plan.guide.patches), listOf(guide.vertices, guide.faces, guide.patches))
        assertEquals("A Wall", plan.name); assertEquals("cacacacacaca", plan.mesh); assertEquals(34.0, plan.lat)
        assertEquals(1.2, plan.frame.metresPerUnit); assertFalse(plan.frame.scaleGuessed)
        assertEquals(0.5, plan.needOf("hold").gsdMm); assertEquals(4, plan.needOf("hold").views)
        val holds = plan.targets.filter { it.isHold }
        assertEquals(2, holds.size)
        assertTrue(holds.all { it.prio == 3 && it.climbs == listOf("c1") && it.haveGsd != null && it.haveViews >= 2 && it.role != null })
        assertTrue(plan.targets.any { it.isEdge && it.beyond != null })
        assertEquals(guide.patches, plan.targets.count { it.kind == "patch" }, "a target for each of the guide's patches")
        assertTrue(plan.cameras.size == 9 && plan.climbs.single().name == "Alpha")
        assertTrue(plan.v == 3 && plan.key == "0123456789ab" && plan.needOf("hold").ladder == 2.0 && plan.bridge == Bridge(1.0, 20.0))
        // its sets of beta, what its roles are drawn in, and what a capture for a set asks of its holds
        assertEquals(listOf(PlanSet("s1", "The line", "c1", listOf("h0")), PlanSet("s2", "Empty", null, emptyList())), plan.sets)
        assertTrue(plan.roles["start"] == "#19d23c" && plan.roles.size == 4)
        assertEquals(Need(null, 6, 2.0, closeM = 0.15, viewDeg = 20.0, sidesDeg = 40.0, ties = 3), plan.needOf("detail"))
        assertEquals(Need(2.0, 1, facingDeg = 35.0), plan.needOf("texture"))
        assertTrue(holds[0].sets == listOf("s1") && holds[0].holds == listOf("0a0b0c0d") && holds[0].detailM == 0.12 && holds[1].sets.isEmpty() && holds[1].detailM == 0.075)
        val view = plan.targets.single { it.isView }
        assertTrue(view.from == "cap_004.jpg" && view.prio == 2 && view.normal.z < -0.8, "where one of its photos was taken, and the way it looked: $view")

        // and it can be captured against: lined up by two taps, asked what to do, and a photo counted
        val s = CaptureSession(plan, guide, "t", "test", "a phone", 0.0)
        val truth = Similarity(1.2, Quat.yaw(0.4), Vec3(2.0, 0.0, -3.0))
        val a = Vec3(-1.0, 0.5, 1.7); val b = Vec3(1.0, 2.5, 1.7)
        s.tap(a, truth.apply(a)); assertNotNull(s.tap(b, truth.apply(b)))
        val cov = assertNotNull(s.coverage)
        assertEquals(guide.patches, cov.patchLevels().size)
        assertTrue(cov.progress().let { it.total > 2 && it.holds == 2 && it.holdsDone == 0 }, "${cov.progress()}: its holds were seen, but not from close enough")
        val hold = holds.first()
        val world = truth.apply(hold.point); val out = truth.dir(hold.normal)
        val lens = Lens(1450.0, 1450.0, 960.0, 540.0, 1920, 1080)
        val pose = TestWorld.look(world + out * 0.5, world, lens)
        val f = s.frame(FrameIn(pose, 0.0, 0.002, true, 1.0, 90))
        assertTrue(f.hint.targetId != null && f.progress.total > 0, f.hint.text)
        val shot = s.shot(ShotIn(pose, SensorSample(Vec3(0.0, 9.81, 0.0), Vec3.ZERO, t = 1.0), ImageStats(1.0, 8.0, 0.0, 0.0, 120.0), 0.002, 80.0, 0.5, true, "a", 1.0, 1.7e9))
        assertTrue(shot.kept && hold.id in shot.helped, "${shot.why} ${shot.helped}")
    }

    /** A capture for a set of beta: its holds in detail (close, from every side, tied in from further off), the other holds left, and the rest for its texture. */
    @Test fun aSetOfBetaInDetail() {
        val plan = Plan.parse(bytes("server/plan.json").decodeToString())
        val guide = GuideMesh.parse(bytes("server/guide.bin"))
        val cov = Coverage(plan, guide)
        cov.scope = Scope(minPrio = 1, set = "s1")
        val h = cov.state("h0")!!; val other = cov.state("h1")!!
        assertTrue(cov.detail(h) && cov.inScope(h) && cov.inScope(other) && !cov.detail(other), "its holds in detail, and every other hold as a hold")
        assertTrue(TestWorld.near(cov.needGsdM(other)!!, 0.0005, 1e-9) && cov.needViews(other) == 4)
        cov.scope = Scope(minPrio = 3, set = "s1"); assertTrue(cov.inScope(h) && !cov.inScope(other), "or only the set's holds, when that's what's asked")
        cov.scope = Scope(minPrio = 1, set = "s1")
        // its region (12 cm round it) fills the picture: further off than the lens's nearest, and about a tenth of a millimetre a pixel
        val close = cov.closeM(h)!!
        assertTrue(TestWorld.near(close, 0.12 * 1.1 / 0.5, 1e-6) && TestWorld.near(cov.needGsdM(h)!!, close / 3000, 1e-9), "$close")
        assertEquals(setOf(0, 1, 2, 3, 4), cov.sidesWanted(h)); assertEquals(1..3, cov.tiesWanted(h))
        val p = h.target.point; val mpu = cov.metresPerUnit
        fun from(side: Int, k: Double = 0.95) = TestWorld.look(p + (cov.sideSpot(h, side) - p) * k, p)
        val far = TestWorld.look(p + h.target.normal * (2.0 / mpu), p)
        assertEquals(Hint.Kind.SHOOT, cov.hint(far, h).kind, "a step of its ladder, on the way in: ${cov.hint(far, h).text}")
        cov.addShot(far)
        assertTrue(cov.hint(far, h).let { it.kind == Hint.Kind.CLOSER && "fill the picture" in it.text })
        assertEquals(Hint.Kind.FACE, cov.hint(from(Coverage.SIDE_LEFT), h).kind, "straight on first")
        assertEquals(Hint.Kind.BACK, cov.hint(from(0, 0.5), h).kind)
        assertEquals(Hint.Kind.SHOOT, cov.hint(from(0), h).kind)
        cov.addShot(from(0))
        val next = cov.hint(from(0), h)
        assertTrue(next.kind == Hint.Kind.ANGLE && "from" in next.text, next.text)
        for (side in 1..4) { assertEquals(Hint.Kind.SHOOT, cov.hint(from(side), h).kind, "side $side"); cov.addShot(from(side)) }
        assertTrue(h.sides.size == 5 && !cov.done(h) && cov.goodViews(h) == 5)
        // a sixth angle, between the others
        val between = TestWorld.look(p + ((cov.sideSpot(h, 1) - p) + (cov.sideSpot(h, 3) - p)).unit() * (close * 0.9 / mpu), p)
        assertEquals(Hint.Kind.SHOOT, cov.hint(between, h).kind, cov.hint(between, h).text); cov.addShot(between)
        // then the photos from further off that tie the close ones in
        val back = cov.hint(from(0), h)
        assertTrue(back.kind == Hint.Kind.BACK && "ties" in back.text && !cov.done(h), back.text)
        for (k in 1..3) cov.addShot(TestWorld.look(p + h.target.normal * (close * Math.pow(2.0, k.toDouble()) / mpu), p))
        assertTrue(cov.done(h), cov.status(h))
        val got = cov.reached().single()
        assertTrue(got.done && got.ids == listOf("0a0b0c0d") && got.rM == 0.12 && got.sides.size == 5 && got.ties == 3 && got.gsdMm!! < 0.1, "$got")
        // a phone that turns out not to focus that near: asked for from further off
        cov.backOff(); assertTrue(TestWorld.near(cov.closeM(h)!!, close * 1.2, 1e-9))
        // rock on no climb: one photo of the capture's own, nearly square on, at 2 mm a pixel, whatever its earlier photos saw
        val rock = cov.states.first { it.target.kind == "patch" && it.target.prio == 1 && it.target.haveViews > 0 && (it.target.p[1] - plan.frame.groundY) * mpu < 2.0 }
        assertTrue(cov.inScope(rock) && !cov.done(rock) && cov.needViews(rock) == 1)
        val q = rock.target.point
        val slant = TestWorld.look(q + (rock.target.normal + Vec3(0.0, 1.0, 0.0).cross(rock.target.normal).unit() * 1.2).unit() * (3.0 / mpu), q)
        cov.addShot(slant); assertFalse(rock.textured, "too far round to texture it with")
        cov.addShot(TestWorld.look(q + rock.target.normal * (3.0 / mpu), q)); assertTrue(rock.textured && cov.done(rock))
    }

    @Test fun writesWhatTheServerReads() {
        val blob = ShotBlob(app = "test 0.1", device = "a phone", cap = "k1", i = 1, t = 1.7e9, down = listOf(0.0, 0.98, 0.2), downSrc = "ar",
            heading = 123.4, gps = BlobGps(34.0, -116.0, 1200.0, 3.0, 0.5), ar = BlobAr("a", listOf(1.0, 2.0, 3.0), listOf(1.0, 0.0, 0.0, 0.0)),
            k = BlobLens(3000.0, 3000.0, 2000.0, 1500.0, 4000, 3000), exp = BlobExposure(0.004, 80.0, 2.0), q = BlobQuality(1.0, 0.2, 0.0, 0.0, true))
        val record = CaptureRecord(id = "k1", app = "test 0.1", device = "a phone", mesh = "cacacacacaca", started = 1.0, ended = 2.0, mode = "plan",
            shots = listOf(ShotRecord("cap_k1_001.jpg", blob, true)))
        same("app/shot.txt", blob.userComment())
        val odd = blob.copy(device = "Téléphone 📱").userComment()
        assertTrue(odd.all { it.code < 128 } && "T\\u00e9l\\u00e9phone \\ud83d\\udcf1" in odd, odd)
        assertEquals("Téléphone 📱", Plan.json.decodeFromString(ShotBlob.serializer(), odd.removePrefix(ShotBlob.MARK)).device, "and it reads back as it was")
        same("app/capture.json", record.json())
    }

    /** That text is what the kept file has; with BM_WRITE_FIXTURES set, the file is written first (after a format change meant). */
    private fun same(name: String, text: String) {
        val f = File("src/jvmTest/resources/$name")
        if (System.getenv("BM_WRITE_FIXTURES") != null) { f.parentFile.mkdirs(); f.writeText(text) }
        assertEquals(f.readText(), text, "$name isn't what the server's test reads: if the format was meant to change, run with BM_WRITE_FIXTURES=1 and change backend/phonedata.py")
    }
}
