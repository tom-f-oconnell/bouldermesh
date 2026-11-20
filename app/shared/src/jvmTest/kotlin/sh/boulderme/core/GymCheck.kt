package sh.boulderme.core

import java.awt.BasicStroke
import java.awt.Color
import java.awt.image.BufferedImage
import java.io.File
import javax.imageio.ImageIO
import kotlin.test.Test

/**
 * A check by hand of finding a gym climb's holds by their colour in a real capture's photos: its kept photos replayed
 * through Gym as the apps would (each photo's patches, put in the world by the capture's tracked points), from a hold
 * picked in one of them. Skipped unless BM_GYM_CHECK is set: "<project folder>|<photo>|<u>|<v>|<out folder>", the
 * project as the server keeps it (capture.json, capture-points.bin, photos/), u and v in the stored photo's pixels.
 * Writes each photo with its patches of the colour boxed and the holds found ringed, and prints what was found.
 * BM_GYM_MODEL=<holds.onnx> runs the hold-finding model on each photo too (ONNX Runtime, as the Android app does), and
 * its patches count (B) rather than the colour finder's (A).
 */
class GymCheck {
    @Test fun realCapture() {
        val (proj, pickName, pu, pv, outName) = System.getenv("BM_GYM_CHECK")?.split('|') ?: return
        val dir = File(proj); val out = File(outName).apply { mkdirs() }
        val rec = Reopen.record(File(dir, "capture.json").readText())!!
        val points = readPoints(File(dir, "capture-points.bin").readBytes())
        val seg = System.getenv("BM_GYM_MODEL")?.let { OnnxSegmenter(File(it).readBytes()) }
        val masks = seg != null
        fun picture(name: String): Pair<Picture, BufferedImage>? {
            val f = File(dir, "photos/$name").takeIf { it.exists() } ?: return null
            val img = ImageIO.read(f)
            val k = maxOf(1, (maxOf(img.width, img.height) + Picture.SIDE - 1) / Picture.SIDE)
            val w = img.width / k; val h = img.height / k
            val small = BufferedImage(w, h, BufferedImage.TYPE_INT_RGB).also { it.createGraphics().apply {
                setRenderingHint(java.awt.RenderingHints.KEY_INTERPOLATION, java.awt.RenderingHints.VALUE_INTERPOLATION_BILINEAR); drawImage(img, 0, 0, w, h, null); dispose() } }
            return Picture.ofRgb(small.getRGB(0, 0, w, h, null, 0, w).map { it and 0xffffff }.toIntArray(), w, h, step = k.toDouble()) to img
        }
        fun blobs(name: String, p: Picture): List<Blob> = if (seg == null) Blobs.find(p) else HoldModel.blobs(p, seg)!!
        val shots = rec.shots.filter { it.kept && it.blob.ar != null && it.blob.k != null && it.blob.ref == null }
        fun pose(s: ShotRecord): CameraPose { val ar = s.blob.ar!!; val k = s.blob.k!!; return CameraPose(Vec3.of(ar.p), Quat.of(ar.q), Lens(k.fx, k.fy, k.cx, k.cy, k.w, k.h)) }
        val gym = Gym()
        val pick = shots.first { it.name == pickName }
        val (pp, _) = picture(pick.name)!!
        val spell = spellNumber(pick.blob.ar!!.s)
        val got = gym.pick(pp, pose(pick), pu.toDouble(), pv.toDouble(), null, spell, add = false)
        println("picked ${got?.let { "%.0f %.0f %.0f".format(it.l, it.a, it.b) }}: ${gym.colour?.name}")
        val found = HashMap<String, List<Blob>>()
        val cloud = points.filter { it.second == spell }.map { it.first }
        for (s in shots) {
            if (spellNumber(s.blob.ar!!.s) != spell) continue
            val (p, _) = picture(s.name) ?: continue
            val all = blobs(s.name, p)
            gym.addPhoto(s.name, pose(s), spell, if (masks) emptyList() else all, if (masks) all else null, cloud)
            found[s.name] = all.filter { gym.colour!!.matches(it) }
        }
        if (masks) gym.useFinder(Gym.FINDER_MODEL)
        println("${shots.size} photos, ${cloud.size} points in spell $spell: ${gym.holds.size} ${gym.colour?.name} holds " +
            gym.holds.joinToString { "${it.id} (${it.photos} photos, ${"%.0f".format(it.rM * 100)} cm)" })
        // how many of the photos each hold is in view of found a patch of the colour there: a ghost (rays of different
        // holds meeting) is in view of many more than found it
        for (h in gym.holds) {
            var seen = 0; var inView = 0
            for (s in shots) {
                val pz = pose(s); val at = pz.project(h.p) ?: continue
                if (at.depth < 0.2 || at.u !in 0.0..pz.lens.w.toDouble() || at.v !in 0.0..pz.lens.h.toDouble()) continue
                inView++
                if (found[s.name].orEmpty().any { b -> kotlin.math.hypot(b.u - at.u, b.v - at.v) < maxOf(b.radiusPx * 2, 25.0) }) seen++
            }
            println("  ${h.id}: found in $seen of the $inView photos it's in view of, y ${"%.2f".format(h.p.y)}")
        }
        for (s in shots) {
            val (_, img) = picture(s.name) ?: continue
            val g = img.createGraphics(); g.stroke = BasicStroke(5f)
            g.color = Color.MAGENTA
            for (b in found[s.name].orEmpty()) g.drawRect(b.x0.toInt(), b.y0.toInt(), (b.x1 - b.x0).toInt(), (b.y1 - b.y0).toInt())
            g.color = Color.WHITE
            val pz = pose(s)
            for (h in gym.holds) pz.project(h.p)?.takeIf { it.depth > 0.1 }?.let { at ->
                val r = (h.rM / at.depth * pz.lens.fx).toInt().coerceAtLeast(12)
                g.drawOval(at.u.toInt() - r, at.v.toInt() - r, 2 * r, 2 * r); g.drawString(h.id, at.u.toInt() + r, at.v.toInt())
            }
            if (s.name == pickName) { g.color = Color.GREEN; g.drawLine(pu.toDouble().toInt() - 30, pv.toDouble().toInt(), pu.toDouble().toInt() + 30, pv.toDouble().toInt()) }
            g.dispose()
            ImageIO.write(img, "jpg", File(out, s.name))
            println("${s.name}: ${found[s.name]?.size} patches of the colour")
        }
    }
}

/** The hold-finding model run by ONNX Runtime, as the Android app runs it. */
class OnnxSegmenter(model: ByteArray) : HoldSegmenter {
    private val env = ai.onnxruntime.OrtEnvironment.getEnvironment()
    private val session = env.createSession(model)

    override fun logits(input: FloatArray, w: Int, h: Int): FloatArray? =
        ai.onnxruntime.OnnxTensor.createTensor(env, java.nio.FloatBuffer.wrap(input), longArrayOf(1, 3, h.toLong(), w.toLong())).use { t ->
            session.run(mapOf(session.inputNames.first() to t)).use { r -> (r[0] as ai.onnxruntime.OnnxTensor).floatBuffer.let { b -> FloatArray(b.remaining()).also { b.get(it) } } }
        }
}
