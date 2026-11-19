package sh.boulderme.ios

import kotlinx.cinterop.addressOf
import kotlinx.cinterop.usePinned
import platform.CoreGraphics.CGBitmapContextCreate
import platform.CoreGraphics.CGBitmapContextCreateImage
import platform.CoreGraphics.CGColorSpaceCreateDeviceGray
import platform.CoreGraphics.CGColorSpaceRelease
import platform.CoreGraphics.CGContextRelease
import platform.CoreGraphics.CGImageAlphaInfo
import platform.CoreGraphics.CGImageRelease
import platform.UIKit.UIImage
import platform.UIKit.UIImageJPEGRepresentation
import sh.boulderme.core.LocateFrame
import sh.boulderme.core.Located
import sh.boulderme.core.Locator
import sh.boulderme.core.Plan
import sh.boulderme.core.ServerLocate
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

/**
 * Places frames against a mesh on the server (POST /api/capture/locate/<mesh>), which needs signal: nothing, rather
 * than an error, with none, or when the server doesn't answer within ServerLocate.TIMEOUT_S.
 */
class IosServerLocator(private val api: IosApi) : Locator {
    override suspend fun locate(plan: Plan, frames: List<LocateFrame>): List<Located> {
        if (plan.mesh.isEmpty() || frames.isEmpty()) return emptyList()
        val sized = ServerLocate.sized(frames)
        val jpegs = sized.map { jpeg(it.grey, it.w, it.h) ?: return emptyList() }
        val body = ServerLocate.request(sized, jpegs)
        return suspendCoroutine { k ->
            api.locate(plan.mesh, body, ServerLocate.TIMEOUT_S) { text, _ ->
                k.resume(text?.let { ServerLocate.located(it, sized) } ?: emptyList())
            }
        }
    }

    private fun jpeg(grey: ByteArray, w: Int, h: Int): ByteArray? = grey.usePinned { pinned ->
        val space = CGColorSpaceCreateDeviceGray()
        val ctx = CGBitmapContextCreate(pinned.addressOf(0), w.toULong(), h.toULong(), 8u, w.toULong(), space,
                                        CGImageAlphaInfo.kCGImageAlphaNone.value)
        val cg = CGBitmapContextCreateImage(ctx)
        CGContextRelease(ctx); CGColorSpaceRelease(space)
        if (cg == null) return@usePinned null
        val data = UIImageJPEGRepresentation(UIImage.imageWithCGImage(cg), ServerLocate.QUALITY / 100.0)
        CGImageRelease(cg)
        data?.toBytes()
    }
}
