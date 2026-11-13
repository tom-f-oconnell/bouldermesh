package sh.boulderme.app

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import sh.boulderme.core.GuideMesh
import sh.boulderme.core.Plan
import java.io.File

/** What the app keeps on the phone: its settings, the plans and guides downloaded for use with no signal, and captures. */
class Store(context: Context) {
    private val prefs = context.getSharedPreferences("bouldermesh", Context.MODE_PRIVATE)
    private val root: File = context.getExternalFilesDir(null) ?: context.filesDir
    private val plans = File(root, "plans").apply { mkdirs() }
    val captures = File(root, "captures").apply { mkdirs() }

    var credit: String      // who photos are credited to, under the licence they're sent with
        get() = prefs.getString("credit", "") ?: ""
        set(v) = prefs.edit().putString("credit", v.trim()).apply()
    var server: String
        get() = prefs.getString("server", DEFAULT_SERVER) ?: DEFAULT_SERVER
        set(v) = prefs.edit().putString("server", v.trim().trimEnd('/')).apply()
    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(v) = prefs.edit().putString("token", v.trim()).apply()

    var magNote: String
        get() = prefs.getString("mag_note", "") ?: ""
        set(v) = prefs.edit().putString("mag_note", v).apply()

    // ---- meshes kept for use with no signal ---------------------------------------------------------------------------
    fun savePlan(mesh: String, plan: String, guide: ByteArray) {
        File(plans, "$mesh.bin").writeBytes(guide)
        File(plans, "$mesh.json").writeText(plan)       // last, so that a plan there is one with its guide
    }

    fun plan(mesh: String): Plan? = runCatching { Plan.parse(File(plans, "$mesh.json").readText()) }.getOrNull()
    fun guide(mesh: String): GuideMesh? = runCatching { GuideMesh.parse(File(plans, "$mesh.bin").readBytes()) }.getOrNull()
    fun removePlan(mesh: String) { File(plans, "$mesh.json").delete(); File(plans, "$mesh.bin").delete() }
    fun downloaded(): List<Plan> = (plans.listFiles { f -> f.name.endsWith(".json") } ?: emptyArray())
        .mapNotNull { f -> runCatching { Plan.parse(f.readText()) }.getOrNull() }.sortedBy { it.name.lowercase() }

    // ---- captures -----------------------------------------------------------------------------------------------------
    fun captureDir(id: String) = File(captures, id).apply { File(this, "photos").mkdirs() }

    @Serializable
    data class Upload(val name: String, val mesh: String? = null, val project: String? = null, val sent: List<String> = emptyList(),
                      val recordSent: Boolean = false, val pointsSent: Boolean = false, val done: Boolean = false, val photos: Int = 0, val note: String = "")

    fun upload(id: String): Upload? = runCatching { json.decodeFromString(Upload.serializer(), File(captureDir(id), "upload.json").readText()) }.getOrNull()
    fun saveUpload(id: String, u: Upload) = File(captureDir(id), "upload.json").writeText(json.encodeToString(Upload.serializer(), u))
    fun captureIds(): List<String> = (captures.listFiles { f -> f.isDirectory && File(f, "upload.json").exists() } ?: emptyArray()).sortedByDescending { it.lastModified() }.map { it.name }
    fun deleteCapture(id: String) { File(captures, id).deleteRecursively() }

    companion object {
        const val DEFAULT_SERVER = "https://boulderme.sh"
        val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    }
}
