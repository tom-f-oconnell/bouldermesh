package sh.boulderme.core

import kotlin.math.hypot

/**
 * The owner's look at how holds are found in one picture (the apps' debug screen): every patch each finder gives — the
 * colour finder's (A), and those within the model's holds (B) — the one tapped, and which of the chosen finder's are
 * of its colour, as a gym capture would take them. A new tap starts again from that hold.
 */
object HoldProbe {
    /**
     * What was found: `colour`, `model` (null without the model, or if it failed), the patch `picked` (of the chosen
     * finder's, at the tap; null if none there), `matches` (the chosen finder's patches of its colour, it among them),
     * and the model's `mask` (true where it says a hold is, at `margin`), to draw.
     */
    class Result(val colour: List<Blob>, val model: List<Blob>?, val picked: Blob?, val matches: List<Blob>, val mask: BooleanArray?,
                 val name: String?, val rgb: Int?)

    /**
     * Args:
     *     finder: Gym.FINDER_COLOUR or the model's (Gym's FINDER_MODEL): whose patches the tap picks and are matched.
     *     u, v: the tap, in the picture's pixels (times its step); null for none yet.
     *     tolerance: HoldColour's, wider above 1.
     *     margin: how much the model's hold score must beat the others' (HoldModel.mask()).
     */
    fun run(p: Picture, seg: HoldSegmenter?, finder: String, u: Double?, v: Double?, tolerance: Double, margin: Float): Result {
        val colour = Blobs.find(p)
        val logits = seg?.logits(HoldModel.input(p), p.w, p.h)?.takeIf { it.size >= HoldModel.CLASSES * p.w * p.h }
        val mask = logits?.let { HoldModel.mask(it, p.w, p.h, margin) }
        val model = mask?.let { Blobs.find(p, it) }
        val among = if (finder == Gym.FINDER_COLOUR) colour else model ?: emptyList()
        val picked = if (u != null && v != null) at(among, u, v) else null
        val hc = picked?.let { HoldColour(it).apply { this.tolerance = tolerance } }
        return Result(colour, model, picked, hc?.let { c -> among.filter { c.matches(it) } } ?: emptyList(), mask, hc?.name, hc?.rgb)
    }

    /** The patch at a spot: the smallest whose box holds it, else the nearest within twice its size. */
    fun at(blobs: List<Blob>, u: Double, v: Double): Blob? =
        blobs.filter { u in it.x0..it.x1 && v in it.y0..it.y1 }.minByOrNull { it.areaPx }
            ?: blobs.filter { hypot(it.u - u, it.v - v) < 2 * it.radiusPx + 4 }.minByOrNull { hypot(it.u - u, it.v - v) }
}
