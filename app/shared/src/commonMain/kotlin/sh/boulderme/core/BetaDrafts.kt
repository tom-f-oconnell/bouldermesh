package sh.boulderme.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.builtins.serializer
import kotlinx.serialization.json.Json

/**
 * Sets of beta changed on the phone with no signal, in the site's viewer (its '+' button), not yet on the server: the
 * page hands them to the app (window.BoulderMeshApp on Android, the bmBeta message on iOS), which keeps them per mesh,
 * shows them to the page again, uses their holds in captures, and sends them once there's signal (send()).
 *
 * A set is the page's whole set as it last was: `local` if the server has never had it (its id is the page's
 * "local-..."), `deleted` for one of the server's deleted here. A hold may say its normal (n), where the page knew it.
 */
@Serializable
data class DraftSet(val id: String, val name: String? = null, val holds: List<DraftHold> = emptyList(), val shared: Boolean = true,
                    val local: Boolean = false, val deleted: Boolean = false)

@Serializable
data class DraftHold(val id: String? = null, val p: List<Double>, val role: String? = null, @SerialName("r_m") val rM: Double? = null,
                     val n: List<Double>? = null)

/**
 * What a capture of holds in detail is for: the sets of beta chosen (`sets`), less the holds of theirs left out (`left`:
 * target ids). Its plan has these as one set, FOCUS (focused()).
 */
@Serializable
data class Focus(val sets: List<String>, val left: List<String> = emptyList())

object BetaDrafts {
    const val FOCUS = "focus"                   // the set a capture's chosen holds are, in its plan
    const val LOCAL = "local-"                  // a set the server has never had: the page's id for it
    private const val HOLD_DETAIL_M = 0.075     // a hold's detail, unless it says (captureplan.HOLD_DETAIL_M)
    private const val HOLD_RADIUS_M = 0.15      // a hold's target, this far round it (captureplan.HOLD_RADIUS_M)
    private val json = Json { ignoreUnknownKeys = true; isLenient = true; encodeDefaults = true }
    private val list = ListSerializer(DraftSet.serializer())

    /** The page's drafts, as it hands them over; empty if they can't be read. */
    fun parse(text: String?): List<DraftSet> = text?.let { runCatching { json.decodeFromString(list, it) }.getOrNull() } ?: emptyList()
    fun write(drafts: List<DraftSet>): String = json.encodeToString(list, drafts)

    /**
     * A plan with the phone's drafts in: each set as the draft has it (one deleted gone), its holds targets (one of the
     * plan's where it's the same hold at the same spot, else a new one, facing as the draft says or as the nearest of
     * the plan's patches), and the sets with holds first among its sets as before.
     */
    fun applied(plan: Plan, drafts: List<DraftSet>): Plan {
        if (drafts.isEmpty()) return plan
        val changed = drafts.map { it.id }.toSet()
        var targets = plan.targets.map { t -> if (t.sets.any { it in changed }) t.copy(sets = t.sets - changed) else t }.toMutableList()
        val patches = plan.targets.filter { it.kind == "patch" }
        val sets = plan.sets.filter { it.id !in changed }.toMutableList()
        for (d in drafts.filter { !it.deleted }) {
            val mine = mutableListOf<String>()
            for ((i, h) in d.holds.withIndex()) {
                if (h.p.size != 3) continue
                val p = Vec3.of(h.p)
                val same = targets.indexOfFirst { t -> t.isHold && h.id != null && h.id in t.holds && t.point.distance(p) < 1e-4 }
                if (same >= 0) {
                    targets[same] = targets[same].copy(sets = (targets[same].sets + d.id).distinct(), role = h.role ?: targets[same].role)
                    mine += targets[same].id
                    continue
                }
                val n = h.n?.takeIf { it.size == 3 } ?: patches.minByOrNull { it.point.distance(p) }?.n ?: plan.frame.up
                val id = "d-${d.id}-$i"
                targets += Target(id = id, kind = "hold", p = h.p, n = n, r = HOLD_RADIUS_M / plan.frame.metresPerUnit, prio = 3, role = h.role,
                                  sets = listOf(d.id), holds = listOfNotNull(h.id), detailM = h.rM ?: HOLD_DETAIL_M)
                mine += id
            }
            sets += PlanSet(d.id, d.name, targets = mine)
        }
        return plan.copy(sets = sets, targets = targets)
    }

    /**
     * A plan with a capture's chosen holds as one set, FOCUS: those of the chosen sets, less those left out, named for
     * the sets chosen. The plan as it is if none are left.
     */
    fun focused(plan: Plan, focus: Focus): Plan {
        val chosen = plan.sets.filter { it.id in focus.sets }
        val ids = chosen.flatMap { it.targets }.distinct().filter { it !in focus.left }.toSet()
        if (ids.isEmpty()) return plan
        val name = chosen.mapNotNull { it.name }.joinToString(" + ").ifEmpty { "Chosen holds" }
        return plan.copy(sets = plan.sets.filter { it.id != FOCUS } + PlanSet(FOCUS, name, targets = ids.toList(), of = focus.sets, left = focus.left),
                         targets = plan.targets.map { t -> if (t.id in ids) t.copy(sets = (t.sets + FOCUS).distinct()) else t })
    }

    /** A hold's line in a list of them: its role and the sets it's in. */
    fun label(plan: Plan, t: Target, sets: Collection<String>): String {
        val role = t.role?.replaceFirstChar { it.uppercase() } ?: "Hold"
        val of = plan.sets.filter { it.id in sets && it.id in t.sets }.mapNotNull { it.name }
        return role + if (of.isEmpty()) "" else " · " + of.joinToString(", ")
    }

    /**
     * What to send the server for each draft, in order: ("DELETE", path, null), ("POST", path, body) for a new set
     * (whose answer's id the rest of that draft's requests take, in place of ID), ("PATCH", path, body).
     */
    fun requests(mesh: String, d: DraftSet): List<Triple<String, String, String?>> {
        val base = "/api/meshes/$mesh/beta/sets"
        if (d.deleted) return if (d.local) emptyList() else listOf(Triple("DELETE", "$base/${d.id}", null))
        val holds = json.encodeToString(ListSerializer(DraftHold.serializer()), d.holds)
        val name = d.name?.let { json.encodeToString(String.serializer(), it) }
        val patch = "{\"holds\":$holds,\"shared\":${d.shared}" + (name?.let { ",\"name\":$it" } ?: "") + "}"
        return if (d.local) listOf(Triple("POST", base, "{\"shared\":${d.shared}" + (name?.let { ",\"name\":$it" } ?: "") + "}"), Triple("PATCH", "$base/$ID", patch))
        else listOf(Triple("PATCH", "$base/${d.id}", patch))
    }
    const val ID = "{id}"
}
