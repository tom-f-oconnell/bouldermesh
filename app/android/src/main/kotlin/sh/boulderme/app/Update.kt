package sh.boulderme.app

import android.app.PendingIntent
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.provider.Settings
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * The app updating itself from the newest build CI published (.github/workflows/apps.yml: the release android-latest,
 * titled "Android app, latest (<commit>, build <n>)", its APK as bouldermesh.apk, and latest.json saying the same).
 * Every build is signed with the same key, so the new one installs over this one, keeping its captures; Android still
 * asks the person to confirm each install, as it does for any app from outside an app store.
 */
object Updates {
    const val RELEASE = "https://api.github.com/repos/tom-f-oconnell/bouldermesh/releases/tags/android-latest"
    const val LATEST = "https://github.com/tom-f-oconnell/bouldermesh/releases/download/android-latest/latest.json"
    const val APK = "https://github.com/tom-f-oconnell/bouldermesh/releases/download/android-latest/bouldermesh.apk"
    const val ACTION = "sh.boulderme.app.UPDATE_STATUS"

    /** A newer build than this one: its build number, commit, and where its APK is. */
    data class Newer(val build: Int, val commit: String, val apk: String, val bytes: Long)

    /**
     * What the Home screen and Settings say: the newer build found, if any, and how installing it is going; and the
     * last look's outcome (checked: when, or null if never; looking: one under way; failed: why it couldn't look).
     */
    data class State(val newer: Newer? = null, val busy: String? = null, val error: String? = null,
                     val checked: Long? = null, val looking: Boolean = false, val failed: String? = null)
    val state = MutableStateFlow(State())

    private val json = Json { ignoreUnknownKeys = true }

    /**
     * The newest published build, if it's newer than this one (by build number). Blocks: call off the main thread.
     * Reads the release's latest.json, a download like the APK's, which GitHub doesn't count against its API's limit
     * of 60 requests an hour per address (a phone on mobile data shares its address with many others, so the API often
     * refuses it with 403); the API only for a release from before latest.json was published with it.
     */
    fun check(): Newer? {
        val c = open(LATEST)
        if (c.responseCode == 200) {
            val o = json.parseToJsonElement(c.inputStream.use { it.readBytes() }.decodeToString()).jsonObject
            val build = o["build"]!!.jsonPrimitive.content.toInt()
            if (build <= BuildConfig.BUILD) return null
            return Newer(build, o["commit"]?.jsonPrimitive?.content ?: "", APK, o["bytes"]?.jsonPrimitive?.long ?: 0)
        }
        if (c.responseCode != 404) throw IOException(refused("the newest build couldn't be looked up", c))
        val r = open(RELEASE).apply { setRequestProperty("Accept", "application/vnd.github+json") }
        if (r.responseCode == 404) throw IOException("no build is published just now (one may be on its way): try again in a few minutes")
        if (r.responseCode != 200) throw IOException(refused("the newest build couldn't be looked up", r))
        val rel = json.parseToJsonElement(r.inputStream.use { it.readBytes() }.decodeToString()).jsonObject
        val title = rel["name"]?.jsonPrimitive?.content ?: ""
        val m = Regex("\\(([0-9a-f]{7,}), build (\\d+)\\)").find(title) ?: return null     // a build from before builds were numbered
        val asset = rel["assets"]?.jsonArray?.map { it.jsonObject }?.firstOrNull { it["name"]?.jsonPrimitive?.content == "bouldermesh.apk" }
            ?: return null
        val build = m.groupValues[2].toInt()
        if (build <= BuildConfig.BUILD) return null
        return Newer(build, m.groupValues[1], asset["browser_download_url"]!!.jsonPrimitive.content, asset["size"]?.jsonPrimitive?.long ?: 0)
    }

    private fun open(url: String) = (URL(url).openConnection() as HttpURLConnection).apply {
        instanceFollowRedirects = true; connectTimeout = 15_000; readTimeout = 30_000
        setRequestProperty("User-Agent", "BoulderMeshApp/${BuildInfo.VERSION} (Android)")
    }

    /** What a refusal says: its status, and GitHub's own reason (e.g. its rate limit) if it gave one. */
    private fun refused(what: String, c: HttpURLConnection): String {
        val why = runCatching { c.errorStream?.use { it.readBytes() }?.decodeToString() }.getOrNull()
            ?.let { runCatching { json.parseToJsonElement(it).jsonObject["message"]?.jsonPrimitive?.content }.getOrNull() }
        val limited = c.responseCode == 403 && c.getHeaderField("x-ratelimit-remaining") == "0"
        return "$what (${c.responseCode}" + (if (limited) ": GitHub's limit for this network, try again within the hour" else why?.let { ": ${it.take(120)}" } ?: "") + ")"
    }

    /**
     * Looks for a newer build in the background, keeping what it found (or why it couldn't) in [state]. With
     * unlessWithinMs, not if the last look was that recent: the app looks each time it comes to the front.
     */
    fun look(unlessWithinMs: Long = 0) {
        val now = System.currentTimeMillis()
        val s = state.value
        if (s.looking || (s.checked != null && now - s.checked < unlessWithinMs)) return
        state.value = s.copy(looking = true)
        Thread {
            val got = runCatching { check() }
            state.value = state.value.copy(looking = false, checked = System.currentTimeMillis(),
                newer = got.getOrNull() ?: state.value.newer.takeIf { got.isFailure },
                failed = got.exceptionOrNull()?.let { it.message ?: it.javaClass.simpleName })
        }.start()
    }

    /**
     * Downloads the newer build and hands it to Android's installer, which asks the person to confirm. If this app
     * may not install apps yet, opens the setting that allows it instead, to come back and press Update again.
     */
    fun install(a: MainActivity, newer: Newer) {
        if (!a.packageManager.canRequestPackageInstalls()) {
            state.value = state.value.copy(busy = null, error = "Allow BoulderMesh to install apps (the setting just opened), then press Update again.")
            a.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${a.packageName}")))
            return
        }
        Thread {
            try {
                val dir = File(a.cacheDir, "update").apply { mkdirs() }
                val apk = File(dir, "bouldermesh.apk")
                val c = URL(newer.apk).openConnection() as HttpURLConnection      // GitHub sends it on to its file host, both https
                c.instanceFollowRedirects = true; c.connectTimeout = 15_000; c.readTimeout = 60_000
                if (c.responseCode != 200) throw IOException("the download was refused (${c.responseCode})")
                val total = c.contentLengthLong.takeIf { it > 0 } ?: newer.bytes
                c.inputStream.use { input ->
                    apk.outputStream().use { out ->
                        val buf = ByteArray(1 shl 16); var got = 0L; var n: Int
                        while (input.read(buf).also { n = it } >= 0) {
                            out.write(buf, 0, n); got += n
                            if (total > 0) state.value = state.value.copy(busy = "Downloading: ${100 * got / total}%")
                        }
                    }
                }
                state.value = state.value.copy(busy = "Installing…")
                val installer = a.packageManager.packageInstaller
                val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply { setAppPackageName(a.packageName) }
                val id = installer.createSession(params)
                installer.openSession(id).use { s ->
                    s.openWrite("bouldermesh.apk", 0, apk.length()).use { out -> apk.inputStream().use { it.copyTo(out) }; s.fsync(out) }
                    val back = Intent(a, MainActivity::class.java).setAction(ACTION).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
                    s.commit(PendingIntent.getActivity(a, id, back, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE).intentSender)
                }
            } catch (e: Exception) {
                state.value = state.value.copy(busy = null, error = "The update failed: ${e.message}")
            }
        }.start()
    }

    /** What Android's installer said, delivered to MainActivity: its confirmation screen to show, or how it ended. */
    fun installerSaid(a: MainActivity, intent: Intent) {
        when (val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION ->
                androidx.core.content.IntentCompat.getParcelableExtra(intent, Intent.EXTRA_INTENT, Intent::class.java)
                    ?.let { a.startActivity(it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
            PackageInstaller.STATUS_SUCCESS -> state.value = State()     // the new build starts in this one's place
            else -> state.value = state.value.copy(busy = null, error = "The update didn't install" +
                (intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)?.let { ": $it" } ?: " ($status)") +
                if (status == PackageInstaller.STATUS_FAILURE_CONFLICT || status == PackageInstaller.STATUS_FAILURE_INCOMPATIBLE)
                    ". This copy was signed by another machine: uninstall it once, then install from the site." else "")
        }
    }
}
