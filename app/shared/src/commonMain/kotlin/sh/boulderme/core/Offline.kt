package sh.boulderme.core

import kotlinx.serialization.Serializable
import kotlin.math.roundToLong

/** How much of a phone a mesh kept for the viewer takes, in bytes: without its textures, and with (the server's guess). */
@Serializable data class OfflineSizes(val plain: Long, val textured: Long)

/** A file the site's viewer fetches to show a mesh: its address as the page asks for it, and its size if the server knows it. */
@Serializable data class OfflineFile(val url: String, val bytes: Long? = null, val texture: Boolean = false, val lib: Boolean = false)

/** Every file the viewer fetches to show a mesh (the server's /api/capture/offline/<mesh>). */
@Serializable data class OfflineBundle(val mesh: String, val v: String = "", val page: String, val files: List<OfflineFile>, val bytes: OfflineSizes? = null)

/** A file kept: the file it's in (in its copy's folder, or for a library the libraries' own), and how the server answered. */
@Serializable data class KeptFile(val file: String, val status: Int = 200, val type: String? = null)

/** A mesh kept on the phone for the viewer, by what each file's address is (Offline.key()). */
@Serializable data class OfflineIndex(val mesh: String, val v: String = "", val textures: Boolean, val made: Double = 0.0,
                                      val files: Map<String, KeptFile> = emptyMap(), val bytes: Long = 0)

/**
 * The site's viewer, kept on the phone to show a mesh with no signal: the page, its scripts (the CDN's too), what of the
 * API it waits for, and the mesh, each kept as it was fetched and given back to the page by its address. Both apps go
 * by this; each keeps the files and answers the page's requests its own way.
 */
object Offline {
    /** On iOS, the scheme a kept page is shown under (a web view there can't be given its answers for https). */
    const val SCHEME = "bmsh"
    const val HOST = "offline"
    private val CDNS = listOf("cdn.jsdelivr.net", "cdnjs.cloudflare.com")

    /**
     * What a file kept goes by: for one of the server's (an address on it, or a path), its path, decoded, and its query's
     * parameters in order; for any other, its whole address. The page at /m/<mesh> goes by its path alone.
     */
    fun key(url: String, server: String): String {
        val base = server.trimEnd('/')
        var u = url.substringBefore('#')
        when {
            u.startsWith("$base/") -> u = u.removePrefix(base)
            u.startsWith("$SCHEME://$HOST/") -> u = u.removePrefix("$SCHEME://$HOST")
            u.startsWith("/") -> {}
            else -> return u
        }
        val cdn = Regex("^/cdn/([^/]+)(/.*)$").find(u)
        if (cdn != null && cdn.groupValues[1] in CDNS) return "https://" + cdn.groupValues[1] + cdn.groupValues[2]
        val path = decode(u.substringBefore('?'))
        if (path.startsWith("/m/")) return path
        val query = u.substringAfter('?', "")
        return if (query.isEmpty()) path else path + "?" + query.split('&').filter { it.isNotEmpty() }.map { decode(it) }.sorted().joinToString("&")
    }

    /** The file kept for this address: the same address's; for the API, else the same path's with another query (the climbs list's length, say). */
    fun find(files: Map<String, KeptFile>, key: String): KeptFile? =
        files[key] ?: if (key.startsWith("/api/")) key.substringBefore('?').let { p -> files[p] ?: files.entries.firstOrNull { it.key.substringBefore('?') == p }?.value } else null

    /**
     * The modules a script imports beside it (by a relative address): what else to keep of a library. Bare names (an
     * import map's) aren't, nor are other sites' addresses (those found are in its comments' examples).
     */
    fun imports(url: String, js: String): List<String> {
        val found = Regex("""(?:\bfrom\s*|\bimport\s*\(?\s*)['"]([^'"\s]+)['"]""").findAll(js).map { it.groupValues[1] }
            .filter { it.startsWith("./") || it.startsWith("../") }
        return found.map { resolve(url, it) }.distinct().toList()
    }

    /** An address relative to another's. */
    fun resolve(from: String, rel: String): String {
        if (rel.startsWith("https://") || rel.startsWith("http://")) return rel
        val origin = Regex("^(\\w+://[^/]+)").find(from)?.value ?: ""
        val parts = from.removePrefix(origin).substringBefore('?').split('/').dropLast(1).toMutableList()
        for (p in rel.split('/')) when (p) { "." -> {}; ".." -> if (parts.size > 1) parts.removeAt(parts.size - 1); else -> parts.add(p) }
        return origin + parts.joinToString("/")
    }

    /** The page's markup, its CDN addresses made its own (/cdn/<host>/…): for iOS, where only the kept page's own scheme can be answered. */
    fun ownCdns(html: String): String = CDNS.fold(html) { h, host -> h.replace("https://$host/", "/cdn/$host/") }

    /** The files to keep: all, or all but the textures. */
    fun wanted(bundle: OfflineBundle, textures: Boolean): List<OfflineFile> = bundle.files.filter { textures || !it.texture }

    /** A size, as the dialog says it: "850 KB", "12 MB", "1.4 GB". */
    fun size(bytes: Long): String = when {
        bytes < 1_000_000 -> "${maxOf(1, (bytes / 1000.0).roundToLong())} KB"
        bytes < 10_000_000 -> "${(bytes / 100_000.0).roundToLong() / 10.0} MB"
        bytes < 1_000_000_000 -> "${(bytes / 1_000_000.0).roundToLong()} MB"
        else -> "${(bytes / 100_000_000.0).roundToLong() / 10.0} GB"
    }

    private fun decode(s: String): String {
        if ('%' !in s) return s
        fun hex(c: Char) = c.isDigit() || c.lowercaseChar() in 'a'..'f'
        val out = ArrayList<Byte>()
        var i = 0
        while (i < s.length) {
            val c = s[i]
            if (c == '%' && i + 2 < s.length && hex(s[i + 1]) && hex(s[i + 2])) {
                out.add(s.substring(i + 1, i + 3).toInt(16).toByte()); i += 3
            } else { c.toString().encodeToByteArray().forEach { out.add(it) }; i++ }
        }
        return out.toByteArray().decodeToString()
    }
}

/**
 * Keeps a mesh for the viewer: each file of its bundle fetched in turn, and the modules each library imports. A library
 * kept already (for another mesh) isn't fetched again. The page's own files, the libraries and the mesh have to come;
 * what of the API the page asks for is kept however the server answered (a mesh with no cleanup answers 404).
 *
 * Args:
 *     fetch: fetches an address (the server's paths given whole), answering its status, type and body, or why it couldn't;
 *         meanwhile telling `got` how many bytes of it have come so far, if it can.
 *     lib: a library kept already, by address, with its body. keep: keeps a file, answering what it's kept as.
 *     done: the copy's index once all is kept, or why not.
 *     progress: how much of the copy has come, 0 to 1, said as it grows (by half a percent or more): by the bytes of the
 *         files the server gave a size for (the mesh and its textures, nearly all of it), else by the files.
 */
class OfflineFetch(
    private val bundle: OfflineBundle, private val textures: Boolean, private val server: String,
    private val fetch: (url: String, got: (Long) -> Unit, done: (status: Int, type: String?, body: ByteArray?, why: String?) -> Unit) -> Unit,
    private val lib: (key: String) -> Pair<KeptFile, ByteArray?>?,
    private val keep: (key: String, lib: Boolean, status: Int, type: String?, body: ByteArray) -> KeptFile,
    private val now: Double,
    private val done: (OfflineIndex?, String?) -> Unit,
    private val progress: (Double) -> Unit = {},
) {
    private val todo = ArrayDeque(Offline.wanted(bundle, textures))
    private val total = todo.sumOf { it.bytes ?: 0L }
    private var files = todo.size
    private var finished = 0
    private var sized = 0L      // bytes of the files finished that the server gave a size for
    private var said = -1
    private val seen = HashSet(todo.map { it.url })
    private val found = HashSet<String>()      // modules found imported, not named by the bundle: kept if they come
    private val kept = LinkedHashMap<String, KeptFile>()
    private var bytes = 0L

    fun start() { tell(0); next() }

    private fun next() {
        while (true) {
            val f = todo.removeFirstOrNull() ?: return done(OfflineIndex(bundle.mesh, bundle.v, textures, now, kept, bytes), null)
            val key = Offline.key(f.url, server)
            val had = if (f.lib) lib(key) else null
            if (had != null) {
                kept[key] = had.first
                had.second?.let { follow(f.url, had.first.type, it) }
                finish(f)
                continue
            }
            val address = if (f.url.startsWith("/")) server.trimEnd('/') + f.url else f.url
            fetch(address, { n -> tell(minOf(n, f.bytes ?: 0L)) }) { status, type, body, why ->
                val api = f.url.startsWith("/api/")
                when {
                    f.url in found && (body == null || status !in 200..299) -> { finish(f); next() }
                    body == null -> done(null, "${f.url}: ${why ?: "no answer"}")
                    status !in 200..299 && !api -> done(null, "${f.url}: the server answered $status")
                    else -> {
                        kept[key] = keep(key, f.lib, status, type, body)
                        bytes += body.size
                        if (f.lib) follow(f.url, type, body)
                        finish(f)
                        next()
                    }
                }
            }
            return
        }
    }

    private fun follow(url: String, type: String?, body: ByteArray) {
        if (type?.contains("javascript") != true && !url.endsWith(".js")) return
        for (dep in Offline.imports(url, body.decodeToString())) if (seen.add(dep)) { found.add(dep); todo.addLast(OfflineFile(dep, lib = true)); files++ }
    }

    private fun finish(f: OfflineFile) { finished++; sized += f.bytes ?: 0L; tell(0) }

    /** Says how far the copy has got, `current` bytes into the file being fetched. */
    private fun tell(current: Long) {
        val part = if (total > 0) (sized + current).toDouble() / total else finished.toDouble() / maxOf(files, 1)
        val step = (part.coerceIn(0.0, 1.0) * 200).toInt()
        if (step != said) { said = step; progress(step / 200.0) }
    }
}
