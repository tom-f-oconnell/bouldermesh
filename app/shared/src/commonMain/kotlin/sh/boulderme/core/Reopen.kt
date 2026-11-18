package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.hypot

/**
 * Carrying a finished capture on, while still at the rock and before any of it is sent: for scenery or a panorama
 * missed, or more photos (CaptureFlow.reopen()). The phone's tracking starts afresh, in a world of its own, so what's
 * taken then is a later spell's, which the server ties to the mesh by that spell's photos as it would after tracking
 * was lost and came back.
 */
object Reopen {
    const val NEAR_M = 150.0        // further than this from where it was taken, the phone is said to have left the rock

    /** A capture's record as it saved it (capture.json); null if there's none, or it can't be read. */
    fun record(json: String?): CaptureRecord? = json?.let { runCatching { Plan.json.decodeFromString(CaptureRecord.serializer(), it) }.getOrNull() }

    /**
     * Why a capture can't be carried on, or null if it can.
     *
     * @param record its record, as saved (null if it couldn't be read).
     * @param planKey the key of the plan of its mesh on the phone now: one got again since may have moved the mesh.
     * @param sent whether any of it has been sent (or its mesh asked for).
     */
    fun cannot(record: CaptureRecord?, planKey: String?, sent: Boolean): String? = when {
        sent -> "Some of it has been sent, so it can't be added to now."
        record == null -> "Its record can't be read, so it can't be added to."
        record.mode == "plan" && (record.plan == null || record.plan.key != planKey) ->
            "The mesh it went by has been got again on this phone since, and may have changed: it can't be added to."
        else -> null
    }

    /** A warning that the phone (at `lat`, `lng`) is far from where the capture was taken; null if near, or unknown. */
    fun far(record: CaptureRecord?, lat: Double?, lng: Double?): String? {
        val p = record?.place ?: return null
        if (lat == null || lng == null) return null
        val m = hypot((lat - p.lat) * M_PER_DEG, (lng - p.lng) * M_PER_DEG * cos(p.lat * PI / 180))
        return if (m <= NEAR_M) null else "The phone is ${Coverage.metres(m)} from where it was taken: adding to it is for while you're still at the rock."
    }

    private const val M_PER_DEG = 111_320.0
}

/** A warning, for the Finish dialog, of frames of the scenery too few photos were taken with to place them by; else "". */
fun CaptureUi.unplacedSaid(): String = if (unplaced == 0) "" else
    "\n\n$unplaced frame${if (unplaced == 1) "" else "s"} of the scenery or panorama may not be placed: " +
        (if (tieLeft > 0) "take $tieLeft more photo${if (tieLeft == 1) "" else "s"} of the rock first, as they're placed by the photos taken since the phone started tracking."
         else "fewer than ${CaptureSession.TIE_SHOTS} photos of the rock were taken in the same stretch of the phone's tracking as them (a new one starts when it loses its place).")

/** Which spell of the phone's tracking an `ar.s` names: 1 the first ("<base>"), n for "<base>.n". */
fun spellNumber(s: String?): Int = s?.substringAfter('.', "")?.toIntOrNull() ?: 1

/** The tracked points a capture saved (CaptureFlow.points()), each with its spell; empty if they can't be read. */
fun readPoints(data: ByteArray): List<Pair<Vec3, Int>> {
    if (data.size < 8 || data.decodeToString(0, 4) != "BMP1") return emptyList()
    fun int(at: Int, bytes: Int = 4): Int { var v = 0; for (b in 0 until bytes) v = v or ((data[at + b].toInt() and 0xff) shl (8 * b)); return v }
    fun float(at: Int) = Float.fromBits(int(at)).toDouble()
    val n = int(4)
    if (n < 0 || data.size < 8 + n.toLong() * 14) return emptyList()
    return List(n) { i -> val at = 8 + i * 14; Vec3(float(at), float(at + 4), float(at + 8)) to int(at + 12, 2) }
}
