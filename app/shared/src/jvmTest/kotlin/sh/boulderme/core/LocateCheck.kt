package sh.boulderme.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.double
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.awt.image.BufferedImage
import java.io.File
import javax.imageio.ImageIO
import kotlin.math.acos
import kotlin.test.Test

/**
 * A check by hand of lining a real capture's mesh up from frames the server placed: what Locate.fromLocated() makes
 * of them, against the fit from all of the capture's photos. Skipped unless BM_LOCATE_CHECK names folders, ':'
 * between, each with fixture.json ({mesh, answer: the server's, scale_json, frames: [{id, file, ar, k}], truth: {s, R,
 * t, probe}}) and frames/ (the photos, as stored).
 */
class LocateCheck {
    @Test fun gymFrames() {
        val dirs = System.getenv("BM_LOCATE_CHECK")?.split(':') ?: return
        for (dir in dirs.map(::File)) {
            val fx = Json.parseToJsonElement(File(dir, "fixture.json").readText()).jsonObject
            fun JsonObject.list(k: String) = this[k]!!.jsonArray.map { it.jsonPrimitive.double }
            val frames = fx["frames"]!!.jsonArray.map { e ->
                val f = e.jsonObject
                val ar = f["ar"]!!.jsonObject; val k = f["k"]!!.jsonObject
                val img = ImageIO.read(File(dir, "frames/" + f["file"]!!.jsonPrimitive.content))
                val grey = BufferedImage(img.width, img.height, BufferedImage.TYPE_BYTE_GRAY).also { it.graphics.drawImage(img, 0, 0, null) }
                val bytes = (grey.raster.dataBuffer as java.awt.image.DataBufferByte).data
                val p = ar.list("p"); val q = ar.list("q")
                val lens = Lens(k["fx"]!!.jsonPrimitive.double, k["fy"]!!.jsonPrimitive.double, k["cx"]!!.jsonPrimitive.double,
                                k["cy"]!!.jsonPrimitive.double, k["w"]!!.jsonPrimitive.int, k["h"]!!.jsonPrimitive.int)
                LocateFrame(f["id"]!!.jsonPrimitive.int, bytes, img.width, img.height, CameraPose(Vec3(p[0], p[1], p[2]), Quat(q[0], q[1], q[2], q[3]), lens))
            }
            val sized = ServerLocate.sized(frames.take(4)) + ServerLocate.sized(frames.drop(4))
            val located = ServerLocate.located(fx["answer"].toString(), sized)
            val t = fx["truth"]!!.jsonObject
            val s = t["s"]!!.jsonPrimitive.double
            val r = t["R"]!!.jsonArray.map { row -> row.jsonArray.map { it.jsonPrimitive.double } }
            val tt = t.list("t"); val probe = t.list("probe").let { Vec3(it[0], it[1], it[2]) }
            fun truth(v: Vec3) = Vec3(s * (r[0][0] * v.x + r[0][1] * v.y + r[0][2] * v.z) + tt[0],
                                      s * (r[1][0] * v.x + r[1][1] * v.y + r[1][2] * v.z) + tt[1],
                                      s * (r[2][0] * v.x + r[2][1] * v.y + r[2][2] * v.z) + tt[2])
            val scale = fx["scale_json"]!!.jsonPrimitive.double
            println("${fx["mesh"]!!.jsonPrimitive.content}: ${located.size} of ${frames.size} placed by the server " +
                    "(inliers ${located.map { it.inliers }}, rms ${located.map { "%.2f".format(it.rmsPx) }}); truth ${"%.4f".format(s)} m/unit")
            for (n in listOf(1, 2, 3, 4, 6, 8)) {
                val ids = frames.take(n).map { it.id }.toSet()
                for (known in listOf(null, scale)) {
                    val res = Locate.fromLocated(located.filter { it.id in ids }, frames.take(n), known)
                    val fit = res.fit
                    val how = if (fit == null) "no fit: ${res.why}" else {
                        // how far the fit puts the rock (the cameras' middle, and a unit off it each way) from the truth
                        val off = listOf(Vec3.ZERO, Vec3(1.0, 0.0, 0.0), Vec3(0.0, 1.0, 0.0), Vec3(0.0, 0.0, 1.0))
                            .maxOf { d -> fit.apply(probe + d).distance(truth(probe + d)) }
                        val turn = listOf(Vec3(1.0, 0.0, 0.0), Vec3(0.0, 1.0, 0.0), Vec3(0.0, 0.0, 1.0)).maxOf { e ->
                            val a = fit.rot.rotate(e); val b = (truth(probe + e) - truth(probe)).unit()
                            acos((a dot b).coerceIn(-1.0, 1.0)) * 180 / Math.PI
                        }
                        "scale ${"%.4f".format(fit.scale)} (${res.scaleFrom}), turn off ${"%.2f".format(turn)}°, rock off up to ${"%.1f".format(off * 100)} cm; " +
                            "${res.frames} frames, spread ${"%.2f".format(res.turnSpreadDeg)}°, centres ${"%.1f".format(res.centreOffM * 100)} cm"
                    }
                    println("  first $n frames${if (known != null) ", scale known" else ""}: $how")
                }
            }
        }
    }
}
