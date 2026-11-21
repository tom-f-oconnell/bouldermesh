package sh.boulderme.core

/** Runs the hold-finding model on a phone (Android: ONNX Runtime; iPhone: Core ML): its input as HoldModel.input() makes it, its output as HoldModel.mask() reads it; null if it couldn't. */
fun interface HoldSegmenter {
    fun logits(input: FloatArray, w: Int, h: Int): FloatArray?
}

/**
 * The hold-finding model (approach B of a gym climb's holds, experiments/holds): which pixels of a picture are a hold.
 * It's given the picture (Picture.rgb, as the camera had it) as 1 x 3 x h x w floats, each channel scaled as ImageNet's,
 * and answers 1 x 3 x h x w scores: background, hold, volume. A patch of one colour within its holds is a hold of that
 * colour (Blobs.find with its mask), whatever the wall behind it is painted. It's run on the colour finder's picture
 * (Picture.SIDE on its longer side), which is as fine as it needs: at half that it missed most small holds.
 */
object HoldModel {
    const val FILE = "holds"            // holds.onnx in the Android app's assets; holds.mlmodelc in the iPhone app
    const val SIDE = Picture.SIDE       // the longer side of the picture it's given
    const val CLASSES = 3
    const val HOLD = 1
    /**
     * What the model was trained on (experiments/holds/README.md), as the apps credit it at the bottom of Settings: each
     * a name, who made it, its licence and where it is. Two are CC BY 4.0, which asks for this.
     */
    val CREDITS: List<List<String>> = listOf(
        listOf("Bouldering Holds", "pwals", "CC BY 4.0", "https://universe.roboflow.com/pwals/bouldering-holds-9wavr"),
        listOf("Climbing Holds and Volumes", "blackcreed", "CC BY 4.0", "https://universe.roboflow.com/blackcreed-xpgxh/climbing-holds-and-volumes"),
        listOf("Climbing Holds", "johns-workspace", "Public Domain", "https://universe.roboflow.com/johns-workspace-rfgwn/climbing-holds-hsnzv"),
    )
    const val CREDITS_NOTE = "A gym climb's holds are found by a model trained on these datasets, from Roboflow Universe. " +
        "CC BY 4.0: creativecommons.org/licenses/by/4.0"

    private val MEAN = floatArrayOf(0.485f, 0.456f, 0.406f)
    private val STD = floatArrayOf(0.229f, 0.224f, 0.225f)

    fun input(p: Picture): FloatArray {
        val n = p.w * p.h
        val out = FloatArray(3 * n)
        for (i in 0 until n) {
            val c = p.rgb[i]
            out[i] = (((c shr 16) and 0xff) / 255f - MEAN[0]) / STD[0]
            out[n + i] = (((c shr 8) and 0xff) / 255f - MEAN[1]) / STD[1]
            out[2 * n + i] = ((c and 0xff) / 255f - MEAN[2]) / STD[2]
        }
        return out
    }

    /** Where the model's best score is a hold's, by at least `margin` (0: any; the owner's debug screen tries others). */
    fun mask(logits: FloatArray, w: Int, h: Int, margin: Float = 0f): BooleanArray {
        val n = w * h
        return BooleanArray(n) { i ->
            val hold = logits[HOLD * n + i]
            (0 until CLASSES).all { k -> k == HOLD || logits[k * n + i] + margin < hold }
        }
    }

    /** The picture's patches of one colour within the holds the model finds in it; null if the model couldn't be run. */
    fun blobs(p: Picture, seg: HoldSegmenter): List<Blob>? =
        seg.logits(input(p), p.w, p.h)?.takeIf { it.size >= CLASSES * p.w * p.h }?.let { Blobs.find(p, mask(it, p.w, p.h)) }
}
