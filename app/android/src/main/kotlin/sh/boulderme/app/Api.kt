package sh.boulderme.app

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import sh.boulderme.core.MeshRow
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** The server, as the app talks to it. Every call blocks: call from a background thread. Failures are IOExceptions, in words. */
class Api(private val store: Store) {
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
        val code = Regex("pair=([\\w-]{16,})").find(codeOrLink)?.groupValues?.get(1) ?: codeOrLink.trim()
        if ("pair=" !in codeOrLink) runCatching {
            postJson("/api/account/pair/take", "{\"code\":${q(code)},\"label\":${q(android.os.Build.MODEL)}}")["token"]?.jsonPrimitive?.content
        }.getOrNull()?.let { return it }
        return postJson("/api/pair", "{\"code\":${Json.encodeToString(kotlinx.serialization.serializer<String>(), code)}}")["token"]?.jsonPrimitive?.content
            ?: throw IOException("the server gave no token")
    }

    /** Tells the server this phone's account accepts the terms, as it did on the phone: nothing, for the site's own token. */
    fun acceptTerms() { runCatching { postJson("/api/account/terms", "{\"version\":${sh.boulderme.core.TERMS_VERSION}}") } }

    fun createProject(name: String): String =
        postJson("/api/projects", "{\"name\":${Json.encodeToString(kotlinx.serialization.serializer<String>(), name)}}")["id"]?.jsonPrimitive?.content
            ?: throw IOException("the server made no project")

    /** One photo, as a form upload. */
    fun uploadPhoto(project: String, file: File) {
        val boundary = "----bouldermesh${System.nanoTime()}"
        val c = open("/api/projects/$project/photos", "POST")
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
    val APP = "android $VERSION"
}
