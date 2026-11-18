package sh.boulderme.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * One purpose a capture's data was for: the photos of one climb, the card for size, the scenery round the rock, or the
 * readings sent beside them. `photos` are file names in the capture's photos/ (or scenery/, for `scenery`); `files`
 * are its other files, each with what it holds.
 */
data class ContentGroup(val key: String, val title: String, val about: String, val photos: List<String> = emptyList(),
                        val scenery: Boolean = false, val files: List<Pair<String, String>> = emptyList())

/** What a capture holds, grouped by what each part of it was for (the upload's own screen in both apps). */
object CaptureContents {
    /**
     * Groups a capture's photos by what each was taken for, from the targets it helped (the record's `helped`) and the
     * plan the capture went by: a hold or rock on a climb goes under that climb, then the card, the photos that tie it to
     * the mesh's earlier ones, the rest of the rock, the ground. A photo that helped several goes under the one that
     * counts most. Without the plan (a capture from before the apps kept it), climbs can't be named, so holds and rock
     * go by kind alone.
     *
     * Args:
     *     record: the capture's record (capture.json), or null if it can't be read: every photo then goes in one group
     *     plan: the plan the capture went by, if kept
     *     photos: the photos on the phone; scenery: the scenery's frames
     *     others: the capture's other files there are, each with its size in bytes
     */
    fun group(record: CaptureRecord?, plan: Plan?, photos: List<String>, scenery: List<String>, others: Map<String, Long>): List<ContentGroup> {
        val targets = plan?.targets?.associateBy { it.id } ?: emptyMap()
        val climbNames = plan?.climbs?.associate { c -> c.id to listOfNotNull(c.name ?: c.id, c.grade).joinToString(" ") } ?: emptyMap()
        val shots = record?.shots?.associateBy { it.name } ?: emptyMap()
        val detailTargets = record?.detail?.holds?.map { it.target }?.toSet() ?: emptySet()
        val setName = record?.detail?.set?.let { s -> plan?.sets?.firstOrNull { it.id == s }?.name ?: "A set of beta" }
        val by = LinkedHashMap<String, MutableList<String>>()

        fun purpose(name: String): String {
            val shot = shots[name] ?: return if (record == null) "unknown" else "other"
            if (shot.blob.ref == CaptureSession.CARD) return "card"
            if (record?.mode == "orbit") return "orbit"
            val helped = shot.helped
            if (helped.any { it in detailTargets }) return "detail"
            if (helped.isEmpty()) return "other"
            if (plan == null) return when {          // by the target's id alone: h<i> a hold, v<i> a view, p<k> and e<i> rock
                helped.any { it.startsWith("h") } -> "holds"
                helped.any { it.startsWith("v") } -> "view"
                else -> "rock"
            }
            val known = helped.mapNotNull { targets[it] }
            val top = known.sortedWith(compareByDescending<Target> { it.prio }.thenByDescending { it.climbs.isNotEmpty() }).firstOrNull() ?: return "other"
            return when {
                top.climbs.isNotEmpty() && !top.isView -> "climb:" + top.climbs.first()
                top.isView -> "view"
                top.isHold -> "holds"
                top.prio == 0 -> "ground"
                else -> "rock"
            }
        }
        for (name in photos.sorted()) by.getOrPut(purpose(name)) { ArrayList() }.add(name)

        val out = ArrayList<ContentGroup>()
        fun add(key: String, title: String, about: String) { by[key]?.let { out.add(ContentGroup(key, title, about, it)) } }
        add("detail", "$setName: its holds in detail", "From close, all round each hold, with photos from further off tying them to the rest")
        for (key in by.keys.filter { it.startsWith("climb:") }.sortedBy { climbNames[it.removePrefix("climb:")] ?: it }) {
            val id = key.removePrefix("climb:")
            add(key, climbNames[id] ?: "A climb", "Its holds and the rock it goes up")
        }
        add("holds", "Holds", "Holds marked on the mesh")
        add("orbit", "All round the rock", "A rock with no mesh yet: photos from every side")
        add("card", "A card for size", "A bank card on the rock beside a hold, to tell its size by")
        add("view", "Tying to the mesh's photos", "From where its earlier photos were taken, so the new ones join up with them")
        add("rock", "The rest of the rock", "Rock no climb goes up, and where the mesh ran out")
        add("ground", "The ground", "Round the rock's foot")
        add("other", "Along the way", "Kept, though nothing was still wanted where they look")
        add("unknown", "Photos", "This capture's record couldn't be read, so what each was for isn't known")

        val walk = scenery.filter { it.startsWith("scn_") }.sorted()
        val pano = scenery.filter { !it.startsWith("scn_") }.sorted()
        if (walk.isNotEmpty()) out.add(ContentGroup("walk", "The scenery round the rock", "A walk round it, for the site's backdrop: never meshed", walk, scenery = true))
        if (pano.isNotEmpty()) out.add(ContentGroup("pano", "A panorama", "From the turn on the spot at the start, for the site's backdrop", pano, scenery = true))

        val files = ArrayList<Pair<String, String>>()
        fun about(name: String, what: String) { others[name]?.let { files.add(name to "$what (${size(it)})") } }
        about("capture.json", "Each photo's readings (where the phone was, which way it faced and which way was down), how the mesh was lined up, and what each photo was for")
        about("points.bin", "What the phone's tracking found of the rock, as points")
        about("capture-log.jsonl", "How the capture went on the phone, for working out what went wrong if anything did")
        if (files.isNotEmpty()) out.add(ContentGroup("readings", "Readings", "Sent beside the photos", files = files))
        return out
    }

    /** A file's size in words: "840 kB", "3.1 MB". */
    fun size(bytes: Long): String = when {
        bytes < 1_000 -> "$bytes bytes"
        bytes < 1_000_000 -> "${bytes / 1_000} kB"
        else -> "${bytes / 100_000 / 10.0} MB"
    }

    /** What a photo's readings say, a line each, for its own screen. */
    fun readings(shot: ShotRecord?): List<String> {
        val b = shot?.blob ?: return emptyList()
        val out = ArrayList<String>()
        b.gps?.let { out.add("Where: ${round(it.lat, 6)}, ${round(it.lng, 6)}, to about ${round(it.acc, 1)} m") }
        b.heading?.let { out.add("Facing ${it.toInt()}° from north") }
        b.down?.let { out.add("Which way was down: known" + (b.downSrc?.let { s -> mapOf("turn" to " from the turn on the spot", "ar" to " from the tracking", "accel" to " from the accelerometer")[s] ?: "" } ?: "")) }
        b.q?.let { out.add("Sharpness: blurred by ${round(it.blurPx, 1)} pixels" + if (it.ok) "" else " (too much)") }
        out.addAll(shot.notes)
        return out
    }

    private fun round(x: Double, places: Int): Double {
        var f = 1.0; repeat(places) { f *= 10 }
        return kotlin.math.round(x * f) / f
    }
}

/** What the server says of one of this phone's uploads (its /api/capture/status). */
@Serializable
data class UploadStatus(val status: String = "", val mesh: String? = null, val approved: Boolean = false, val left: List<String> = emptyList(),
                        val failure: String? = null, @SerialName("mesh_gone") val meshGone: Boolean = false) {
    /** How it stands, in words, and how it's coloured: "good", "bad", "warn" or "wait". */
    fun said(): Pair<String, String> = when (status) {
        "done" -> if (mesh != null && !meshGone) ("Meshed" + (if (left.isNotEmpty()) "; ${left.joinToString(", ")}" else "") +
            if (approved) "" else ". Only you can see it until it's approved") to "good"
            else "Finished, but no mesh came of it" to "bad"
        "new", "queued", "pending", "held", "waiting", "harvested" -> "Waiting its turn to be meshed" to "wait"
        "running", "harvesting" -> "Being meshed now" to "wait"
        "failed" -> ("Meshing failed" + (failure?.let { ": $it" } ?: "")) to "bad"
        "interrupted" -> "Meshing was interrupted: it will be tried again" to "warn"
        "stopped", "stopping" -> "Meshing was stopped on the server" to "warn"
        "gone" -> "It isn't on the server any more" to "bad"
        else -> "On the server ($status)" to "wait"
    }
}

/**
 * One of the account's captures as the server keeps it (an entry of its /api/account/projects): listed by the apps when
 * it isn't among the phone's own, as after the app was uninstalled, or for one sent from another phone.
 */
@Serializable
data class SentCapture(val id: String, val name: String = "", val created: Double = 0.0, val status: String = "", val mesh: String? = null,
                       val photos: Int = 0) {
    /** Its name without the " (capture)" the apps add to a new rock's. */
    val title: String get() = name.removeSuffix(" (capture)").ifEmpty { id }

    companion object {
        /** Those of `all` that aren't any of the phone's own captures (`here`: their projects), newest first. */
        fun notOnPhone(all: List<SentCapture>, here: Collection<String?>): List<SentCapture> {
            val have = here.filterNotNull().toSet()
            return all.filter { it.id !in have }.sortedByDescending { it.created }
        }
    }
}

/** /api/account/projects, as the apps read it. */
@Serializable
data class AccountProjects(val projects: List<SentCapture> = emptyList())
