package sh.boulderme.core

import kotlin.math.max
import kotlin.math.min

/**
 * A hold a capture will add to its mesh as a set of beta (its record's `marked`), as its uploader checks it before
 * sending: where it came from, and where it's seen best in the capture's photos.
 *
 * `found`: the app found it, rather than it being tapped, so it's sent only once its uploader has kept it (HoldReview).
 * `how`: where it came from, in words. `view`: the photo it's seen best in, if any.
 */
data class ReviewHold(val id: String, val found: Boolean, val how: String, val view: HoldView?)

/** Where a hold is in one photo (in the capture's photos/), in its pixels as stored, and how far round it is there. */
data class HoldView(val photo: String, val u: Double, val v: Double, val rPx: Double, val w: Int, val h: Int)

/**
 * The holds of a capture to be checked before it's uploaded: those the app found by itself (a gym climb's, by their
 * colour or the hold-finding model) rather than tapped, which are only guesses. Each one kept is named in the record's
 * `checked`; one left out goes from its `marked` (and its gym's holds), so it's never sent.
 */
object HoldReview {
    /** Every hold the capture marked, gym climbs' found ones first (most photos first), then those tapped. */
    fun holds(record: CaptureRecord): List<ReviewHold> {
        val gym = record.gym
        val gymHolds = gym?.holds?.associateBy { it.id } ?: emptyMap()
        val out = record.marked.map { m ->
            val g = gymHolds[m.id]
            val colour = gym?.name?.let { "$it, " } ?: ""
            val (found, how) = when {
                g != null && g.picked == 1 -> false to "Tapped by you"
                g != null && gym?.finder == Gym.FINDER_MODEL ->
                    true to "Found by the hold-finding model, then its colour (${colour}as the hold you tapped), in ${photos(g.photos)}"
                g != null -> true to "Found by its colour (${colour}as the hold you tapped), in ${photos(g.photos)}"
                // a gym capture carried on keeps its holds in marked, not its gym: which were tapped isn't known then
                GYM_ID.matches(m.id) && gym == null -> true to "Found by colour, on the visit before this capture was carried on"
                else -> false to "Marked by you (Mark a hold)"
            }
            ReviewHold(m.id, found, how, view(record, m, g?.rM ?: TAPPED_R_M))
        }
        return out.sortedWith(compareByDescending<ReviewHold> { it.found }.thenByDescending { h -> gymHolds[h.id]?.photos ?: 0 })
    }

    /** Those the app found that haven't been checked yet: while there are any, the capture isn't sent. */
    fun unchecked(record: CaptureRecord): List<ReviewHold> = holds(record).filter { it.found && it.id !in record.checked }

    /** What the holds a capture would send are and where they came from, in a line, for the screen before uploading. */
    fun said(record: CaptureRecord): String? {
        val all = holds(record)
        if (all.isEmpty()) return null
        val found = all.count { it.found }; val tapped = all.size - found
        val parts = listOfNotNull(
            if (found > 0) "$found found by the app" + (record.gym?.let { g -> if (g.finder == Gym.FINDER_MODEL) " (the hold-finding model, then their colour, ${g.name})" else " (by their colour, ${g.name}, as the hold you tapped)" } ?: "") else null,
            if (tapped > 0) "$tapped tapped by you" else null)
        return "${plural(all.size, "hold")} to add to the mesh as a set of beta: " + parts.joinToString(", ")
    }

    /**
     * The record with only the found holds in `keep` (and every one tapped) left in; those kept and checked named in
     * `checked`, so that they aren't asked about again.
     */
    fun keep(record: CaptureRecord, keep: Set<String>): CaptureRecord {
        val all = holds(record)
        val dropped = all.filter { it.found && it.id !in keep }.map { it.id }.toSet()
        val checked = (record.checked + all.filter { it.found && it.id in keep }.map { it.id }).distinct()
        return record.copy(marked = record.marked.filter { it.id !in dropped }, checked = checked,
            gym = record.gym?.let { g -> g.copy(holds = g.holds.filter { it.id !in dropped }) })
    }

    fun parse(text: String?): CaptureRecord? = text?.let { runCatching { Plan.json.decodeFromString(CaptureRecord.serializer(), it) }.getOrNull() }

    /**
     * The photo of the hold's spell it's seen best in: the one it's largest in, wholly in the picture with some room
     * round it; null if no photo of that spell has it in view (or its spell isn't known).
     */
    fun view(record: CaptureRecord, m: RecordMarked, rM: Double): HoldView? {
        val p = Vec3.of(m.p)
        var best: HoldView? = null
        for (s in record.shots) {
            val ar = s.blob.ar; val k = s.blob.k
            if (!s.kept || s.blob.ref != null || ar == null || k == null || ar.s != m.s) continue
            val at = CameraPose(Vec3.of(ar.p), Quat.of(ar.q), Lens(k.fx, k.fy, k.cx, k.cy, k.w, k.h)).project(p) ?: continue
            if (at.depth < 0.1) continue
            val r = rM * k.fx / at.depth
            val room = max(r * 1.5, min(k.w, k.h) * 0.05)
            if (at.u < room || at.v < room || at.u > k.w - room || at.v > k.h - room) continue
            if (best == null || r > best.rPx) best = HoldView(s.name, at.u, at.v, r, k.w, k.h)
        }
        return best
    }

    private fun photos(n: Int) = plural(n, "photo")
    private fun plural(n: Int, what: String) = "$n $what" + if (n == 1) "" else "s"

    private val GYM_ID = Regex("g\\d+")
    private const val TAPPED_R_M = 0.04
}
