package sh.boulderme.ios

import kotlinx.cinterop.ExperimentalForeignApi
import kotlinx.cinterop.FloatVar
import kotlinx.cinterop.get
import kotlinx.cinterop.reinterpret
import kotlinx.cinterop.set
import platform.CoreML.MLDictionaryFeatureProvider
import platform.CoreML.MLFeatureValue
import platform.CoreML.MLModel
import platform.CoreML.MLMultiArray
import platform.CoreML.MLMultiArrayDataTypeFloat32
import platform.CoreML.create
import platform.Foundation.NSBundle
import platform.Foundation.NSNumber
import sh.boulderme.core.HoldModel
import sh.boulderme.core.HoldSegmenter

/**
 * The hold-finding model (HoldModel), run by Core ML: holds.mlpackage, compiled into the app by Xcode (app/ios/xcode
 * Models, made by experiments/holds/export.py). Its input is "image" (1 x 3 x SIDE x SIDE: Core ML can't take a size
 * known only as it runs), the picture in its top left and the rest zero (the mean colour, as the input is scaled); its
 * output "logits", of which the picture's part is read. Null if the app hasn't it or it won't load.
 */
@OptIn(ExperimentalForeignApi::class)
class HoldFinder private constructor(private val model: MLModel) : HoldSegmenter {
    override fun logits(input: FloatArray, w: Int, h: Int): FloatArray? {
        if (w > SIDE || h > SIDE) return null
        val shape = listOf(1, 3, SIDE, SIDE).map { NSNumber(int = it) }
        val arr = MLMultiArray.create(shape, MLMultiArrayDataTypeFloat32, null) ?: return null
        val into = arr.dataPointer?.reinterpret<FloatVar>() ?: return null
        for (i in 0 until 3 * SIDE * SIDE) into[i] = 0f
        for (k in 0 until 3) for (y in 0 until h) for (x in 0 until w) into[(k * SIDE + y) * SIDE + x] = input[(k * h + y) * w + x]
        val features = MLDictionaryFeatureProvider(dictionary = mapOf<Any?, Any?>("image" to MLFeatureValue.featureValueWithMultiArray(arr)), error = null)
        val got = model.predictionFromFeatures(features, error = null)?.featureValueForName("logits")?.multiArrayValue ?: return null
        if (got.dataType != MLMultiArrayDataTypeFloat32) return null
        val from = got.dataPointer?.reinterpret<FloatVar>() ?: return null
        // as strided as Core ML left it: classes, rows, columns
        val st = got.strides.map { (it as NSNumber).longValue }
        if (st.size != 4) return null
        val out = FloatArray(HoldModel.CLASSES * w * h)
        for (k in 0 until HoldModel.CLASSES) for (y in 0 until h) for (x in 0 until w)
            out[(k * h + y) * w + x] = from[k * st[1] + y * st[2] + x * st[3]]
        return out
    }

    companion object {
        const val SIDE = HoldModel.SIDE
        fun load(): HoldFinder? {
            val url = NSBundle.mainBundle.URLForResource(HoldModel.FILE, withExtension = "mlmodelc") ?: return null
            return MLModel.modelWithContentsOfURL(url, error = null)?.let { HoldFinder(it) }
        }
    }
}
