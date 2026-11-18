package sh.boulderme.ios

import platform.Foundation.NSDate
import platform.Foundation.timeIntervalSince1970
import kotlinx.serialization.json.Json
import platform.Foundation.NSUserDefaults
import kotlinx.serialization.builtins.MapSerializer
import kotlinx.serialization.builtins.serializer
import sh.boulderme.core.CaptureContents
import sh.boulderme.core.CaptureRecord
import sh.boulderme.core.ContentGroup
import sh.boulderme.core.GuideMesh
import sh.boulderme.core.UploadStatus
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
    var terms: Int          // the version of the terms accepted on this phone (0: none): nothing else works until the current one is
        get() = prefs.integerForKey("terms").toInt()
        set(v) = prefs.setInteger(v.toLong(), forKey = "terms")
    var signInAsked: Boolean    // the first-opening prompt to sign in (Home's) was left once: not shown again
        get() = prefs.boolForKey("sign_in_asked")
        set(v) = prefs.setBool(v, forKey = "sign_in_asked")
    var anonFirst: Boolean  // holds others marked privately are asked for from the start of a capture, not at its end
        get() = prefs.boolForKey("anon_first")
        set(v) = prefs.setBool(v, forKey = "anon_first")
    var dingKept: Boolean   // a soft bell each time a photo is kept
        get() = prefs.boolForKey("ding_kept")
        set(v) = prefs.setBool(v, forKey = "ding_kept")
    var tickSame: Boolean   // a tick over and over while a photo from where the phone is would add nothing
        get() = prefs.boolForKey("tick_same")
        set(v) = prefs.setBool(v, forKey = "tick_same")
    var credit: String      // who photos are credited to, under the licence they're sent with
        get() = prefs.stringForKey("credit") ?: ""
        set(v) = prefs.setObject(v.trim(), forKey = "credit")
    var token: String
        get() = prefs.stringForKey("token") ?: ""
        set(v) = prefs.setObject(v.trim(), forKey = "token")
    var topHalf: Boolean    // the account's role (HALF_ROLE) has the app drawn in the screen's top half only
        get() = prefs.boolForKey("top_half")
        set(v) = prefs.setBool(v, forKey = "top_half")
    var downloadWifiOnly: Boolean   // meshes are got only over Wi-Fi, waiting for it if need be (Net)
        get() = prefs.boolForKey("download_wifi_only")
        set(v) = prefs.setBool(v, forKey = "download_wifi_only")
    var uploadWifiOnly: Boolean     // captures are sent only over Wi-Fi, waiting for it if need be
        get() = prefs.boolForKey("upload_wifi_only")
        set(v) = prefs.setBool(v, forKey = "upload_wifi_only")
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
    /** A copy of the plan a capture goes by, kept with it: what each of its photos was for is told from it, after the plan's gone. */
    fun keepPlan(id: String, mesh: String) { Files.readBytes("$plans/$mesh.json")?.let { Files.write("${captureDir(id)}/plan.json", it) } }

    fun removePlan(mesh: String) { Files.delete("$plans/$mesh.json"); Files.delete("$plans/$mesh.bin"); offline.remove(mesh) }
    /** Meshes kept for the viewer with no signal. */
    val offline: IosOffline by lazy { IosOffline(this) }
    fun downloaded(): List<Plan> = Files.list(plans).filter { it.endsWith(".json") }.mapNotNull { plan(it.removeSuffix(".json")) }.sortedBy { it.name.lowercase() }

    // ---- captures -----------------------------------------------------------------------------------------------------
    fun captureDir(id: String): String = "$captures/$id".also { Files.mkdirs("$it/photos") }
    fun upload(id: String): UploadState? = Files.readText("$captures/$id/upload.json")?.let { runCatching { json.decodeFromString(UploadState.serializer(), it) }.getOrNull() }
    fun saveUpload(id: String, u: UploadState) { Files.write("${captureDir(id)}/upload.json", json.encodeToString(UploadState.serializer(), u)) }
    fun captureIds(): List<String> = Files.list(captures).filter { Files.exists("$captures/$it/upload.json") }.sortedByDescending { Files.modified("$captures/$it/upload.json") }
    fun photos(id: String): List<String> = Files.list("$captures/$id/photos").filter { it.endsWith(".jpg") }
    fun scenery(id: String): List<String> = Files.list("$captures/$id/scenery").filter { it.endsWith(".jpg") }
    fun deleteCapture(id: String) { Files.delete("$captures/$id") }

    /** What a capture holds, grouped by what each part was for (CaptureContents), with its record if it can be read. */
    fun contents(id: String): Pair<List<ContentGroup>, CaptureRecord?> {
        val dir = "$captures/$id"
        val record = Files.readText("$dir/capture.json")?.let { runCatching { json.decodeFromString(CaptureRecord.serializer(), it) }.getOrNull() }
        val plan = Files.readText("$dir/plan.json")?.let { runCatching { Plan.parse(it) }.getOrNull() }
            ?: record?.mesh?.let { plan(it) }?.takeIf { p -> record.plan?.key.let { it == null || it == p.key } }
        val others = listOf("capture.json", "points.bin", "capture-log.jsonl").associateWith { Files.size("$dir/$it") }.filterValues { it > 0 }
        return CaptureContents.group(record, plan, photos(id), scenery(id), others) to record
    }

    /** What the server last said of each upload (UploadStatus, by project id), and when it was asked (Unix seconds; 0: never). */
    var statuses: Map<String, UploadStatus>
        get() = runCatching { json.decodeFromString(STATUSES, prefs.stringForKey("statuses") ?: "{}") }.getOrDefault(emptyMap())
        set(v) { prefs.setObject(json.encodeToString(STATUSES, v), forKey = "statuses"); prefs.setDouble(NSDate().timeIntervalSince1970, forKey = "statuses_at") }
    val statusesAt: Double get() = prefs.doubleForKey("statuses_at")

    companion object {
        const val DEFAULT_SERVER = "https://boulderme.sh"
        const val VERSION = "0.1.0"
        const val APP = "ios $VERSION"
        val json = Json { ignoreUnknownKeys = true; encodeDefaults = true; coerceInputValues = true }
        private val STATUSES = MapSerializer(String.serializer(), UploadStatus.serializer())
    }
}
