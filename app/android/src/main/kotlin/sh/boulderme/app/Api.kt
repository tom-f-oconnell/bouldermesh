package sh.boulderme.app

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.builtins.MapSerializer
import kotlinx.serialization.builtins.serializer
import sh.boulderme.core.HALF_ROLE
import sh.boulderme.core.MeshRow
import sh.boulderme.core.UploadStatus
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** The server, as the app talks to it. Every call blocks: call from a background thread. Failures are IOExceptions, in words. */
class Api(internal val store: Store) {
    private val json = Json { ignoreUnknownKeys = true; coerceInputValues = true }

    private fun open(path: String, method: String = "GET"): HttpURLConnection {
        val c = URL(store.server + path).openConnection() as HttpURLConnection
        c.requestMethod = method
        c.connectTimeout = 15_000; c.readTimeout = 120_000
        c.setRequestProperty("User-Agent", "BoulderMeshApp/${BuildInfo.VERSION} (Android)")
        if (store.token.isNotEmpty()) c.setRequestProperty("Authorization", "Bearer ${store.token}")
        return c
    }

    private fun read(c: HttpURLConnection): ByteArray {
        val code = c.responseCode
        val body = (if (code in 200..299) c.inputStream else c.errorStream)?.use { it.readBytes() } ?: ByteArray(0)
        if (code !in 200..299) {
            val said = runCatching { json.parseToJsonElement(body.decodeToString()).jsonObject["error"]?.jsonPrimitive?.content }.getOrNull()
            throw IOException(said ?: "the server answered $code")
        }
        return body
    }

    private fun postJson(path: String, body: String): JsonObject {
        val c = open(path, "POST")
        c.doOutput = true
        c.setRequestProperty("Content-Type", "application/json")
        c.outputStream.use { it.write(body.toByteArray()) }
        return json.parseToJsonElement(read(c).decodeToString()).jsonObject
    }

    /** The current meshes made from photos, which a capture can add to. */
    fun meshes(): List<MeshRow> = json.decodeFromString<List<MeshRow>>(read(open("/api/capture/meshes")).decodeToString())

    /** How each of these uploads (project ids) stands on the server: whether it's meshed, and as what. */
    fun status(projects: Collection<String>): Map<String, UploadStatus> =
        json.decodeFromString(MapSerializer(String.serializer(), UploadStatus.serializer()), read(open("/api/capture/status?ids=" + projects.joinToString(","))).decodeToString())

    /** Whether each of these meshes kept on the phone is as the server has it now, or was rebuilt or is gone. */
    fun fresh(meshes: Collection<String>): Map<String, sh.boulderme.core.MeshFresh> =
        json.decodeFromString(MapSerializer(String.serializer(), sh.boulderme.core.MeshFresh.serializer()), read(open("/api/capture/fresh?ids=" + meshes.joinToString(","))).decodeToString())

    /**
     * The account's captures kept on the server, newest first; null where this phone's token isn't an account's (the
     * site's own) or there's none.
     */
    fun accountProjects(): List<sh.boulderme.core.SentCapture>? {
        if (store.token.isEmpty()) return null
        val c = open("/api/account/projects")
        if (c.responseCode == 401) { c.errorStream?.close(); return null }
        return json.decodeFromString(sh.boulderme.core.AccountProjects.serializer(), read(c).decodeToString()).projects
    }

    /** Every file the site's viewer fetches to show a mesh, to keep for showing it with no signal (OfflineStore). */
    fun offline(mesh: String): sh.boulderme.core.OfflineBundle = json.decodeFromString(read(open("/api/capture/offline/$mesh")).decodeToString())

    /**
     * Frames placed against a mesh on the server (ServerLocate's request), as its answer's text: within `seconds` all
     * told, or an IOException.
     */
    fun locate(mesh: String, body: String, seconds: Double): String {
        val c = open("/api/capture/locate/$mesh", "POST")
        c.connectTimeout = (seconds * 500).toInt(); c.readTimeout = (seconds * 1000).toInt()
        c.doOutput = true
        c.setRequestProperty("Content-Type", "application/json")
        val bytes = body.toByteArray()
        c.setFixedLengthStreamingMode(bytes.size)
        c.outputStream.use { it.write(bytes) }
        return read(c).decodeToString()
    }

    /** A mesh's locate pack (PhoneLocate), by its plan's locate_url; null if it names none or the pack isn't made yet. */
    fun locatePack(plan: sh.boulderme.core.Plan): ByteArray? {
        if (plan.locateUrl.isEmpty()) return null
        val c = open(plan.locateUrl)
        c.connectTimeout = 8_000; c.readTimeout = 30_000
        if (c.responseCode != 200) { c.errorStream?.close(); return null }
        return read(c)
    }

    /** A mesh's plan and guide, as text and bytes, to keep. */
    fun plan(mesh: String): Pair<String, ByteArray> {
        val text = read(open("/api/capture/plan/$mesh")).decodeToString()
        val url = json.parseToJsonElement(text).jsonObject["guide_url"]?.jsonPrimitive?.content ?: throw IOException("the plan names no guide")
        return text to read(open(url))
    }

    /**
     * A token for this phone, from what was pasted: an invite link (an account is made, under `name`), a code from an
     * account's "Sign in another device", or the admin page's pairing code or link, which gives the site's own token.
     */
    fun pair(codeOrLink: String, name: String): String {
        fun q(s: String) = Json.encodeToString(kotlinx.serialization.serializer<String>(), s)
        Regex("invite=([\\w-]{16,})").find(codeOrLink)?.groupValues?.get(1)?.let { invite ->
            if (name.isBlank()) throw IOException("set the name your photos are credited to first: the account is made under it")
            return postJson("/api/account/join", "{\"invite\":${q(invite)},\"name\":${q(name)}}")["token"]?.jsonPrimitive?.content ?: throw IOException("the server gave no token")
        }
        // an account's "Sign in another device": its QR's link (#signin=), the site's link for this app (?code=), or the code typed
        Regex("(?:signin|[?&]code)=([\\w-]{6,})").find(codeOrLink)?.groupValues?.get(1)?.let { code ->
            return postJson("/api/account/pair/take", "{\"code\":${q(code)},\"label\":${q(android.os.Build.MODEL)}}")["token"]?.jsonPrimitive?.content
                ?: throw IOException("the server gave no token")
        }
        val code = Regex("pair=([\\w-]{16,})").find(codeOrLink)?.groupValues?.get(1) ?: codeOrLink.trim()
        if ("pair=" !in codeOrLink) runCatching {
            postJson("/api/account/pair/take", "{\"code\":${q(code)},\"label\":${q(android.os.Build.MODEL)}}")["token"]?.jsonPrimitive?.content
        }.getOrNull()?.let { return it }
        return postJson("/api/pair", "{\"code\":${Json.encodeToString(kotlinx.serialization.serializer<String>(), code)}}")["token"]?.jsonPrimitive?.content
            ?: throw IOException("the server gave no token")
    }

    /**
     * The name of the account this phone's token is for, or null for the site's own token (or none). Keeps whether
     * its role draws the app in the screen's top half only (Store.topHalf), as it goes.
     */
    fun accountName(): String? = runCatching {
        val user = json.parseToJsonElement(read(open("/api/account")).decodeToString()).jsonObject["user"]
        val u = (user as? JsonObject)
        store.topHalf = u?.get("role")?.jsonPrimitive?.content == HALF_ROLE
        u?.get("name")?.jsonPrimitive?.content
    }.getOrNull()?.takeIf { it.isNotBlank() }

    /**
     * Signs this phone in with a pairing code, and if photos aren't credited to anyone yet, credits them to the
     * account's name. Returns that name, or null for the site's own token.
     */
    fun signIn(codeOrLink: String): String? {
        store.token = pair(codeOrLink, store.credit)
        return accountName()?.also { if (store.credit.isBlank()) store.credit = it }
    }

    /** A code another device signs in to this phone's account with (the site's Edit…, Sign in with a code), once, for 10 minutes. */
    fun pairCode(): String = postJson("/api/account/pair", "{}")["code"]?.jsonPrimitive?.content ?: throw IOException("the server gave no code")

    /** Tells the server this phone's account accepts the terms, as it did on the phone: nothing, for the site's own token. */
    fun acceptTerms() { runCatching { postJson("/api/account/terms", "{\"version\":${sh.boulderme.core.TERMS_VERSION}}") } }

    fun createProject(name: String): String =
        postJson("/api/projects", "{\"name\":${Json.encodeToString(kotlinx.serialization.serializer<String>(), name)}}")["id"]?.jsonPrimitive?.content
            ?: throw IOException("the server made no project")

    /** One photo, as a form upload. */
    fun uploadPhoto(project: String, file: File) = uploadTo("/api/projects/$project/photos", file)

    private fun uploadTo(path: String, file: File) {
        val boundary = "----bouldermesh${System.nanoTime()}"
        val c = open(path, "POST")
        c.doOutput = true
        c.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
        val head = "--$boundary\r\nContent-Disposition: form-data; name=\"photos\"; filename=\"${file.name}\"\r\nContent-Type: image/jpeg\r\n\r\n".toByteArray()
        val tail = "\r\n--$boundary--\r\n".toByteArray()
        c.setFixedLengthStreamingMode(head.size + file.length() + tail.size)
        c.outputStream.use { out -> out.write(head); file.inputStream().use { it.copyTo(out) }; out.write(tail) }
        val saved = json.parseToJsonElement(read(c).decodeToString()).jsonObject["saved"]?.toString() ?: ""
        if (file.name !in saved) throw IOException("the server didn't keep ${file.name}")
    }

    fun sendRecord(project: String, record: String) { postJson("/api/projects/$project/capture", record) }

    /** The points the phone's tracking found during the capture, as CaptureFlow.points() wrote them. */
    /** How a capture went on the phone (its log), or a crash's report: kind "log" or "crash". */
    fun sendLog(project: String, kind: String, file: File) {
        val c = open("/api/projects/$project/capture/$kind", "POST")
        c.doOutput = true
        c.setRequestProperty("Content-Type", "text/plain; charset=utf-8")
        c.setFixedLengthStreamingMode(file.length())
        c.outputStream.use { out -> file.inputStream().use { it.copyTo(out) } }
        read(c)
    }

    /** A frame of the scenery round the rock (CaptureFlow's grab), kept apart from the photos it's meshed from. */
    fun uploadScenery(project: String, file: File) = uploadTo("/api/projects/$project/scenery", file)

    /** All the scenery's frames are there: the server fits the backdrop once its mesh is made. */
    fun sceneryDone(project: String) { postJson("/api/projects/$project/scenery/done", "{}") }
    /** Which of the areas close by an upload is in (UploadArea.choices[k]), as its uploader says. */
    fun chooseArea(project: String, k: Int) { postJson("/api/projects/$project/area", "{\"k\": $k}") }

    fun sendPoints(project: String, file: File) {
        val c = open("/api/projects/$project/capture/points", "POST")
        c.doOutput = true
        c.setRequestProperty("Content-Type", "application/octet-stream")
        c.setFixedLengthStreamingMode(file.length())
        c.outputStream.use { out -> file.inputStream().use { it.copyTo(out) } }
        read(c)
    }
    fun reconstruct(project: String) { postJson("/api/projects/$project/reconstruct", "{}") }
}

object BuildInfo {
    const val VERSION = "0.1.0"
    val COMMIT: String = BuildConfig.COMMIT     // the commit it was built from, and CI's build number (1 elsewhere)
    val BUILD: Int = BuildConfig.BUILD
    val APP = "android $VERSION ($COMMIT, build $BUILD)"
}
