package sh.boulderme.core

import com.sun.net.httpserver.HttpServer
import java.io.File
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.URL
import java.net.URLDecoder
import kotlin.test.Test
import kotlin.test.assertNotNull

/**
 * A check by hand of keeping a mesh for the viewer against a running server, as the apps do (OfflineFetch), and of
 * answering the page from what's kept (Offline.key, find), for a browser to be pointed at. Skipped unless
 * BM_OFFLINE_CHECK is "<server>|<mesh>|<folder>|<textures 0/1>|<port to answer on>|<seconds to answer for>": the
 * browser asks http://127.0.0.1:<port>/answer?u=<address> for each of the page's requests; at http://ios.test/ as iOS shows it.
 */
class OfflineCheck {
    @Test fun keepAndAnswer() {
        val spec = System.getenv("BM_OFFLINE_CHECK") ?: return
        val (server, mesh, folder, textures, port, seconds) = spec.split('|')
        val dir = File(folder).apply { deleteRecursively(); mkdirs() }
        val libs = HashMap<String, Pair<KeptFile, ByteArray>>()
        val files = HashMap<String, ByteArray>()
        fun raw(url: String): Triple<Int, String?, ByteArray> {
            val c = URL(url).openConnection() as HttpURLConnection
            val code = c.responseCode
            return Triple(code, c.contentType, ((if (code in 200..299) c.inputStream else c.errorStream)?.use { it.readBytes() }) ?: ByteArray(0))
        }
        val bundle = Plan.json.decodeFromString(OfflineBundle.serializer(), raw("$server/api/capture/offline/$mesh").third.decodeToString())
        var index: OfflineIndex? = null; var why: String? = null
        OfflineFetch(bundle, textures == "1", server,
            fetch = { url, _, done -> runCatching { raw(url) }.fold({ (s, t, b) -> done(s, t, b, null) }, { done(0, null, null, it.message) }) },
            lib = { libs[it] },
            keep = { key, lib, status, type, body -> KeptFile("f${files.size}", status, type).also { files[it.file] = body; File(dir, it.file).writeBytes(body); if (lib) libs[key] = it to body } },
            now = 0.0, done = { i, w -> index = i; why = w }).start()
        val kept = assertNotNull(index, why)
        File(dir, "index.json").writeText(Plan.json.encodeToString(OfflineIndex.serializer(), kept))
        println("kept ${kept.files.size} files, ${kept.bytes} bytes; libraries: ${libs.keys.joinToString()}")
        val http = HttpServer.create(InetSocketAddress("127.0.0.1", port.toInt()), 0)
        http.createContext("/answer") { ex ->
            // http://ios.test/ stands for iOS's own scheme: the page's CDN addresses made its own, as OfflinePages does
            val u = URLDecoder.decode(ex.requestURI.rawQuery.removePrefix("u="), "UTF-8").replace("http://ios.test/", "${Offline.SCHEME}://${Offline.HOST}/")
            val k = Offline.find(kept.files, Offline.key(u, server))
            val raw = k?.let { files[it.file] } ?: "{\"error\":\"not kept\"}".toByteArray()
            val body = if (u.startsWith(Offline.SCHEME) && k?.type?.startsWith("text/html") == true) Offline.ownCdns(raw.decodeToString()).toByteArray() else raw
            ex.responseHeaders.add("Content-Type", k?.type ?: "application/json")
            ex.sendResponseHeaders(k?.status ?: 503, body.size.toLong())
            ex.responseBody.use { it.write(body) }
        }
        http.start()
        Thread.sleep(seconds.toLong() * 1000)
        http.stop(0)
    }

    private operator fun <T> List<T>.component6(): T = this[5]
}
