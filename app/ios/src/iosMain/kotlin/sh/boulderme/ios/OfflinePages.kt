package sh.boulderme.ios

import kotlinx.cinterop.BetaInteropApi
import kotlinx.cinterop.ExperimentalForeignApi
import kotlinx.cinterop.ObjCSignatureOverride
import kotlinx.serialization.builtins.MapSerializer
import kotlinx.serialization.builtins.serializer
import platform.Foundation.NSHTTPURLResponse
import platform.Foundation.NSMutableURLRequest
import platform.Foundation.NSURL
import platform.Foundation.NSURLSession
import platform.Foundation.HTTPMethod
import platform.Foundation.timeIntervalSince1970
import platform.Foundation.dataTaskWithRequest
import platform.Foundation.setHTTPMethod
import platform.Foundation.setValue
import platform.UIKit.UIAlertAction
import platform.UIKit.UIAlertActionStyleCancel
import platform.UIKit.UIAlertActionStyleDefault
import platform.UIKit.UIAlertController
import platform.UIKit.UIAlertControllerStyleAlert
import platform.UIKit.UIViewController
import platform.WebKit.WKURLSchemeHandlerProtocol
import platform.WebKit.WKURLSchemeTaskProtocol
import platform.WebKit.WKWebView
import platform.darwin.NSObject
import platform.darwin.dispatch_async
import platform.darwin.dispatch_get_main_queue
import sh.boulderme.core.KeptFile
import platform.Foundation.NSTimer
import sh.boulderme.core.Offline
import sh.boulderme.core.OfflineBundle
import sh.boulderme.core.OfflineFetch
import sh.boulderme.core.OfflineIndex
import sh.boulderme.core.OfflineSizes

/**
 * Meshes kept on the phone for the site's viewer, to show with no signal (core's Offline): each in viewer/<mesh>/ with
 * its index.json, and the libraries every mesh's page uses once, in viewer/lib/.
 */
class IosOffline(private val store: IosStore) {
    private val root = Files.docs + "/viewer"
    private val libDir = "$root/lib"
    private val kept = MapSerializer(String.serializer(), KeptFile.serializer())
    private val json = IosStore.json

    private fun dir(mesh: String) = "$root/$mesh"
    private fun name(key: String): String {     // a file's name from its address: two hashes of it, enough apart not to meet
        var a = 1469598103934665603L; var b = 7L
        for (c in key) { a = (a xor c.code.toLong()) * 1099511628211L; b = b * 31 + c.code }
        return a.toULong().toString(16) + b.toULong().toString(16)
    }

    fun index(mesh: String): OfflineIndex? = Files.readText("${dir(mesh)}/index.json")?.let { runCatching { json.decodeFromString(OfflineIndex.serializer(), it) }.getOrNull() }
    private fun libs(): Map<String, KeptFile> = Files.readText("$libDir/index.json")?.let { runCatching { json.decodeFromString(kept, it) }.getOrNull() } ?: emptyMap()
    fun lib(key: String): Pair<KeptFile, ByteArray?>? = libs()[key]?.let { k -> Files.readBytes("$libDir/${k.file}")?.let { k to it } }

    private fun keep(mesh: String, key: String, lib: Boolean, status: Int, type: String?, body: ByteArray): KeptFile {
        val k = KeptFile(name(key), status, type)
        if (lib) {
            Files.mkdirs(libDir)
            Files.write("$libDir/${k.file}", body)
            Files.write("$libDir/index.json", json.encodeToString(kept, libs() + (key to k)))
        } else { Files.mkdirs(dir(mesh)); Files.write("${dir(mesh)}/${k.file}", body) }
        return k
    }

    fun remove(mesh: String) = Files.delete(dir(mesh))

    /** A kept file's body, and how the server answered for it. */
    fun file(index: OfflineIndex, key: String): Pair<KeptFile, ByteArray>? {
        val k = Offline.find(index.files, key) ?: return null
        val lib = libs()[key]?.takeIf { it.file == k.file } != null
        return Files.readBytes(if (lib) "$libDir/${k.file}" else "${dir(index.mesh)}/${k.file}")?.let { k to it }
    }

    /**
     * Keeps a mesh's files for the viewer, with its textures or without; what was kept of it before goes first. Answers
     * on the main thread.
     *
     * Args:
     *     progress: how much has come, 0 to 1, as it grows (OfflineFetch's).
     *     waiting: told when a file waits for Wi-Fi (true; meshes are got only over Wi-Fi), and when it stops waiting.
     */
    fun fetch(api: IosApi, mesh: String, textures: Boolean, bundle: OfflineBundle, progress: (Double) -> Unit = {}, waiting: (Boolean) -> Unit = {},
              done: (OfflineIndex?, String?) -> Unit) {
        remove(mesh)
        OfflineFetch(bundle, textures, store.server,
            fetch = { url, got, answer ->
                var waited = false
                Net.await({ store.downloadWifiOnly }, { waited = true; waiting(true) }) {
                    if (waited) waiting(false)
                    raw(url, got = got) { status, type, body, why -> answer(status, type, body, why) }
                }
            },
            lib = ::lib, keep = { key, lib, status, type, body -> keep(mesh, key, lib, status, type, body) },
            now = platform.Foundation.NSDate().timeIntervalSince1970,
            done = { index, why ->
                if (index != null) Files.write("${dir(mesh)}/index.json", json.encodeToString(OfflineIndex.serializer(), index)) else remove(mesh)
                done(index, why)
            }, progress = progress).start()
    }

    /**
     * Fetches any address, the server's with this phone's token: its status, type and body whatever the status; on the
     * main thread. `got` is told how many bytes of it have come, a few times a second while they do.
     */
    fun raw(url: String, timeout: Double = 120.0, got: ((Long) -> Unit)? = null, done: (Int, String?, ByteArray?, String?) -> Unit) {
        val u = NSURL.URLWithString(url) ?: return done(0, null, null, "not an address: $url")
        val req = NSMutableURLRequest.requestWithURL(u)
        req.setHTTPMethod("GET")
        req.setTimeoutInterval(timeout)
        req.setValue("BoulderMeshApp/${IosStore.VERSION} (iOS)", forHTTPHeaderField = "User-Agent")
        if (url.startsWith(store.server.trimEnd('/') + "/") && store.token.isNotEmpty()) req.setValue("Bearer ${store.token}", forHTTPHeaderField = "Authorization")
        var watch: NSTimer? = null
        val task = NSURLSession.sharedSession.dataTaskWithRequest(req) { data, response, error ->
            val r = response as? NSHTTPURLResponse
            val status = r?.statusCode?.toInt() ?: 0
            val type = r?.allHeaderFields?.entries?.firstOrNull { (it.key as? String)?.lowercase() == "content-type" }?.value as? String
            val bytes = data?.toBytes()
            dispatch_async(dispatch_get_main_queue()) {
                watch?.invalidate()
                if (error != null || r == null) done(0, null, null, error?.localizedDescription ?: "no answer") else done(status, type, bytes ?: ByteArray(0), null)
            }
        }
        if (got != null) watch = NSTimer.scheduledTimerWithTimeInterval(0.25, repeats = true) { _ -> got(task.countOfBytesReceived) }
        task.resume()
    }

    /** Whether the server answers within a few seconds: if not, the viewer shows what's kept. */
    fun reachable(done: (Boolean) -> Unit) = raw(store.server.trimEnd('/') + "/api/status", 4.0) { status, _, _, _ -> done(status in 200..499) }
}

/**
 * Answers a kept page's requests (bmsh://offline/…) from what's kept of its mesh: its markup with its CDN files made its
 * own (Offline.ownCdns()), so that those come here too; a 503 for anything not kept.
 */
@OptIn(ExperimentalForeignApi::class, BetaInteropApi::class)
class OfflinePages(private val offline: IosOffline, private val index: OfflineIndex, private val server: String) : NSObject(), WKURLSchemeHandlerProtocol {
    @ObjCSignatureOverride
    override fun webView(webView: WKWebView, startURLSchemeTask: WKURLSchemeTaskProtocol) {
        val url = startURLSchemeTask.request.URL ?: return
        val key = Offline.key(url.absoluteString ?: "", server)
        val got = if ((startURLSchemeTask.request.HTTPMethod ?: "GET") == "GET") offline.file(index, key) else null
        val type = got?.first?.type ?: "application/json"
        val body = when {
            got == null -> "{\"error\":\"no signal: this isn't kept on the phone\"}".encodeToByteArray()
            type.startsWith("text/html") -> Offline.ownCdns(got.second.decodeToString()).encodeToByteArray()
            else -> got.second
        }
        val headers = mapOf<Any?, Any?>("Content-Type" to type, "Content-Length" to body.size.toString(), "Access-Control-Allow-Origin" to "*", "Cache-Control" to "no-store")
        val response = NSHTTPURLResponse(uRL = url, statusCode = (got?.first?.status ?: 503).toLong(), HTTPVersion = "HTTP/1.1", headerFields = headers)
        startURLSchemeTask.didReceiveResponse(response)
        startURLSchemeTask.didReceiveData(body.toData())
        startURLSchemeTask.didFinish()
    }

    @ObjCSignatureOverride
    override fun webView(webView: WKWebView, stopURLSchemeTask: WKURLSchemeTaskProtocol) {}
}

/** Asks how to keep meshes for the viewer: with their textures or without (shown as a wireframe), with what each takes. */
fun UIViewController.keepChoice(what: String, sizes: List<OfflineSizes?>, choose: (textures: Boolean) -> Unit) {
    fun said(pick: (OfflineSizes) -> Long): String {
        val known = sizes.filterNotNull()
        if (known.isEmpty()) return "size not known"
        return Offline.size(known.sumOf(pick)) + if (known.size < sizes.size) ", and ${sizes.size - known.size} not known" else ""
    }
    val alert = UIAlertController.alertControllerWithTitle("Keep $what", "For capturing, and for the viewer with no signal.", UIAlertControllerStyleAlert)
    alert.addAction(UIAlertAction.actionWithTitle("Without textures, as a wireframe · ${said { it.plain }}", UIAlertActionStyleDefault) { _ -> choose(false) })
    alert.addAction(UIAlertAction.actionWithTitle("With textures · ${said { it.textured }}", UIAlertActionStyleDefault) { _ -> choose(true) })
    alert.addAction(UIAlertAction.actionWithTitle("Cancel", UIAlertActionStyleCancel, null))
    presentViewController(alert, animated = true, completion = null)
}
