package sh.boulderme.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class ServerLocateTest {
    private val lens = Lens(1000.0, 1000.0, 1280.0, 960.0, 2560, 1920)

    private fun frame(id: Int, w: Int, h: Int) =
        LocateFrame(id, ByteArray(w * h) { ((it % w + it / w) % 256).toByte() }, w, h, CameraPose(Vec3.ZERO, Quat.IDENTITY, lens.scaledTo(w, h)))

    @Test fun framesAreShrunkWithTheirLens() {
        val s = ServerLocate.sized(listOf(frame(1, 2560, 1920), frame(2, 640, 480)) + (3..6).map { frame(it, 64, 48) })
        assertEquals(ServerLocate.MAX_FRAMES, s.size)
        assertEquals(1280 to 960, s[0].w to s[0].h)
        assertEquals(Lens(500.0, 500.0, 640.0, 480.0, 1280, 960), s[0].lens)
        assertEquals(1280 * 960, s[0].grey.size)
        assertEquals(640 to 480, s[1].w to s[1].h, "small enough already")
        // the mean of each 2x2: (x + y) of its top left, plus one
        assertEquals((2 * 10 + 2 * 7 + 1) % 256, s[0].grey[7 * 1280 + 10].toInt() and 0xff)
    }

    @Test fun theRequestIsAsTheServerTakesIt() {
        val s = ServerLocate.sized(listOf(frame(7, 640, 480)))
        val body = Json.parseToJsonElement(ServerLocate.request(s, listOf(byteArrayOf(-1, -40, 1, 2)))).jsonObject
        val f = body["frames"]!!.jsonArray[0].jsonObject
        assertEquals(7, f["id"]!!.jsonPrimitive.int)
        assertEquals("/9gBAg==", f["jpeg_b64"]!!.jsonPrimitive.content)
        assertEquals(640, f["lens"]!!.jsonObject["w"]!!.jsonPrimitive.int)
        assertEquals("250.0", f["lens"]!!.jsonObject["fx"]!!.jsonPrimitive.content)
    }

    @Test fun theAnswerIsTheCameraInTheMesh() {
        // backend/locateapi.pose_answer() of COLMAP's R (130° about (1, 2, 3)) and t for a camera at (1, -2, 3); the
        // point below is at (0.3, -0.2, 5) in that camera: (335, 230) in its picture (fx 250), by COLMAP's x = R X + t
        val s = ServerLocate.sized(listOf(frame(3, 640, 480)))
        val answer = """{"located": [{"id": 3, "c": [1.0, -2.0, 3.0], "q": [0.42261826, -0.24222094, -0.48444189, -0.72666283],
            "inliers": 40, "rms_px": 1.25, "how": "phone"}, {"id": 9, "c": [0, 0, 0], "q": [1, 0, 0, 0], "inliers": 99, "rms_px": 1}],
            "ms": 210}"""
        val got = ServerLocate.located(answer, s)
        assertEquals(1, got.size, "a frame that wasn't sent is left out")
        val l = got[0]
        assertEquals(3, l.id); assertEquals(40, l.inliers); assertEquals("server", l.by)
        val p = l.mesh.project(Vec3(0.3853788158712479, 2.4647576469623935, 5.195035296734652))!!
        assertTrue(kotlin.math.abs(p.u - 335.0) < 1e-4 && kotlin.math.abs(p.v - 230.0) < 1e-4, "$p")
        assertEquals(emptyList(), ServerLocate.located("<html>busy</html>", s))
        assertEquals(emptyList(), ServerLocate.located("""{"error": "the server is placing others' frames", "retry_s": 2}""", s))
    }
}
