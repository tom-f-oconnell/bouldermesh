package sh.boulderme.core

import kotlinx.serialization.Serializable

/** A mesh as the server lists it for capturing more of (its /api/capture/meshes). */
@Serializable
data class MeshRow(val id: String, val name: String = "", val lat: Double? = null, val lng: Double? = null,
                   val climbs: List<String> = emptyList(), val holds: Int = 0)

/** How far a capture's upload has got, kept beside its photos so that one cut off carries on from there. */
@Serializable
data class UploadState(val name: String, val mesh: String? = null, val project: String? = null, val sent: List<String> = emptyList(),
                       val recordSent: Boolean = false, val pointsSent: Boolean = false, val logSent: Boolean = false, val done: Boolean = false, val photos: Int = 0, val note: String = "")
