package sh.boulderme.ios

import kotlinx.serialization.json.Json
import platform.Foundation.NSUserDefaults
import sh.boulderme.core.GuideMesh
import sh.boulderme.core.Plan
import sh.boulderme.core.UploadState

/** What the app keeps on the phone: its settings, the plans and guides downloaded for use with no signal, and captures. */
class IosStore {
    private val prefs = NSUserDefaults.standardUserDefaults
    private val plans = Files.docs + "/plans"
    val captures = Files.docs + "/captures"

    init { Files.mkdirs(plans); Files.mkdirs(captures) }

    var server: String
        get() = prefs.stringForKey("server")?.takeIf { it.isNotBlank() } ?: DEFAULT_SERVER
        set(v) = prefs.setObject(v.trim().trimEnd('/'), forKey = "server")
    var token: String
        get() = prefs.stringForKey("token") ?: ""
        set(v) = prefs.setObject(v.trim(), forKey = "token")
    var magNote: String
        get() = prefs.stringForKey("mag_note") ?: ""
        set(v) = prefs.setObject(v, forKey = "mag_note")

    // ---- meshes kept for use with no signal ---------------------------------------------------------------------------
    fun savePlan(mesh: String, plan: String, guide: ByteArray) {
        Files.write("$plans/$mesh.bin", guide)
        Files.write("$plans/$mesh.json", plan)       // last, so that a plan there is one with its guide
    }

    fun plan(mesh: String): Plan? = Files.readText("$plans/$mesh.json")?.let { runCatching { Plan.parse(it) }.getOrNull() }
    fun guide(mesh: String): GuideMesh? = Files.readBytes("$plans/$mesh.bin")?.let { runCatching { GuideMesh.parse(it) }.getOrNull() }
    fun removePlan(mesh: String) { Files.delete("$plans/$mesh.json"); Files.delete("$plans/$mesh.bin") }
    fun downloaded(): List<Plan> = Files.list(plans).filter { it.endsWith(".json") }.mapNotNull { plan(it.removeSuffix(".json")) }.sortedBy { it.name.lowercase() }

    // ---- captures -----------------------------------------------------------------------------------------------------
    fun captureDir(id: String): String = "$captures/$id".also { Files.mkdirs("$it/photos") }
    fun upload(id: String): UploadState? = Files.readText("$captures/$id/upload.json")?.let { runCatching { json.decodeFromString(UploadState.serializer(), it) }.getOrNull() }
    fun saveUpload(id: String, u: UploadState) { Files.write("${captureDir(id)}/upload.json", json.encodeToString(UploadState.serializer(), u)) }
    fun captureIds(): List<String> = Files.list(captures).filter { Files.exists("$captures/$it/upload.json") }.sortedByDescending { Files.modified("$captures/$it/upload.json") }
    fun photos(id: String): List<String> = Files.list("$captures/$id/photos").filter { it.endsWith(".jpg") }
    fun deleteCapture(id: String) { Files.delete("$captures/$id") }

    companion object {
        const val DEFAULT_SERVER = "https://boulderme.sh"
        const val VERSION = "0.1.0"
        const val APP = "ios $VERSION"
        val json = Json { ignoreUnknownKeys = true; encodeDefaults = true; coerceInputValues = true }
    }
}
