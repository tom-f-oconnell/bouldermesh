package sh.boulderme.ios

import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import platform.Foundation.NSData
import platform.Foundation.NSHTTPURLResponse
import platform.Foundation.NSMutableData
import platform.Foundation.NSMutableURLRequest
import platform.Foundation.NSURL
import platform.Foundation.NSURLSession
import platform.Foundation.appendData
import platform.Foundation.dataTaskWithRequest
import platform.Foundation.dataWithContentsOfFile
import platform.Foundation.setHTTPBody
import platform.Foundation.setHTTPMethod
import platform.Foundation.setValue
import platform.darwin.dispatch_async
import platform.darwin.dispatch_get_main_queue
import sh.boulderme.core.MeshRow

/**
 * The server, as the app talks to it. Nothing here blocks: each call answers later, on the main thread, with what it
 * got or else why not, in words.
 */
class IosApi(private val store: IosStore) {
    private val json = IosStore.json

    private fun call(path: String, method: String = "GET", body: NSData? = null, type: String? = null, done: (ByteArray?, String?) -> Unit) {
        val url = NSURL.URLWithString(store.server + path)
        if (url == null) { done(null, "the server's address isn't one"); return }
        val req = NSMutableURLRequest.requestWithURL(url)
        req.setHTTPMethod(method)
        req.setTimeoutInterval(120.0)
        req.setValue("BoulderMeshApp/${IosStore.VERSION} (iOS)", forHTTPHeaderField = "User-Agent")
        if (store.token.isNotEmpty()) req.setValue("Bearer ${store.token}", forHTTPHeaderField = "Authorization")
        if (type != null) req.setValue(type, forHTTPHeaderField = "Content-Type")
        if (body != null) req.setHTTPBody(body)
        NSURLSession.sharedSession.dataTaskWithRequest(req) { data, response, error ->
            val code = (response as? NSHTTPURLResponse)?.statusCode?.toInt() ?: 0
            val bytes = data?.toBytes() ?: ByteArray(0)
            val why = when {
                error != null -> error.localizedDescription
                code !in 200..299 -> runCatching { json.parseToJsonElement(bytes.decodeToString()).jsonObject["error"]?.jsonPrimitive?.content }.getOrNull() ?: "the server answered $code"
                else -> null
            }
            dispatch_async(dispatch_get_main_queue()) { if (why == null) done(bytes, null) else done(null, why) }
        }.resume()
    }

    private fun post(path: String, body: String, done: (JsonObject?, String?) -> Unit) =
        call(path, "POST", body.encodeToByteArray().toData(), "application/json") { bytes, why ->
            val obj = bytes?.let { runCatching { json.parseToJsonElement(it.decodeToString()).jsonObject }.getOrNull() }
            if (obj != null) done(obj, null) else done(null, why ?: "the server's answer couldn't be read")
        }

    private fun quoted(s: String) = JsonPrimitive(s).toString()

    /** The current meshes made from photos, which a capture can add to. */
    fun meshes(done: (List<MeshRow>?, String?) -> Unit) = call("/api/capture/meshes") { bytes, why ->
        val rows = bytes?.let { runCatching { json.decodeFromString(ListSerializer(MeshRow.serializer()), it.decodeToString()) }.getOrNull() }
        if (rows != null) done(rows, null) else done(null, why ?: "the list couldn't be read")
    }

    /** A mesh's plan and guide, as text and bytes, to keep. */
    fun plan(mesh: String, done: (String?, ByteArray?, String?) -> Unit) = call("/api/capture/plan/$mesh") { bytes, why ->
        val text = bytes?.decodeToString()
        val url = text?.let { runCatching { json.parseToJsonElement(it).jsonObject["guide_url"]?.jsonPrimitive?.content }.getOrNull() }
        if (text == null || url == null) done(null, null, why ?: "the plan names no guide")
        else call(url) { guide, whyNot -> if (guide != null) done(text, guide, null) else done(null, null, whyNot) }
    }

    /** The editing token for a pairing code or link from the admin page's QR. */
    fun pair(codeOrLink: String, done: (String?, String?) -> Unit) {
        val code = Regex("pair=([\\w-]{16,})").find(codeOrLink)?.groupValues?.get(1) ?: codeOrLink.trim()
        post("/api/pair", "{\"code\":${quoted(code)}}") { obj, why -> val t = obj?.get("token")?.jsonPrimitive?.content; if (t != null) done(t, null) else done(null, why ?: "the server gave no token") }
    }

    fun createProject(name: String, done: (String?, String?) -> Unit) =
        post("/api/projects", "{\"name\":${quoted(name)}}") { obj, why -> val id = obj?.get("id")?.jsonPrimitive?.content; if (id != null) done(id, null) else done(null, why ?: "the server made no project") }

    /** One photo, as a form upload. Answers why it failed, or null. */
    fun uploadPhoto(project: String, path: String, name: String, done: (String?) -> Unit) {
        val file = NSData.dataWithContentsOfFile(path)
        if (file == null) { done("$name can't be read"); return }
        val boundary = "----bouldermesh${name.hashCode().toUInt()}${file.length}"
        val body = NSMutableData()
        body.appendData("--$boundary\r\nContent-Disposition: form-data; name=\"photos\"; filename=\"$name\"\r\nContent-Type: image/jpeg\r\n\r\n".encodeToByteArray().toData())
        body.appendData(file)
        body.appendData("\r\n--$boundary--\r\n".encodeToByteArray().toData())
        call("/api/projects/$project/photos", "POST", body, "multipart/form-data; boundary=$boundary") { bytes, why ->
            done(if (bytes == null) why ?: "it wasn't sent" else if (name !in bytes.decodeToString()) "the server didn't keep $name" else null)
        }
    }

    /** The points the phone's tracking found during the capture, as CaptureFlow.points() wrote them. Answers why it failed, or null. */
    fun sendPoints(project: String, path: String, done: (String?) -> Unit) {
        val file = NSData.dataWithContentsOfFile(path)
        if (file == null) done("its points can't be read")
        else call("/api/projects/$project/capture/points", "POST", file, "application/octet-stream") { bytes, why -> done(if (bytes != null) null else why ?: "they weren't sent") }
    }

    fun sendRecord(project: String, record: String, done: (String?) -> Unit) = post("/api/projects/$project/capture", record) { obj, why -> done(if (obj != null) null else why) }
    fun reconstruct(project: String, done: (String?) -> Unit) = post("/api/projects/$project/reconstruct", "{}") { obj, why -> done(if (obj != null) null else why) }
}
