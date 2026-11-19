package sh.boulderme.app

import android.graphics.Bitmap
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import sh.boulderme.core.LocateFrame
import sh.boulderme.core.Located
import sh.boulderme.core.Locator
import sh.boulderme.core.Plan
import sh.boulderme.core.ServerLocate
import java.io.ByteArrayOutputStream

/**
 * Places frames against a mesh on the server (POST /api/capture/locate/<mesh>), which needs signal: nothing, rather
 * than an error, with none, or when the server doesn't answer within ServerLocate.TIMEOUT_S.
 */
class ServerLocator(private val api: Api) : Locator {
    override suspend fun locate(plan: Plan, frames: List<LocateFrame>): List<Located> = withContext(Dispatchers.IO) {
        if (plan.mesh.isEmpty() || frames.isEmpty()) return@withContext emptyList()
        runCatching {
            val sized = ServerLocate.sized(frames)
            val body = ServerLocate.request(sized, sized.map { jpeg(it.grey, it.w, it.h) })
            ServerLocate.located(api.locate(plan.mesh, body, ServerLocate.TIMEOUT_S), sized)
        }.getOrDefault(emptyList())
    }

    private fun jpeg(grey: ByteArray, w: Int, h: Int): ByteArray {
        val px = IntArray(w * h) { val g = grey[it].toInt() and 0xff; (0xff shl 24) or (g shl 16) or (g shl 8) or g }
        val bmp = Bitmap.createBitmap(px, w, h, Bitmap.Config.ARGB_8888)
        return ByteArrayOutputStream().also { bmp.compress(Bitmap.CompressFormat.JPEG, ServerLocate.QUALITY, it); bmp.recycle() }.toByteArray()
    }
}
