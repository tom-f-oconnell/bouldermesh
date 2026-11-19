package sh.boulderme.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class ContentsTest {
    private fun shot(i: Int, helped: List<String>, card: Boolean = false) = ShotRecord(
        "cap_k_${i.toString().padStart(3, '0')}.jpg",
        ShotBlob(app = "test", device = "a phone", cap = "k", i = i, t = 0.0, ref = if (card) CaptureSession.CARD else null), true, helped = helped)

    private fun target(id: String, kind: String, prio: Int, climbs: List<String> = emptyList()) =
        Target(id, kind, listOf(0.0, 0.0, 0.0), listOf(0.0, 0.0, 1.0), 0.1, prio, climbs = climbs)

    private val plan = Plan(
        climbs = listOf(PlanClimb("c1", "Alpha", "V2"), PlanClimb("c2", "Beta", null)),
        sets = listOf(PlanSet("s1", "The line", "c1", listOf("h0"))),
        targets = listOf(target("h0", "hold", 3, listOf("c1")), target("h1", "hold", 3, listOf("c2")), target("p0", "patch", 2, listOf("c2")),
            target("p1", "patch", 1), target("p2", "patch", 0), target("v0", "view", 2), target("e0", "edge", 1)))

    private fun record(shots: List<ShotRecord>, mode: String = "plan", detail: RecordDetail? = null) =
        CaptureRecord(id = "k", app = "test", device = "a phone", started = 0.0, ended = 1.0, mode = mode, shots = shots, detail = detail)

    @Test fun groupsPhotosByWhatTheyWereFor() {
        val shots = listOf(shot(1, listOf("p1", "h0")), shot(2, listOf("p0")), shot(3, listOf("h1", "v0")), shot(4, listOf("v0", "p1")),
            shot(5, listOf("e0")), shot(6, listOf("p2")), shot(7, emptyList()), shot(8, emptyList(), card = true))
        val names = shots.map { it.name } + "cap_k_009.jpg"     // one the record doesn't have yet
        val groups = CaptureContents.group(record(shots), plan, names, listOf("scn_k_001.jpg", "pano_k_01.jpg"),
            mapOf("capture.json" to 52_000L, "points.bin" to 2_400_000L))
        val by = groups.associate { it.key to it.photos }
        assertEquals(listOf("climb:c1", "climb:c2", "card", "view", "rock", "ground", "other", "walk", "pano", "readings"), groups.map { it.key })
        assertEquals("Alpha V2", groups[0].title)
        assertEquals(listOf(names[0]), by["climb:c1"], "a hold on a climb counts over the rest of the rock")
        assertEquals(listOf(names[1], names[2]), by["climb:c2"], "rock on a climb, and a hold over a view")
        assertEquals(listOf(names[3]), by["view"]); assertEquals(listOf(names[4]), by["rock"]); assertEquals(listOf(names[5]), by["ground"])
        assertEquals(listOf(names[7]), by["card"]); assertEquals(listOf(names[6], names[8]), by["other"])
        assertTrue(groups.single { it.key == "walk" }.scenery && by["pano"] == listOf("pano_k_01.jpg"))
        assertEquals(listOf("capture.json", "points.bin"), groups.last().files.map { it.first })
        assertTrue(groups.last().files[1].second.endsWith("(2.4 MB)"))
    }

    @Test fun aSetsHoldsInDetailFirst_andOrbitsAndNoPlan() {
        val detail = RecordDetail("s1", listOf(RecordHold("h0")))
        val shots = listOf(shot(1, listOf("h0")), shot(2, listOf("h1")))
        val g = CaptureContents.group(record(shots, detail = detail), plan, shots.map { it.name }, emptyList(), emptyMap())
        assertEquals(listOf("detail", "climb:c2"), g.map { it.key }); assertEquals("The line: its holds in detail", g[0].title)
        // no plan kept: by the targets' kinds alone
        val bare = CaptureContents.group(record(listOf(shot(1, listOf("h1")), shot(2, listOf("v0")), shot(3, listOf("p4")))), null,
            listOf("cap_k_001.jpg", "cap_k_002.jpg", "cap_k_003.jpg"), emptyList(), emptyMap())
        assertEquals(listOf("holds", "view", "rock"), bare.map { it.key })
        val orbit = CaptureContents.group(record(listOf(shot(1, listOf("o3"))), mode = "orbit"), null, listOf("cap_k_001.jpg"), emptyList(), emptyMap())
        assertEquals(listOf("orbit"), orbit.map { it.key })
        assertEquals(listOf("unknown"), CaptureContents.group(null, null, listOf("cap_k_001.jpg"), emptyList(), emptyMap()).map { it.key })
    }

    @Test fun saysHowAnUploadStands() {
        assertEquals("Meshed; size queued. Only you can see it until it's approved" to "good",
            UploadStatus("done", "m1", approved = false, left = listOf("size queued")).said())
        assertEquals("Meshed" to "good", UploadStatus("done", "m1", approved = true).said())
        assertEquals("bad", UploadStatus("done", "m1", meshGone = true).said().second)
        assertEquals("Waiting its turn to be meshed" to "wait", UploadStatus("held").said())
        // made again (as when the mapper changed): it has its mesh meanwhile, to view
        assertEquals("Meshed; waiting its turn to be made again" to "good", UploadStatus("queued", "m1", approved = true).said())
        assertEquals("Meshed; being made again now" to "good", UploadStatus("running", "m1").said())
        assertEquals("m1", UploadStatus("queued", "m1").meshed)
        assertEquals("Waiting its turn to be meshed" to "wait", UploadStatus("queued", "m1", meshGone = true).said())
        assertEquals(null, UploadStatus("queued", "m1", meshGone = true).meshed)
        assertEquals("Meshing failed: too few photos placed" to "bad", UploadStatus("failed", failure = "too few photos placed").said())
        val got = Plan.json.decodeFromString(kotlinx.serialization.builtins.MapSerializer(kotlinx.serialization.serializer<String>(), UploadStatus.serializer()),
            """{"ab": {"status": "done", "mesh": "m1", "approved": true, "left": [], "failure": null, "mesh_gone": false}, "cd": {"status": "gone"}}""")
        assertEquals("m1", got["ab"]!!.mesh); assertEquals("gone", got["cd"]!!.status)
    }

    @Test fun listsTheAccountsCapturesNotOnThePhone() {
        // as the server answers: unknown fields, and an "approved" that's 1 or null
        val got = Plan.json.decodeFromString(AccountProjects.serializer(), """{"projects": [
            {"id": "a1", "name": "gym (capture)", "created": 100.5, "status": "done", "mesh": "m1", "photos": 40, "approved": 1, "bytes": 9},
            {"id": "b2", "name": "gym2 (capture)", "created": 200.0, "status": "queued", "mesh": null, "photos": 12, "approved": null},
            {"id": "c3", "name": "on the phone", "created": 300.0, "status": "done"}], "used": 5, "quota": null}""").projects
        val shown = SentCapture.notOnPhone(got, listOf("c3", null))
        assertEquals(listOf("b2", "a1"), shown.map { it.id })
        assertEquals("gym", shown[1].title); assertEquals("m1", shown[1].mesh); assertEquals(40, shown[1].photos)
        assertEquals(null, shown[0].mesh)
    }
}
