package sh.boulderme.app

import android.graphics.Bitmap
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import sh.boulderme.core.LocateFrame
import sh.boulderme.core.Located
import sh.boulderme.core.Locator
import sh.boulderme.core.Plan
import sh.boulderme.core.ServerLocate
import sh.boulderme.core.TimedLocator
import java.io.ByteArrayOutputStream

/**
 * Places frames against a mesh on the server (POST /api/capture/locate/<mesh>), which needs signal: nothing, rather
 * than an error, with none, or when the server doesn't answer within ServerLocate.TIMEOUT_S.
 */
class ServerLocator(private val api: Api) : Locator, TimedLocator {
    @Volatile override var took: Map<String, Any?> = emptyMap()
        private set

    override suspend fun locate(plan: Plan, frames: List<LocateFrame>): List<Located> = withContext(Dispatchers.IO) {
        if (plan.mesh.isEmpty() || frames.isEmpty()) return@withContext emptyList()
        val steps = LinkedHashMap<String, Any?>()
        var at = System.nanoTime()
        fun step(name: String) { val now = System.nanoTime(); steps[name] = (now - at) / 1e6; at = now }
        runCatching {
            val sized = ServerLocate.sized(frames)
            step("shrink_ms")
            val jpegs = sized.map { jpeg(it.grey, it.w, it.h) }
            step("jpeg_ms")
            val body = ServerLocate.request(sized, jpegs)
            step("body_ms"); steps["bytes"] = body.length
            val answer = api.locate(plan.mesh, body, ServerLocate.TIMEOUT_S)
            step("http_ms")         // sending, the server's part (server_ms) and the answer back
            steps.putAll(ServerLocate.took(answer))
            ServerLocate.located(answer, sized)
        }.onFailure { steps["error"] = "${it.javaClass.simpleName}: ${it.message}"; step("failed_after_ms") }.getOrDefault(emptyList()).also { took = steps }
    }

    private fun jpeg(grey: ByteArray, w: Int, h: Int): ByteArray {
        val px = IntArray(w * h) { val g = grey[it].toInt() and 0xff; (0xff shl 24) or (g shl 16) or (g shl 8) or g }
        val bmp = Bitmap.createBitmap(px, w, h, Bitmap.Config.ARGB_8888)
        return ByteArrayOutputStream().also { bmp.compress(Bitmap.CompressFormat.JPEG, ServerLocate.QUALITY, it); bmp.recycle() }.toByteArray()
    }
}
