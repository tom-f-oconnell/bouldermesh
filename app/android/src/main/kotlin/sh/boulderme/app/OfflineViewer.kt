package sh.boulderme.app

import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.dp
import kotlinx.serialization.builtins.MapSerializer
import kotlinx.serialization.builtins.serializer
import sh.boulderme.core.KeptFile
import sh.boulderme.core.Offline
import sh.boulderme.core.OfflineBundle
import sh.boulderme.core.OfflineFetch
import sh.boulderme.core.OfflineIndex
import sh.boulderme.core.OfflineSizes
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * Meshes kept on the phone for the site's viewer, to show with no signal (core's Offline): each in viewer/<mesh>/ with
 * its index.json, and the libraries every mesh's page uses once, in viewer/lib/.
 */
class OfflineStore(private val root: File) {
    private val libDir = File(root, "lib")
    private val libIndex = File(libDir, "index.json")
    private val kept = MapSerializer(String.serializer(), KeptFile.serializer())

    private fun dir(mesh: String) = File(root, mesh)
    private fun name(key: String) = MessageDigest.getInstance("SHA-1").digest(key.toByteArray()).joinToString("") { "%02x".format(it) }.take(24)

    fun index(mesh: String): OfflineIndex? = runCatching { Store.json.decodeFromString(OfflineIndex.serializer(), File(dir(mesh), "index.json").readText()) }.getOrNull()
    private fun libs(): Map<String, KeptFile> = runCatching { Store.json.decodeFromString(kept, libIndex.readText()) }.getOrDefault(emptyMap())

    /** A library kept already, with its body. */
    fun lib(key: String): Pair<KeptFile, ByteArray?>? = libs()[key]?.let { k -> File(libDir, k.file).takeIf { it.isFile }?.let { k to it.readBytes() } }

    @Synchronized
    fun keep(mesh: String, key: String, lib: Boolean, status: Int, type: String?, body: ByteArray): KeptFile {
        val k = KeptFile(name(key), status, type)
        if (lib) {
            libDir.mkdirs()
            File(libDir, k.file).writeBytes(body)
            libIndex.writeText(Store.json.encodeToString(kept, libs() + (key to k)))
        } else File(dir(mesh).apply { mkdirs() }, k.file).writeBytes(body)
        return k
    }

    /** Starts a mesh's copy afresh: what was kept of it before goes. */
    fun clear(mesh: String) { dir(mesh).deleteRecursively() }
    fun save(index: OfflineIndex) = File(dir(index.mesh).apply { mkdirs() }, "index.json").writeText(Store.json.encodeToString(OfflineIndex.serializer(), index))
    fun remove(mesh: String) = clear(mesh)

    /** The page's answer to a request, from what's kept; a 503 for anything that isn't, so that nothing waits on the network. */
    fun answer(index: OfflineIndex, server: String, request: WebResourceRequest): WebResourceResponse {
        val key = Offline.key(request.url.toString(), server)
        val mine = if (request.method == "GET") Offline.find(index.files, key) else null
        val lib = mine?.let { k -> libs()[key]?.takeIf { it.file == k.file } }
        val file = mine?.let { File(if (lib != null) libDir else dir(index.mesh), it.file) }?.takeIf { it.isFile }
        val headers = mapOf("Access-Control-Allow-Origin" to "*", "Cache-Control" to "no-store")
        if (mine == null || file == null) return WebResourceResponse("application/json", "utf-8", 503, "Offline", headers,
            ByteArrayInputStream("{\"error\":\"no signal: this isn't kept on the phone\"}".toByteArray()))
        val type = mine.type ?: "application/octet-stream"
        val charset = Regex("charset=([\\w-]+)").find(type)?.groupValues?.get(1)
        return WebResourceResponse(type.substringBefore(';').trim(), charset, mine.status, if (mine.status in 200..299) "OK" else "Kept", headers, file.inputStream())
    }

    /**
     * Keeps a mesh's files for the viewer (blocking: call off the main thread), with its textures or without; what was
     * kept of it before goes first. Returns its index. Raises IOException, in words, if a file the page needs didn't come.
     *
     * Args:
     *     progress: how much has come, 0 to 1, as it grows (OfflineFetch's).
     *     wait: blocks before each file until it may be fetched (Store.awaitDownloads()).
     */
    fun fetch(api: Api, store: Store, mesh: String, textures: Boolean, bundle: OfflineBundle = api.offline(mesh),
              progress: (Double) -> Unit = {}, wait: () -> Unit = {}): OfflineIndex {
        clear(mesh)
        var got: OfflineIndex? = null; var why: String? = null
        OfflineFetch(bundle, textures, store.server,
            fetch = { url, bytes, done ->
                wait()
                runCatching { api.raw(url, got = bytes) }.fold({ (status, type, body) -> done(status, type, body, null) }, { done(0, null, null, it.message) })
            },
            lib = ::lib, keep = { key, lib, status, type, body -> keep(mesh, key, lib, status, type, body) },
            now = System.currentTimeMillis() / 1000.0, done = { i, w -> got = i; why = w }, progress = progress).start()
        val index = got ?: run { clear(mesh); throw IOException(why ?: "it couldn't be kept") }
        save(index)
        return index
    }
}

/**
 * Fetches any address, the server's with this phone's token: its status, type and body, whatever the status. `got` is
 * told how many bytes of the body have come, as they do.
 */
fun Api.raw(url: String, server: String = store.server, timeoutMs: Int = 120_000, got: ((Long) -> Unit)? = null): Triple<Int, String?, ByteArray> {
    val c = URL(url).openConnection() as HttpURLConnection
    c.connectTimeout = minOf(15_000, timeoutMs); c.readTimeout = timeoutMs
    c.setRequestProperty("User-Agent", "BoulderMeshApp/${BuildInfo.VERSION} (Android)")
    if (url.startsWith(server.trimEnd('/') + "/") && store.token.isNotEmpty()) c.setRequestProperty("Authorization", "Bearer ${store.token}")
    try {
        val code = c.responseCode
        val body = (if (code in 200..299) c.inputStream else c.errorStream)?.use { input ->
            if (got == null) input.readBytes() else {
                val out = java.io.ByteArrayOutputStream(maxOf(c.contentLength, 32 * 1024))
                val chunk = ByteArray(64 * 1024)
                while (true) { val n = input.read(chunk); if (n < 0) break; out.write(chunk, 0, n); got(out.size().toLong()) }
                out.toByteArray()
            }
        } ?: ByteArray(0)
        return Triple(code, c.contentType, body)
    } finally { c.disconnect() }
}

/** Whether the server answers within a few seconds: if not, the viewer shows what's kept. */
fun Api.reachable(): Boolean = runCatching { raw(store.server.trimEnd('/') + "/api/status", timeoutMs = 4000).first in 200..499 }.getOrDefault(false)

/**
 * Asks how to keep meshes for the viewer: with their textures or without (shown as a wireframe), with what each takes.
 * sizes: the meshes' (null where the server didn't say).
 */
@Composable
fun KeepChoice(what: String, sizes: List<OfflineSizes?>, dismiss: () -> Unit, choose: (textures: Boolean) -> Unit) {
    fun said(pick: (OfflineSizes) -> Long): String {
        val known = sizes.filterNotNull()
        if (known.isEmpty()) return "size not known"
        return Offline.size(known.sumOf(pick)) + if (known.size < sizes.size) ", and ${sizes.size - known.size} not known" else ""
    }
    AlertDialog(onDismissRequest = dismiss, title = { Text("Keep $what") },
        text = { Text("For capturing, and for the viewer with no signal.") },
        confirmButton = {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                OutlinedButton(onClick = { choose(false) }) { Text("Without textures, as a wireframe · ${said { it.plain }}") }
                OutlinedButton(onClick = { choose(true) }) { Text("With textures · ${said { it.textured }}") }
                TextButton(onClick = dismiss) { Text("Cancel") }
            }
        })
}
