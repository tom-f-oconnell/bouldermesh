package sh.boulderme.core

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlin.io.encoding.Base64
import kotlin.io.encoding.ExperimentalEncodingApi

/**
 * What the apps' server Locators (ServerLocator on Android, IosServerLocator on iOS) share: the request to POST
 * /api/capture/locate/<mesh> (backend/locateapi.py), and its answer as Located frames. Each app only turns frames into
 * JPEGs and sends them.
 */
object ServerLocate {
    /** Seconds an app waits for the server's answer before going on without it. */
    const val TIMEOUT_S = 8.0
    /** A frame's longer side is shrunk to this, in pixels, before it's sent. */
    const val MAX_SIDE = 1280
    /** Frames sent at once, at most (the server's limit). */
    const val MAX_FRAMES = 4
    /** JPEG quality the frames are sent at. */
    const val QUALITY = 85

    /** A frame as sent: its picture (grey, w*h bytes), possibly shrunk, and its lens for that size. */
    class Sized(val id: Int, val grey: ByteArray, val w: Int, val h: Int, val lens: Lens)

    /** The frames to send (the first MAX_FRAMES), shrunk to MAX_SIDE, each with its lens for its size. */
    fun sized(frames: List<LocateFrame>): List<Sized> = frames.take(MAX_FRAMES).map { f ->
        val k = minOf(1.0, MAX_SIDE.toDouble() / maxOf(f.w, f.h))
        val w = maxOf(1, (f.w * k).toInt()); val h = maxOf(1, (f.h * k).toInt())
        val grey = if (k < 1.0) shrink(f.grey, f.w, f.h, w, h) else f.grey
        Sized(f.id, grey, w, h, f.world.lens.scaledTo(w, h))
    }

    /** A grey picture shrunk to w by h, each pixel the mean of those it covers. */
    fun shrink(grey: ByteArray, sw: Int, sh: Int, w: Int, h: Int): ByteArray {
        val out = ByteArray(w * h)
        for (y in 0 until h) {
            val y0 = y * sh / h; val y1 = maxOf(y0 + 1, (y + 1) * sh / h)
            for (x in 0 until w) {
                val x0 = x * sw / w; val x1 = maxOf(x0 + 1, (x + 1) * sw / w)
                var sum = 0
                for (yy in y0 until y1) for (xx in x0 until x1) sum += grey[yy * sw + xx].toInt() and 0xff
                out[y * w + x] = (sum / ((y1 - y0) * (x1 - x0))).toByte()
            }
        }
        return out
    }

    @Serializable private class LensJson(val fx: Double, val fy: Double, val cx: Double, val cy: Double, val w: Int, val h: Int)
    @Serializable private class FrameJson(val id: Int, val jpeg_b64: String, val lens: LensJson)
    @Serializable private class RequestJson(val frames: List<FrameJson>)
    @Serializable private class LocatedJson(val id: Int, val c: List<Double>, val q: List<Double>, val inliers: Int,
                                            val rms_px: Double, val how: String = "")
    @Serializable private class AnswerJson(val located: List<LocatedJson> = emptyList(), val ms: Int = 0)

    private val json = Json { ignoreUnknownKeys = true }

    /** The request's body, for frames as sized() made them and their JPEGs (in the same order). */
    @OptIn(ExperimentalEncodingApi::class)
    fun request(frames: List<Sized>, jpegs: List<ByteArray>): String = json.encodeToString(RequestJson.serializer(), RequestJson(
        frames.zip(jpegs).map { (f, j) ->
            FrameJson(f.id, Base64.encode(j), LensJson(f.lens.fx, f.lens.fy, f.lens.cx, f.lens.cy, f.lens.w, f.lens.h))
        }))

    /**
     * The frames the server placed, from its answer: each camera in the mesh's coordinates, with the lens it was sent
     * with. Empty if the answer can't be read, or names frames that weren't sent.
     */
    fun located(answer: String, sent: List<Sized>): List<Located> {
        val got = runCatching { json.decodeFromString(AnswerJson.serializer(), answer) }.getOrNull() ?: return emptyList()
        val byId = sent.associateBy { it.id }
        return got.located.mapNotNull { l ->
            val f = byId[l.id] ?: return@mapNotNull null
            if (l.c.size != 3 || l.q.size != 4 || (l.c + l.q).any { !it.isFinite() }) return@mapNotNull null
            val pose = CameraPose(Vec3(l.c[0], l.c[1], l.c[2]), Quat(l.q[0], l.q[1], l.q[2], l.q[3]).unit(), f.lens)
            Located(l.id, pose, l.inliers, l.rms_px, "server")
        }
    }
}
