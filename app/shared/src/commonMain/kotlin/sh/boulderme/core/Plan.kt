package sh.boulderme.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** What the server says of a mesh for capturing more of it (its /api/capture/plan/<mesh>; backend/captureplan.py). */
@Serializable
data class Plan(
    val v: Int = 1,
    val key: String = "",           // what it was made from: a capture's record names it, so the server knows which plan the capture went by
    val made: Double = 0.0,
    val mesh: String = "",
    val name: String = "",
    val lat: Double? = null,
    val lng: Double? = null,
    @SerialName("heading_deg") val headingDeg: Double? = null,
    @SerialName("guide_url") val guideUrl: String = "",
    val frame: Frame = Frame(),
    val guide: GuideInfo = GuideInfo(),
    val need: Map<String, Need> = emptyMap(),
    val bridge: Bridge = Bridge(),
    val sets: List<PlanSet> = emptyList(),              // its sets of beta, each with its holds among the targets
    val roles: Map<String, String> = emptyMap(),        // what each role of hold is drawn in, as the site draws it: "#rrggbb"
    @SerialName("view_deg") val viewDeg: Double = 15.0,
    @SerialName("facing_deg") val facingDeg: Double = 75.0,
    val cameras: List<PlacedCamera> = emptyList(),
    val targets: List<Target> = emptyList(),
    val climbs: List<PlanClimb> = emptyList(),
) {
    /** What a role of hold is drawn in, as the site draws it: red, green and blue from 0 to 1; null if the plan doesn't say. */
    fun roleColor(role: String?): List<Float>? {
        val hex = roles[role ?: return null]?.removePrefix("#")?.takeIf { it.length == 6 } ?: return null
        return (0..2).map { (hex.substring(2 * it, 2 * it + 2).toIntOrNull(16) ?: return null) / 255f }
    }

    /** Its sets of beta that have holds to capture: a capture can be for one of them. */
    val setsWithHolds: List<PlanSet> get() = sets.filter { it.targets.isNotEmpty() }

    fun needOf(kind: String): Need = need[kind] ?: DEFAULT_NEED[kind] ?: Need()

    companion object {
        val DEFAULT_NEED = mapOf("hold" to Need(0.5, 4), "patch" to Need(1.5, 3), "edge" to Need(null, 3), "view" to Need(null, 1),
            "detail" to Need(null, 6, 2.0, closeM = 0.15, viewDeg = 20.0, sidesDeg = 40.0, ties = 3))
        internal val json = Json { ignoreUnknownKeys = true; isLenient = true; encodeDefaults = true }

        /** Reads a plan. Throws SerializationException if it isn't one. */
        fun parse(text: String): Plan = json.decodeFromString(serializer(), text)
    }
}

@Serializable
data class Frame(
    val up: List<Double> = listOf(0.0, 1.0, 0.0),
    @SerialName("ground_y") val groundY: Double = 0.0,
    val size: Double = 1.0,
    val height: Double = 1.0,
    @SerialName("metres_per_unit") val metresPerUnit: Double = 1.0,
    @SerialName("scale_guessed") val scaleGuessed: Boolean = true,
)

@Serializable
data class GuideInfo(val vertices: Int = 0, val faces: Int = 0, val patches: Int = 0, val sha1: String = "")

/**
 * What a target needs: seen at least this finely (millimetres of rock a pixel), from at least this many directions.
 * closeM, in place of gsdMm (the "detail" need, of the holds of the set of beta a capture is for): as finely as the
 * phone's own lens gives from as close as it focuses, taken as this far off; viewDeg: its directions this far apart.
 * sidesDeg: besides straight on, a close view from its left, right, above and below, this far round, where a phone can
 * get to; ties: this many steps of its ladder are needed, not only asked for on the way in. facingDeg (the "texture"
 * need, of rock on no climb): its one view within this of square on, taken now.
 * ladder: on the way in to it, a photo at each step this many times coarser, up to how its earlier photos saw it, so
 * that the close photos tie to those through photos in between (wanted, and asked for on the way, but not needed).
 */
@Serializable
data class Need(@SerialName("gsd_mm") val gsdMm: Double? = null, val views: Int = 3, val ladder: Double? = null,
                @SerialName("close_m") val closeM: Double? = null, @SerialName("view_deg") val viewDeg: Double? = null,
                @SerialName("sides_deg") val sidesDeg: Double? = null, val ties: Int = 0, @SerialName("facing_deg") val facingDeg: Double? = null)

/** A set of beta made by hand on the site: its name, the climb it's for if it says, and which of the plan's targets are its holds. */
@Serializable
data class PlanSet(val id: String, val name: String? = null, val climb: String? = null, val targets: List<String> = emptyList())

/** How near where an earlier photo was taken (a "view" target) a photo has to be taken to count as the same view again. */
@Serializable
data class Bridge(@SerialName("within_m") val withinM: Double = 1.0, val deg: Double = 20.0)

@Serializable
data class PlacedCamera(val c: List<Double>, val d: List<Double>)

@Serializable
data class PlanClimb(val id: String, val name: String? = null, val grade: String? = null)

/**
 * Something to photograph, in the mesh's own coordinates: a hold, a patch of rock, an edge where the mesh ends, or a
 * view: where one of its earlier photos was taken (p) and the way it looked (n), to photograph it from again, which ties
 * the new photos to the old.
 * prio: 3 a hold, 2 rock on a climb or a view, 1 other rock, 0 the ground. have: what its photos so far gave it.
 */
@Serializable
data class Target(
    val id: String,
    val kind: String,
    val p: List<Double>,
    val n: List<Double>,
    val r: Double,
    val prio: Int = 1,
    val role: String? = null,
    val from: String? = null,
    val out: List<Double>? = null,
    @SerialName("have_gsd") val haveGsd: Double? = null,
    @SerialName("have_views") val haveViews: Int = 0,
    val climbs: List<String> = emptyList(),
    val sets: List<String> = emptyList(),       // a hold: the sets of beta it's in,
    val holds: List<String> = emptyList(),      // which holds of theirs it is (ids that outlast a plan),
    @SerialName("detail_m") val detailM: Double? = null,    // and how far round it reaches, in metres: what a close capture of it is of
) {
    val point: Vec3 get() = Vec3.of(p)
    val normal: Vec3 get() = Vec3.of(n)
    val beyond: Vec3? get() = out?.let { Vec3.of(it) }
    val isHold: Boolean get() = kind == "hold"
    val isEdge: Boolean get() = kind == "edge"
    val isView: Boolean get() = kind == "view"
}
