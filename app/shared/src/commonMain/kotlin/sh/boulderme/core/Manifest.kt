package sh.boulderme.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** What goes into each photo's own file, in its EXIF UserComment after MARK (app/FORMAT.md; backend/phonedata.py reads it). */
@Serializable
data class ShotBlob(
    val v: Int = 1,
    val app: String,
    val device: String,
    val cap: String,
    val i: Int,
    val t: Double,
    val down: List<Double>? = null,
    @SerialName("down_src") val downSrc: String? = null,
    @SerialName("down_acc") val downAcc: List<Double>? = null,
    @SerialName("down_track") val downTrack: List<Double>? = null,
    val turn: Int = 0,                      // how far clockwise the sensor's picture was turned to store it upright
    val heading: Double? = null,
    @SerialName("heading_acc") val headingAcc: Double? = null,
    val gps: BlobGps? = null,
    val ar: BlobAr? = null,
    val k: BlobLens? = null,
    val exp: BlobExposure? = null,
    val q: BlobQuality? = null,
    val ref: String? = null,                // "id1-card": a bank card lies flat on the rock in it, for size; of which hold (ref_hold)
    @SerialName("ref_hold") val refHold: String? = null,
    val still: Int? = null,                 // 1: a full-size still, where the phone's other photos are its tracking's frames
    @SerialName("k_src") val kSrc: String? = null,      // "derived": its lens (k) was worked out from the tracking camera's, not given
    val lic: String? = null,                // the licence it's given under, and who to credit
    val by: String? = null,
) {
    /** As it goes into the photo: plain ASCII, so that whatever writes the EXIF keeps it a byte a character. */
    fun userComment(): String = MARK + ascii(Plan.json.encodeToString(serializer(), this))

    companion object {
        const val MARK = "BMSH1:"

        /** JSON with every character past ASCII written as its escape (they're only ever inside its strings). */
        internal fun ascii(json: String): String = if (json.all { it.code < 128 }) json else buildString {
            for (c in json) if (c.code < 128) append(c) else append("\\u").append(c.code.toString(16).padStart(4, '0'))
        }
    }
}

@Serializable data class BlobGps(val lat: Double, val lng: Double, val alt: Double? = null, val acc: Double, val age: Double)
@Serializable data class BlobAr(val s: String, val p: List<Double>, val q: List<Double>, val track: String = "normal",
                                val span: BlobSpan? = null, val paused: Int? = null)
/** A full-size still's pose is the tracking's either side of it: how long apart those were, and how far the phone moved and turned between. */
@Serializable data class BlobSpan(@SerialName("dt_s") val dtS: Double, @SerialName("moved_m") val movedM: Double, @SerialName("turned_deg") val turnedDeg: Double)
@Serializable data class BlobLens(val fx: Double, val fy: Double, val cx: Double, val cy: Double, val w: Int, val h: Int)
@Serializable data class BlobExposure(val t: Double? = null, val iso: Double? = null, @SerialName("focus_m") val focusM: Double? = null)
@Serializable data class BlobQuality(val sharp: Double? = null, @SerialName("blur_px") val blurPx: Double, @SerialName("clip_hi") val clipHi: Double? = null,
                                     @SerialName("clip_lo") val clipLo: Double? = null, val ok: Boolean)

/** A photo in a capture's record: its file, its readings, the verdict on it and the targets it helped. */
@Serializable
data class ShotRecord(val name: String, val blob: ShotBlob, val kept: Boolean, val why: List<String> = emptyList(),
                      val notes: List<String> = emptyList(), val helped: List<String> = emptyList())

/**
 * What a capture got of a hold: its finest view (mm of rock a pixel), the directions that count of those it needs, and
 * for one wanted in detail the sides it has close views from of those wanted, the photos from further off that tie
 * them in, and whether one has a card beside it. `ids`: which holds of the mesh's sets of beta it is; p: where, in the
 * mesh's coordinates; r_m: how far round it was wanted in detail.
 */
@Serializable
data class RecordHold(
    val target: String, val ids: List<String> = emptyList(), val p: List<Double> = emptyList(), @SerialName("r_m") val rM: Double? = null,
    @SerialName("gsd_mm") val gsdMm: Double? = null, val views: Int = 0, @SerialName("views_of") val viewsOf: Int = 0,
    val sides: List<String> = emptyList(), @SerialName("sides_of") val sidesOf: List<String> = emptyList(),
    val ties: Int = 0, @SerialName("ties_of") val tiesOf: Int = 0, val card: Boolean = false, val done: Boolean = false,
)

/** The set of beta a capture was for, and what it got of each of its holds. */
@Serializable data class RecordDetail(val set: String, val holds: List<RecordHold>)

/** A whole capture, as sent to the server beside its photos (POST /api/projects/<id>/capture). */
@Serializable
data class CaptureRecord(
    val v: Int = 3,                         // 2: plan, level, down_agree_deg; `ar.s` changes when tracking was lost and came back; accel_bias gone
                                            // 3: detail, licence, credit; a photo's ref, still, ar.span
    val id: String,
    val app: String,
    val device: String,
    val mesh: String? = null,
    val climbs: List<String> = emptyList(),
    val plan: RecordPlan? = null,           // which plan of that mesh the capture went by
    val started: Double,
    val ended: Double,
    val mode: String,                       // "plan" (guided by a mesh there already was) or "orbit" (a rock with none)
    val fit: RecordFit? = null,
    val place: RecordPlace? = null,
    val calibration: RecordCalibration? = null,
    val level: RecordLevel? = null,
    @SerialName("down_agree_deg") val downAgreeDeg: Double? = null,
    val skipped: Map<String, String> = emptyMap(),
    val progress: RecordProgress? = null,
    val detail: RecordDetail? = null,       // the set of beta it was for, if one was chosen
    val licence: String? = null,            // what its photos are given under by whoever took them, and who to credit
    val credit: String? = null,
    val shots: List<ShotRecord>,
) {
    fun json(): String = Plan.json.encodeToString(serializer(), this)
}

/** The plan a capture went by: what it was made from (its key), when, and its guide. A mesh's frame can change between a plan's download and a capture's upload. */
@Serializable data class RecordPlan(val key: String, val made: Double, val guide: String)

/** How the mesh was lined up with the world: metres a unit, how far off level it turned out, and how well it fitted. */
@Serializable
data class RecordFit(val scale: Double, val rot: List<Double>, val shift: List<Double>, @SerialName("tilt_deg") val tiltDeg: Double,
                     val taps: Int, val matched: Int? = null, @SerialName("rms_m") val rmsM: Double? = null)

@Serializable data class RecordPlace(val lat: Double, val lng: Double, val alt: Double? = null, @SerialName("acc_m") val accM: Double, val n: Int, val seconds: Double)
/** Which way is down, as a turn on the spot found it (Level): in the tracking's world, with how far the tracking's own was off and how well the turn's directions agreed. */
@Serializable data class RecordLevel(val down: List<Double>, @SerialName("tilt_deg") val tiltDeg: Double, val offset: List<Double>, @SerialName("offset_deg") val offsetDeg: Double,
                                     @SerialName("spread_deg") val spreadDeg: Double, val sectors: Int, val samples: Int)
@Serializable data class RecordCalibration(@SerialName("mag_offset") val magOffset: List<Double>? = null,
                                           @SerialName("mag_field_ut") val magFieldUt: Double? = null, @SerialName("mag_rms_ut") val magRmsUt: Double? = null)
@Serializable data class RecordProgress(val total: Int, val done: Int, val skipped: Int, val holds: Int, @SerialName("holds_done") val holdsDone: Int)
