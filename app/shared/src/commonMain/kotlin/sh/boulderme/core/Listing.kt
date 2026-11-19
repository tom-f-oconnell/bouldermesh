package sh.boulderme.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** A mesh as the server lists it for capturing more of (its /api/capture/meshes). */
@Serializable
data class MeshRow(val id: String, val name: String = "", val lat: Double? = null, val lng: Double? = null,
                   val climbs: List<String> = emptyList(), val holds: Int = 0,
                   val bytes: OfflineSizes? = null)     // what keeping it for the viewer takes, without its textures and with

/**
 * What the server says of a mesh kept on the phone (its /api/capture/fresh): its plan's key now, the newer mesh a rebuild
 * made of it, or that it's gone.
 */
@Serializable
data class MeshFresh(val key: String? = null, val newer: String? = null, @SerialName("newer_name") val newerName: String? = null,
                     val gone: Boolean = false) {
    /** Whether there's a newer copy to get than the one here, whose plan went by `have` (Plan.key). */
    fun stale(have: String): Boolean = newer != null || (key != null && key != have)

    /** How the copy here stands, in words; null if it's as the server has it. */
    fun said(have: String): String? = when {
        newer != null -> "Rebuilt on the server: a newer mesh replaces this"
        key != null && key != have -> "Changed on the server since it was got: its holds, beta, mesh or size"
        gone -> "No longer on the server: the copy here still works"
        else -> null
    }

    /** What its button says. */
    val action: String get() = if (newer != null) "Get the newer mesh" else "Update"

    companion object {
        /**
         * Whether a mesh a newer one replaced may go from the phone once the newer is got: not while a capture still to be
         * sent was taken to add to it (`unsent`: those captures' meshes), as carrying it on goes by its plan.
         */
        fun mayDrop(old: String, unsent: Collection<String?>): Boolean = old !in unsent
    }
}

/** How far a capture's upload has got, kept beside its photos so that one cut off carries on from there. */
@Serializable
data class UploadState(val name: String, val mesh: String? = null, val project: String? = null, val sent: List<String> = emptyList(),
                       val recordSent: Boolean = false, val pointsSent: Boolean = false, val logSent: Boolean = false,
                       val scenerySent: List<String> = emptyList(), val sceneryDone: Boolean = false, val meshAsked: Boolean = false, val done: Boolean = false, val photos: Int = 0, val note: String = "")
