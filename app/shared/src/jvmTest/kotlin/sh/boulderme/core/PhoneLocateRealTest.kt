package sh.boulderme.core

import java.io.File
import kotlin.test.Test
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.double
import kotlinx.serialization.json.int

/**
 * Photos left out of a real mesh's locate pack, placed against it on the JVM as the phones would: only where
 * LOCATE_REAL names a folder made by a script that wrote frames/ (grey pictures, their lenses and true cameras) and
 * loo.bin. Prints how far each lands from its true camera; fails only if none are placed.
 */
class PhoneLocateRealTest {
    @Test fun realFrames() {
        val dir = System.getenv("LOCATE_REAL")?.let { File(it) } ?: return
        val pack = LocatePack.parse(File(dir, "loo.bin").readBytes())
        var placed = 0
        for (row in Json.parseToJsonElement(File(dir, "frames/frames.json").readText()).jsonArray) {
            val o = row.jsonObject
            fun d(k: String) = o[k]!!.jsonPrimitive.double
            val w = o["w"]!!.jsonPrimitive.int; val h = o["h"]!!.jsonPrimitive.int
            val grey = File(dir, "frames/" + o["name"]!!.jsonPrimitive.content + ".grey").readBytes()
            val lens = Lens(d("fx"), d("fy"), d("cx"), d("cy"), w, h)
            val t0 = System.nanoTime()
            val sift = Sift.of(grey, w, h, max = PhoneLocate.FEATURES)
            val t1 = System.nanoTime()
            val p = PhoneLocate.place(pack, grey, w, h, lens)
            val t2 = System.nanoTime()
            val c = o["c"]!!.jsonArray.map { it.jsonPrimitive.double }
            val off = p?.pose(lens)?.centre?.distance(Vec3(c[0], c[1], c[2]))
            if (p != null) placed++
            println("${o["name"]!!.jsonPrimitive.content} ${w}x$h sift ${sift.size} in ${(t1 - t0) / 1e6} ms; placed ${p?.inliers}/${p?.matched} rms ${p?.rmsPx} off $off in ${(t2 - t1) / 1e6} ms; python ${o["py"]}")
        }
        kotlin.test.assertTrue(placed > 0)
    }
}
