package sh.boulderme.app

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import sh.boulderme.core.HoldModel
import sh.boulderme.core.HoldSegmenter
import java.nio.FloatBuffer

/** The hold-finding model (HoldModel), run by ONNX Runtime from the APK's assets; null if it isn't there or won't load. */
class HoldFinder private constructor(private val env: OrtEnvironment, private val session: OrtSession) : HoldSegmenter {
    override fun logits(input: FloatArray, w: Int, h: Int): FloatArray? = runCatching {
        OnnxTensor.createTensor(env, FloatBuffer.wrap(input), longArrayOf(1, 3, h.toLong(), w.toLong())).use { t ->
            session.run(mapOf(session.inputNames.first() to t)).use { r -> (r[0] as OnnxTensor).floatBuffer.let { b -> FloatArray(b.remaining()).also { b.get(it) } } }
        }
    }.getOrNull()

    companion object {
        fun load(ctx: Context): HoldFinder? = runCatching {
            val bytes = ctx.assets.open("${HoldModel.FILE}.onnx").use { it.readBytes() }
            val env = OrtEnvironment.getEnvironment()
            HoldFinder(env, env.createSession(bytes, OrtSession.SessionOptions().apply { setIntraOpNumThreads(2) }))
        }.getOrNull()
    }
}
