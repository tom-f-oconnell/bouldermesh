package sh.boulderme.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.double
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.awt.Polygon
import java.awt.image.BufferedImage
import java.io.File
import javax.imageio.ImageIO
import kotlin.test.Test

/**
 * A check by hand of the finders on photos whose holds are marked by colour (pwals' bouldering-holds, its valid and
 * test splits, which the model wasn't trained on): for each colour in a photo, its biggest hold is "tapped", and the
 * patches of that colour found are scored against the photo's holds of it. A hold is found if a patch's middle is in
 * it; a patch is wrong if its middle is in no hold of the colour. Skipped unless BM_FINDER_CHECK names the dataset's
 * folder (holding valid/ and test/); BM_GYM_MODEL=<holds.onnx> scores the model's finder (B) beside the colour finder (A).
 */
class FinderCheck {
    private class Hold(val colour: String, val shape: Polygon, val area: Double)

    @Test fun markedPhotos() {
        val root = File(System.getenv("BM_FINDER_CHECK") ?: return)
        val seg = System.getenv("BM_GYM_MODEL")?.let { OnnxSegmenter(File(it).readBytes()) }
        // per finder: holds found, holds in all, patches right, patches in all
        val score = HashMap<String, IntArray>()
        val perColour = HashMap<String, IntArray>()
        for (split in listOf("valid", "test")) {
            val dir = File(root, split)
            val j = Json.parseToJsonElement(File(dir, "_annotations.coco.json").readText()).jsonObject
            val cats = j["categories"]!!.jsonArray.associate { it.jsonObject["id"]!!.jsonPrimitive.int to it.jsonObject["name"]!!.jsonPrimitive.content }
            val anns = j["annotations"]!!.jsonArray.map { it.jsonObject }.groupBy { it["image_id"]!!.jsonPrimitive.int }
            for (im in j["images"]!!.jsonArray.map { it.jsonObject }) {
                val img = ImageIO.read(File(dir, im["file_name"]!!.jsonPrimitive.content))
                val holds = anns[im["id"]!!.jsonPrimitive.int].orEmpty().mapNotNull { a ->
                    val poly = a["segmentation"]?.jsonArray?.firstOrNull()?.jsonArray?.map { it.jsonPrimitive.double } ?: return@mapNotNull null
                    val p = Polygon(); for (k in poly.indices step 2) p.addPoint(poly[k].toInt(), poly[k + 1].toInt())
                    Hold(cats[a["category_id"]!!.jsonPrimitive.int]!!.removeSuffix("-hold"), p, a["area"]?.jsonPrimitive?.double ?: 0.0)
                }
                fun small(side: Int): Picture {
                    val k = maxOf(1, (maxOf(img.width, img.height) + side - 1) / side)
                    val w = img.width / k; val h = img.height / k
                    val s = BufferedImage(w, h, BufferedImage.TYPE_INT_RGB).also { it.createGraphics().apply {
                        setRenderingHint(java.awt.RenderingHints.KEY_INTERPOLATION, java.awt.RenderingHints.VALUE_INTERPOLATION_BILINEAR); drawImage(img, 0, 0, w, h, null); dispose() } }
                    return Picture.ofRgb(s.getRGB(0, 0, w, h, null, 0, w).map { it and 0xffffff }.toIntArray(), w, h, step = k.toDouble())
                }
                val p = small(Picture.SIDE)
                val finders = linkedMapOf("A colour" to Blobs.find(p))
                if (seg != null) finders["B model"] = HoldModel.blobs(p, seg)!!
                for ((name, blobs) in finders) for (colour in holds.map { it.colour }.toSet()) {
                    val mine = holds.filter { it.colour == colour }
                    if (mine.size < 3) continue
                    val tapped = mine.maxByOrNull { it.area }!!
                    val seed = blobs.filter { tapped.shape.contains(it.u, it.v) }.maxByOrNull { it.areaPx } ?: continue
                    val c = HoldColour(seed)
                    val matched = blobs.filter { c.matches(it) }
                    val found = mine.count { h -> matched.any { h.shape.contains(it.u, it.v) } }
                    val right = matched.count { b -> mine.any { it.shape.contains(b.u, b.v) } }
                    for ((key, m) in listOf(name to score, "$name $colour" to perColour))
                        m.getOrPut(key) { IntArray(4) }.let { it[0] += found; it[1] += mine.size; it[2] += right; it[3] += matched.size }
                }
            }
        }
        fun say(k: String, s: IntArray) = println("$k: found ${s[0]} of ${s[1]} holds (${100 * s[0] / maxOf(1, s[1])}%), " +
            "${s[2]} of ${s[3]} patches right (${100 * s[2] / maxOf(1, s[3])}%)")
        for ((k, s) in score) say(k, s)
        for ((k, s) in perColour.toSortedMap()) say("  $k", s)
    }
}
