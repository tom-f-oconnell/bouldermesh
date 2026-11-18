package sh.boulderme.ios

import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.builtins.MapSerializer
import kotlinx.serialization.builtins.serializer
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
import sh.boulderme.core.AccountProjects
import sh.boulderme.core.MeshRow
import sh.boulderme.core.SentCapture
import sh.boulderme.core.UploadStatus

/**
 * The server, as the app talks to it. Nothing here blocks: each call answers later, on the main thread, with what it
 * got or else why not, in words.
 */
class IosApi(private val store: IosStore) {
    private val json = IosStore.json

    private fun call(path: String, method: String = "GET", body: NSData? = null, type: String? = null, done: (ByteArray?, String?) -> Unit) =
        fetch(path, method, body, type) { bytes, why, _ -> done(bytes, why) }

    /** As call(), also answering the HTTP status (0: none came). */
    private fun fetch(path: String, method: String = "GET", body: NSData? = null, type: String? = null, done: (ByteArray?, String?, Int) -> Unit) {
        val url = NSURL.URLWithString(store.server + path)
        if (url == null) { done(null, "the server's address isn't one", 0); return }
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
            dispatch_async(dispatch_get_main_queue()) { if (why == null) done(bytes, null, code) else done(null, why, code) }
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

    /** Every file the site's viewer fetches to show a mesh, to keep for showing it with no signal (IosOffline). */
    fun offline(mesh: String, done: (sh.boulderme.core.OfflineBundle?, String?) -> Unit) = call("/api/capture/offline/$mesh") { bytes, why ->
        val b = bytes?.let { runCatching { json.decodeFromString(sh.boulderme.core.OfflineBundle.serializer(), it.decodeToString()) }.getOrNull() }
        if (b != null) done(b, null) else done(null, why ?: "the server's answer couldn't be read")
    }

    /** A mesh's plan and guide, as text and bytes, to keep. */
    fun plan(mesh: String, done: (String?, ByteArray?, String?) -> Unit) = call("/api/capture/plan/$mesh") { bytes, why ->
        val text = bytes?.decodeToString()
        val url = text?.let { runCatching { json.parseToJsonElement(it).jsonObject["guide_url"]?.jsonPrimitive?.content }.getOrNull() }
        if (text == null || url == null) done(null, null, why ?: "the plan names no guide")
        else call(url) { guide, whyNot -> if (guide != null) done(text, guide, null) else done(null, null, whyNot) }
    }

    /**
     * A token for this phone, from what was pasted: an invite link (an account is made, under `name`), a code from an
     * account's "Sign in another device", or the admin page's pairing code or link, which gives the site's own token.
     */
    fun pair(codeOrLink: String, name: String, done: (String?, String?) -> Unit) {
        fun token(obj: kotlinx.serialization.json.JsonObject?) = obj?.get("token")?.jsonPrimitive?.content
        val invite = Regex("invite=([\\w-]{16,})").find(codeOrLink)?.groupValues?.get(1)
        if (invite != null) {
            if (name.isBlank()) return done(null, "set the name your photos are credited to first: the account is made under it")
            return post("/api/account/join", "{\"invite\":${quoted(invite)},\"name\":${quoted(name)}}") { obj, why -> token(obj)?.let { done(it, null) } ?: done(null, why ?: "the server gave no token") }
        }
        val code = Regex("pair=([\\w-]{16,})").find(codeOrLink)?.groupValues?.get(1) ?: codeOrLink.trim()
        if ("pair=" in codeOrLink) return pairSite(code, done)
        post("/api/account/pair/take", "{\"code\":${quoted(code)},\"label\":\"iPhone\"}") { obj, _ -> token(obj)?.let { done(it, null) } ?: pairSite(code, done) }
    }

    /** Tells the server this phone's account accepts the terms, as it did on the phone: nothing, for the site's own token. */
    /**
     * Asks who this phone's token is for, and keeps whether its role draws the app in the top half only
     * (IosStore.topHalf), crediting photos to the account's name if they're credited to nobody yet; then done(that),
     * on the main thread. Without an answer, what was kept stays.
     */
    fun account(done: (Boolean) -> Unit) = call("/api/account") { bytes, _ ->
        val user = bytes?.let { runCatching { json.parseToJsonElement(it.decodeToString()).jsonObject["user"] }.getOrNull() }
        if (bytes != null) store.topHalf = (user as? JsonObject)?.get("role")?.jsonPrimitive?.content == sh.boulderme.core.HALF_ROLE
        (user as? JsonObject)?.get("name")?.jsonPrimitive?.content?.takeIf { store.credit.isBlank() }?.let { store.credit = it }
        done(store.topHalf)
    }

    fun acceptTerms(done: () -> Unit) = post("/api/account/terms", "{\"version\":${sh.boulderme.core.TERMS_VERSION}}") { _, _ -> done() }

    private fun pairSite(code: String, done: (String?, String?) -> Unit) {
        post("/api/pair", "{\"code\":${quoted(code)}}") { obj, why -> val t = obj?.get("token")?.jsonPrimitive?.content; if (t != null) done(t, null) else done(null, why ?: "the server gave no token") }
    }

    fun createProject(name: String, done: (String?, String?) -> Unit) =
        post("/api/projects", "{\"name\":${quoted(name)}}") { obj, why -> val id = obj?.get("id")?.jsonPrimitive?.content; if (id != null) done(id, null) else done(null, why ?: "the server made no project") }

    /** One photo, as a form upload. Answers why it failed, or null. */
    fun uploadPhoto(project: String, path: String, name: String, done: (String?) -> Unit) = uploadTo("/api/projects/$project/photos", path, name, done)

    /** A frame of the scenery round the rock (CaptureFlow's grab), kept apart from the photos it's meshed from. */
    fun uploadScenery(project: String, path: String, name: String, done: (String?) -> Unit) = uploadTo("/api/projects/$project/scenery", path, name, done)

    /** All the scenery's frames are there: the server fits the backdrop once its mesh is made. */
    fun sceneryDone(project: String, done: (String?) -> Unit) = post("/api/projects/$project/scenery/done", "{}") { obj, why -> done(if (obj != null) null else why ?: "it wasn't taken") }

    private fun uploadTo(route: String, path: String, name: String, done: (String?) -> Unit) {
        val file = NSData.dataWithContentsOfFile(path)
        if (file == null) { done("$name can't be read"); return }
        val boundary = "----bouldermesh${name.hashCode().toUInt()}${file.length}"
        val body = NSMutableData()
        body.appendData("--$boundary\r\nContent-Disposition: form-data; name=\"photos\"; filename=\"$name\"\r\nContent-Type: image/jpeg\r\n\r\n".encodeToByteArray().toData())
        body.appendData(file)
        body.appendData("\r\n--$boundary--\r\n".encodeToByteArray().toData())
        call(route, "POST", body, "multipart/form-data; boundary=$boundary") { bytes, why ->
            done(if (bytes == null) why ?: "it wasn't sent" else if (name !in bytes.decodeToString()) "the server didn't keep $name" else null)
        }
    }

    /** The points the phone's tracking found during the capture, as CaptureFlow.points() wrote them. Answers why it failed, or null. */
    /** How a capture went on the phone (its log), or a crash's report: kind "log" or "crash". */
    fun sendLog(project: String, kind: String, path: String, done: (String?) -> Unit) {
        val file = NSData.dataWithContentsOfFile(path)
        if (file == null) done("it can't be read")
        else call("/api/projects/$project/capture/$kind", "POST", file, "text/plain; charset=utf-8") { bytes, why -> done(if (bytes != null) null else why ?: "it wasn't taken") }
    }

    fun sendPoints(project: String, path: String, done: (String?) -> Unit) {
        val file = NSData.dataWithContentsOfFile(path)
        if (file == null) done("its points can't be read")
        else call("/api/projects/$project/capture/points", "POST", file, "application/octet-stream") { bytes, why -> done(if (bytes != null) null else why ?: "they weren't sent") }
    }

    fun sendRecord(project: String, record: String, done: (String?) -> Unit) = post("/api/projects/$project/capture", record) { obj, why -> done(if (obj != null) null else why) }
    fun reconstruct(project: String, done: (String?) -> Unit) = post("/api/projects/$project/reconstruct", "{}") { obj, why -> done(if (obj != null) null else why) }

    /**
     * The account's captures kept on the server, newest first; or null with no reason where this phone's token isn't an
     * account's (the site's own) or there's none; or null and why it couldn't ask.
     */
    fun accountProjects(done: (List<SentCapture>?, String?) -> Unit) {
        if (store.token.isEmpty()) return done(null, null)
        fetch("/api/account/projects") { bytes, why, code ->
            if (code == 401) return@fetch done(null, null)
            val got = bytes?.let { runCatching { json.decodeFromString(AccountProjects.serializer(), it.decodeToString()).projects }.getOrNull() }
            if (got != null) done(got, null) else done(null, why ?: "the answer couldn't be read")
        }
    }

    /** How each of these uploads (project ids) stands on the server: whether it's meshed, and as what. */
    fun status(projects: Collection<String>, done: (Map<String, UploadStatus>?, String?) -> Unit) = call("/api/capture/status?ids=" + projects.joinToString(",")) { bytes, why ->
        val got = bytes?.let { runCatching { json.decodeFromString(MapSerializer(String.serializer(), UploadStatus.serializer()), it.decodeToString()) }.getOrNull() }
        if (got != null) done(got, null) else done(null, why ?: "the answer couldn't be read")
    }
}
