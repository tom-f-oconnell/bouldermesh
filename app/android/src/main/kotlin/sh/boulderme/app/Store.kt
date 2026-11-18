package sh.boulderme.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.builtins.MapSerializer
import kotlinx.serialization.builtins.serializer
import sh.boulderme.core.CaptureContents
import sh.boulderme.core.CaptureRecord
import sh.boulderme.core.ContentGroup
import sh.boulderme.core.GuideMesh
import sh.boulderme.core.SentCapture
import sh.boulderme.core.UploadStatus
import sh.boulderme.core.Plan
import java.io.File

/** What the app keeps on the phone: its settings, the plans and guides downloaded for use with no signal, and captures. */
class Store(context: Context) {
    private val connectivity = context.getSystemService(ConnectivityManager::class.java)
    private val prefs = context.getSharedPreferences("bouldermesh", Context.MODE_PRIVATE)
    private val root: File = context.getExternalFilesDir(null) ?: context.filesDir
    private val plans = File(root, "plans").apply { mkdirs() }
    val captures = File(root, "captures").apply { mkdirs() }

    var terms: Int          // the version of the terms accepted on this phone (0: none): nothing else works until the current one is
        get() = prefs.getInt("terms", 0)
        set(v) = prefs.edit().putInt("terms", v).apply()
    var signInAsked: Boolean    // the first-opening prompt to sign in (SignInScreen) was left once: not shown again
        get() = prefs.getBoolean("sign_in_asked", false)
        set(v) = prefs.edit().putBoolean("sign_in_asked", v).apply()
    var anonFirst: Boolean  // holds others marked privately are asked for from the start of a capture, not at its end
        get() = prefs.getBoolean("anon_first", false)
        set(v) = prefs.edit().putBoolean("anon_first", v).apply()
    var dingKept: Boolean   // a soft bell each time a photo is kept
        get() = prefs.getBoolean("ding_kept", false)
        set(v) = prefs.edit().putBoolean("ding_kept", v).apply()
    var tickSame: Boolean   // a tick over and over while a photo from where the phone is would add nothing
        get() = prefs.getBoolean("tick_same", false)
        set(v) = prefs.edit().putBoolean("tick_same", v).apply()
    var topHalf: Boolean    // the account's role (HALF_ROLE) has the app drawn in the screen's top half only
        get() = prefs.getBoolean("top_half", false)
        set(v) { prefs.edit().putBoolean("top_half", v).apply(); onTopHalf?.invoke(v) }
    var onTopHalf: ((Boolean) -> Unit)? = null      // the activity, to redraw at once
    var credit: String      // who photos are credited to, under the licence they're sent with
        get() = prefs.getString("credit", "") ?: ""
        set(v) = prefs.edit().putString("credit", v.trim()).apply()
    var server: String
        get() = prefs.getString("server", DEFAULT_SERVER) ?: DEFAULT_SERVER
        set(v) = prefs.edit().putString("server", v.trim().trimEnd('/')).apply()
    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(v) = prefs.edit().putString("token", v.trim()).apply()

    var downloadWifiOnly: Boolean   // meshes are got only over Wi-Fi, waiting for it if need be
        get() = prefs.getBoolean("download_wifi_only", false)
        set(v) { prefs.edit().putBoolean("download_wifi_only", v).apply(); changed() }
    var uploadWifiOnly: Boolean     // captures are sent only over Wi-Fi, waiting for it if need be
        get() = prefs.getBoolean("upload_wifi_only", false)
        set(v) { prefs.edit().putBoolean("upload_wifi_only", v).apply(); changed() }

    /** Whether the phone is on Wi-Fi (or a cable) that isn't metered, as a phone's hotspot is: what "only over Wi-Fi" waits for. */
    fun onWifi(): Boolean {
        val caps = connectivity?.let { it.getNetworkCapabilities(it.activeNetwork) } ?: return false
        return (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) || caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) &&
            caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
    }

    /** Whether the phone has a connection that reaches the internet: nothing is asked of the server without one, unless pressed for. */
    fun online(): Boolean {
        val caps = connectivity?.let { it.getNetworkCapabilities(it.activeNetwork) } ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
    }

    // waiting for Wi-Fi sleeps until Android says the network changed (or a Wi-Fi-only setting did), rather than looking
    // every few seconds: `changes` counts those, for a waiter to sleep only if none came since it last looked
    private val lock = Object()
    @Volatile var changes = 0L
        private set
    private fun changed() = synchronized(lock) { changes++; lock.notifyAll() }
    private val watching by lazy {
        runCatching {
            connectivity?.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) = changed()
                override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) = changed()
                override fun onLost(network: Network) = changed()
            })
        }
    }

    /** Blocks until the network or a Wi-Fi-only setting has changed since `seen` (a value of `changes`), or `ms` at most. */
    fun awaitChange(seen: Long, ms: Long = 30_000) {
        watching
        synchronized(lock) { if (changes == seen) lock.wait(ms) }
    }

    /**
     * Blocks until meshes may be got: at once, unless they're got only over Wi-Fi and it isn't on it. Calls `waiting`
     * first if it has to wait; raises CancellationException once `alive` says the one waiting has gone (looked at each
     * time the network changes, and every 30 s).
     */
    fun awaitDownloads(alive: () -> Boolean, waiting: () -> Unit = {}) {
        var told = false
        while (true) {
            val seen = changes
            if (!downloadWifiOnly || onWifi()) return
            if (!alive()) throw kotlinx.coroutines.CancellationException("no longer wanted")
            if (!told) { told = true; waiting() }
            awaitChange(seen)
        }
    }

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
    /** A copy of the plan a capture goes by, kept with it: what each of its photos was for is told from it, after the plan's gone. */
    fun keepPlan(id: String, mesh: String) = runCatching { File(plans, "$mesh.json").copyTo(File(captureDir(id), "plan.json"), overwrite = true) }

    fun removePlan(mesh: String) { File(plans, "$mesh.json").delete(); File(plans, "$mesh.bin").delete(); offline.remove(mesh) }
    /** Meshes kept for the viewer with no signal. */
    val offline = OfflineStore(File(root, "viewer"))
    fun downloaded(): List<Plan> = (plans.listFiles { f -> f.name.endsWith(".json") } ?: emptyArray())
        .mapNotNull { f -> runCatching { Plan.parse(f.readText()) }.getOrNull() }.sortedBy { it.name.lowercase() }

    // ---- captures -----------------------------------------------------------------------------------------------------
    /** Reports of the app crashing, written as it did (Crashes), each sent with the next capture uploaded and then gone. */
    val crashes: File get() = File(root, "crashes").apply { mkdirs() }
    fun captureDir(id: String) = File(captures, id).apply { File(this, "photos").mkdirs() }

    @Serializable
    data class Upload(val name: String, val mesh: String? = null, val project: String? = null, val sent: List<String> = emptyList(),
                      val recordSent: Boolean = false, val pointsSent: Boolean = false, val logSent: Boolean = false,
                      val scenerySent: List<String> = emptyList(), val sceneryDone: Boolean = false, val meshAsked: Boolean = false, val done: Boolean = false,
                      val photos: Int = 0, val note: String = "")

    fun upload(id: String): Upload? = runCatching { json.decodeFromString(Upload.serializer(), File(captureDir(id), "upload.json").readText()) }.getOrNull()
    fun saveUpload(id: String, u: Upload) = File(captureDir(id), "upload.json").writeText(json.encodeToString(Upload.serializer(), u))
    fun captureIds(): List<String> = (captures.listFiles { f -> f.isDirectory && File(f, "upload.json").exists() } ?: emptyArray()).sortedByDescending { it.lastModified() }.map { it.name }
    fun deleteCapture(id: String) { File(captures, id).deleteRecursively() }

    /** What a capture holds, grouped by what each part was for (CaptureContents), with its record if it can be read. */
    fun contents(id: String): Pair<List<ContentGroup>, CaptureRecord?> {
        val dir = File(captures, id)
        val record = runCatching { json.decodeFromString(CaptureRecord.serializer(), File(dir, "capture.json").readText()) }.getOrNull()
        val plan = runCatching { Plan.parse(File(dir, "plan.json").readText()) }.getOrNull()
            ?: record?.mesh?.let { plan(it) }?.takeIf { p -> record.plan?.key.let { it == null || it == p.key } }
        fun jpgs(sub: String) = (File(dir, sub).listFiles() ?: emptyArray()).filter { it.name.endsWith(".jpg") }.map { it.name }
        val others = listOf("capture.json", "points.bin", "capture-log.jsonl").associateWith { File(dir, it).length() }.filterValues { it > 0 }
        return CaptureContents.group(record, plan, jpgs("photos"), jpgs("scenery"), others) to record
    }

    /** What the server last said of each upload (UploadStatus, by project id), and when it was asked (Unix ms; 0: never). */
    var statuses: Map<String, UploadStatus>
        get() = runCatching { json.decodeFromString(STATUSES, prefs.getString("statuses", "{}") ?: "{}") }.getOrDefault(emptyMap())
        set(v) = prefs.edit().putString("statuses", json.encodeToString(STATUSES, v)).putLong("statuses_at", System.currentTimeMillis()).apply()
    val statusesAt: Long get() = prefs.getLong("statuses_at", 0)
    /** The account's captures on the server that aren't on this phone, as last heard (Statuses). */
    var sent: List<SentCapture>
        get() = runCatching { json.decodeFromString(SENT, prefs.getString("sent", "[]") ?: "[]") }.getOrDefault(emptyList())
        set(v) = prefs.edit().putString("sent", json.encodeToString(SENT, v)).apply()

    companion object {
        const val DEFAULT_SERVER = "https://boulderme.sh"
        val json = Json { ignoreUnknownKeys = true; encodeDefaults = true; coerceInputValues = true }
        private val STATUSES = MapSerializer(String.serializer(), UploadStatus.serializer())
        private val SENT = ListSerializer(SentCapture.serializer())
    }
}
