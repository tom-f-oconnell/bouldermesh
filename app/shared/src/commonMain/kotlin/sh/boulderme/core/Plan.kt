package sh.boulderme.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** What the server says of a mesh for capturing more of it (its /api/capture/plan/<mesh>; backend/captureplan.py). */
@Serializable
data class Plan(
    val v: Int = 1,
    val mesh: String = "",
    val name: String = "",
    val lat: Double? = null,
    val lng: Double? = null,
    @SerialName("heading_deg") val headingDeg: Double? = null,
    @SerialName("guide_url") val guideUrl: String = "",
    val frame: Frame = Frame(),
    val guide: GuideInfo = GuideInfo(),
    val need: Map<String, Need> = emptyMap(),
    @SerialName("view_deg") val viewDeg: Double = 15.0,
    @SerialName("facing_deg") val facingDeg: Double = 75.0,
    val cameras: List<PlacedCamera> = emptyList(),
    val targets: List<Target> = emptyList(),
    val climbs: List<PlanClimb> = emptyList(),
) {
    fun needOf(kind: String): Need = need[kind] ?: DEFAULT_NEED[kind] ?: Need()

    companion object {
        val DEFAULT_NEED = mapOf("hold" to Need(0.5, 4), "patch" to Need(1.5, 3), "edge" to Need(null, 3))
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

/** What a target needs: seen at least this finely (millimetres of rock a pixel), from at least this many directions. */
@Serializable
data class Need(@SerialName("gsd_mm") val gsdMm: Double? = null, val views: Int = 3)

@Serializable
data class PlacedCamera(val c: List<Double>, val d: List<Double>)

@Serializable
data class PlanClimb(val id: String, val name: String? = null, val grade: String? = null)

/**
 * Something to photograph, in the mesh's own coordinates: a hold, a patch of rock, or an edge where the mesh ends.
 * prio: 3 a hold, 2 rock on a climb, 1 other rock, 0 the ground. have: what its photos so far gave it.
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
) {
    val point: Vec3 get() = Vec3.of(p)
    val normal: Vec3 get() = Vec3.of(n)
    val beyond: Vec3? get() = out?.let { Vec3.of(it) }
    val isHold: Boolean get() = kind == "hold"
    val isEdge: Boolean get() = kind == "edge"
}
