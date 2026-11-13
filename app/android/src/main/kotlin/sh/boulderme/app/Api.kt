package sh.boulderme.app

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** A mesh as the server lists it for capturing more of (its /api/capture/meshes). */
@Serializable
data class MeshRow(val id: String, val name: String = "", val lat: Double? = null, val lng: Double? = null,
                   val climbs: List<String> = emptyList(), val holds: Int = 0)

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

    /** The editing token for a pairing code or link from the admin page's QR. */
    fun pair(codeOrLink: String): String {
        val code = Regex("pair=([\\w-]{16,})").find(codeOrLink)?.groupValues?.get(1) ?: codeOrLink.trim()
        return postJson("/api/pair", "{\"code\":${Json.encodeToString(kotlinx.serialization.serializer<String>(), code)}}")["token"]?.jsonPrimitive?.content
            ?: throw IOException("the server gave no token")
    }

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
    fun reconstruct(project: String) { postJson("/api/projects/$project/reconstruct", "{}") }
}

object BuildInfo {
    const val VERSION = "0.1.0"
    val APP = "android $VERSION"
}
