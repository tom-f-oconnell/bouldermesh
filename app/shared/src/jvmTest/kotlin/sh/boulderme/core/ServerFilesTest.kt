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
