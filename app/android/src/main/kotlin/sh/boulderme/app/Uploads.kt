package sh.boulderme.app

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.util.LruCache
import android.webkit.JavascriptInterface
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonPrimitive
import sh.boulderme.core.CaptureContents
import sh.boulderme.core.SentCapture
import sh.boulderme.core.UploadArea
import sh.boulderme.core.UploadStatus
import java.io.File

/** Waiting: yellow, as nothing else is. */
val WAIT = Color(0xFFE8C547)

fun toneColor(tone: String): Color = when (tone) { "good" -> GOOD; "bad" -> BAD; "warn" -> WARN; "wait" -> WAIT; else -> Color.Unspecified }

/**
 * What the server says of this phone's uploads (Store.statuses, kept for when there's no signal), and of the account's
 * captures that aren't on the phone (Store.sent), asked again each time the Home screen comes up and when its Check is
 * pressed: never on its own, and with no connection only when pressed (to say there's none).
 */
object Statuses {
    val state = MutableStateFlow<Map<String, UploadStatus>>(emptyMap())
    val sent = MutableStateFlow<List<SentCapture>>(emptyList())
    val error = MutableStateFlow<String?>(null)
    val busy = MutableStateFlow(false)

    suspend fun refresh(store: Store, api: Api, pressed: Boolean = false) {
        if (state.value.isEmpty()) state.value = store.statuses
        if (sent.value.isEmpty()) sent.value = store.sent
        if (busy.value) return
        if (!store.online()) {
            if (pressed) error.value = "No connection: this is what the server said last"
            return
        }
        val local = store.captureIds().mapNotNull { store.upload(it) }
        val asked = local.filter { it.meshAsked || it.done }.mapNotNull { it.project }
        if (store.token.isEmpty() && sent.value.isNotEmpty()) { store.sent = emptyList(); sent.value = emptyList() }
        if (asked.isEmpty() && store.token.isEmpty()) return
        busy.value = true
        try {
            withContext(Dispatchers.IO) {
                val others = api.accountProjects()?.let { SentCapture.notOnPhone(it, local.map { u -> u.project }) }.orEmpty()
                val ids = asked + others.map { it.id }
                val got = if (ids.isEmpty()) emptyMap() else api.status(ids)
                store.statuses = got; state.value = got
                store.sent = others; sent.value = others
            }
            error.value = null
        } catch (e: Exception) {
            error.value = "Couldn't ask the server: ${e.message}"
        } finally {
            busy.value = false
        }
    }
}

// ------------------------------------------------------------------------------------------------------------ uploads
private val thumbs = object : LruCache<String, Bitmap>(24 * 1024 * 1024) { override fun sizeOf(key: String, value: Bitmap) = value.byteCount }

/** A photo, read at about `px` across (a power of two smaller than it is). */
private fun decode(file: File, px: Int): Bitmap? = runCatching {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeFile(file.path, bounds)
    var sample = 1
    while (maxOf(bounds.outWidth, bounds.outHeight) / (sample * 2) >= px) sample *= 2
    BitmapFactory.decodeFile(file.path, BitmapFactory.Options().apply { inSampleSize = sample })
}.getOrNull()

@Composable
private fun Thumb(file: File, sent: Boolean, modifier: Modifier, open: () -> Unit) {
    val bitmap by produceState(thumbs.get(file.path), file) {
        if (value == null) value = withContext(Dispatchers.IO) { decode(file, 240)?.also { thumbs.put(file.path, it) } }
    }
    Box(modifier.aspectRatio(1f).background(MaterialTheme.colorScheme.surfaceVariant).clickable(onClick = open)) {
        bitmap?.let { Image(it.asImageBitmap(), null, Modifier.fillMaxSize(), contentScale = ContentScale.Crop) }
        if (sent) Text("✓", color = GOOD, fontWeight = FontWeight.Bold, modifier = Modifier.align(Alignment.BottomEnd).background(Color(0x99000000)).padding(horizontal = 4.dp))
    }
}

/** How a capture stands, in words and colour: on the phone, being sent, or what the server says of it. */
fun uploadSaid(u: Store.Upload, sending: String?, server: UploadStatus?): Pair<String, Color> = when {
    sending != null -> sending to if (sending == Uploader.WAITING) WAIT else Color.Unspecified
    server != null && (u.meshAsked || u.done) -> server.said().let { (text, tone) -> text to toneColor(tone) }
    u.done -> "Sent: the server is meshing it, or has" to GOOD
    u.note.isNotEmpty() -> "Stopped: ${u.note}" to BAD
    u.sent.isNotEmpty() -> "${u.sent.size} of ${u.photos} photos sent" to Color.Unspecified
    else -> "${u.photos} photos, not sent yet" to Color.Unspecified
}

/** One capture: what it holds, grouped by what each part was for, with what's been sent ticked; each photo opens. */
@Composable
fun UploadScreen(a: MainActivity, id: String, go: (Screen) -> Unit) {
    val u = remember { a.store.upload(id) }
    val (groups, _) = remember { a.store.contents(id) }
    val dir = a.store.captureDir(id)
    val statuses by Statuses.state.collectAsState()
    val server = statuses[u?.project ?: ""]
    LaunchedEffect(Unit) { Statuses.refresh(a.store, a.api) }
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = { go(Screen.Home) }) { Text("‹ Back") }
            Text(u?.name ?: "Gone", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(vertical = 12.dp))
        }
        if (u == null) { Text("This capture is gone."); return }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.weight(1f)) {
            item {
                val (said, color) = uploadSaid(u, null, server)
                Text(said, color = color)
                ReopenButton(a, id, u, go)
                server?.area?.let { AreaQuestion(a, u.project ?: "", it) }
                server?.meshed?.let { mesh ->
                    TextButton(onClick = { go(Screen.Viewer(mesh, u.name, Screen.Upload(id))) }) { Text("View its mesh") }
                    KeepMesh(a, mesh, u.name)
                }
            }
            for (g in groups) {
                item(key = g.key) {
                    Column(Modifier.padding(top = 12.dp)) {
                        Text(g.title, fontWeight = FontWeight.SemiBold)
                        val sentOf = if (g.scenery) u.scenerySent else u.sent
                        val count = if (g.photos.isEmpty()) "" else {
                            val n = g.photos.count { it in sentOf }
                            " · ${g.photos.size} ${if (g.scenery) "frame" else "photo"}${if (g.photos.size == 1) "" else "s"}, " +
                                if (n == g.photos.size) "all sent" else if (n == 0) "not sent yet" else "$n sent"
                        }
                        Text(g.about + count, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
                items(g.photos.chunked(4), key = { g.key + it.first() }) { row ->
                    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        for (name in row) {
                            val file = File(dir, (if (g.scenery) "scenery/" else "photos/") + name)
                            Thumb(file, name in (if (g.scenery) u.scenerySent else u.sent), Modifier.weight(1f)) { go(Screen.Photo(id, name, g.scenery, g.title)) }
                        }
                        repeat(4 - row.size) { Spacer(Modifier.weight(1f)) }
                    }
                }
                items(g.files, key = { g.key + it.first }) { (name, about) ->
                    val sent = when (name) { "capture.json" -> u.recordSent; "points.bin" -> u.pointsSent; "capture-log.jsonl" -> u.logSent; else -> false }
                    Column {
                        Text(name + if (sent) "  ✓ sent" else "  · not sent yet", style = MaterialTheme.typography.bodyMedium)
                        Text(about, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
            item { Spacer(Modifier.padding(12.dp)) }
        }
    }
}

/** The area the server put an upload in by its GPS; and, where several are close by, which it's in, asked. */
@Composable
private fun AreaQuestion(a: MainActivity, project: String, area: UploadArea) {
    val scope = rememberCoroutineScope()
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    area.said()?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
    if (!area.ask) return
    Text("Which area is it in? Where its photos were taken is close to ${area.choices.size} of them:", color = WAIT)
    area.choices.forEachIndexed { k, c ->
        OutlinedButton(enabled = !busy, onClick = {
            busy = true
            scope.launch {
                error = runCatching { withContext(Dispatchers.IO) { a.api.chooseArea(project, k) } }.exceptionOrNull()?.let { "Couldn't say so: ${it.message}" }
                Statuses.refresh(a.store, a.api)
                busy = false
            }
        }) { Text(c.label() + if (k == 0) " · as it is" else "") }
    }
    error?.let { Text(it, color = BAD, style = MaterialTheme.typography.bodySmall) }
}

/** One photo of a capture, whole, with what it was for and its readings. */
@Composable
fun PhotoScreen(a: MainActivity, s: Screen.Photo, go: (Screen) -> Unit) {
    val file = File(a.store.captureDir(s.id), (if (s.scenery) "scenery/" else "photos/") + s.name)
    val bitmap by produceState<Bitmap?>(null, file) { value = withContext(Dispatchers.IO) { decode(file, 1600) } }
    val shot = remember { a.store.contents(s.id).second?.shots?.firstOrNull { it.name == s.name } }
    val u = remember { a.store.upload(s.id) }
    Page(s.name, back = { go(Screen.Upload(s.id)) }) {
        bitmap?.let { Image(it.asImageBitmap(), null, Modifier.fillMaxWidth(), contentScale = ContentScale.FillWidth) } ?: Text("Reading it…")
        Text(s.group, fontWeight = FontWeight.SemiBold)
        val sent = u != null && s.name in (if (s.scenery) u.scenerySent else u.sent)
        Text(if (sent) "Sent" else "Not sent yet", color = if (sent) GOOD else Color.Unspecified)
        for (line in CaptureContents.readings(shot)) Text(line, style = MaterialTheme.typography.bodySmall)
    }
}

// ------------------------------------------------------------------------------------------------------------- viewer
/**
 * A mesh in the site's own viewer (its /m/<id> page, in a WebView), signed in as this phone is, so that an upload's mesh
 * nobody has approved yet shows too. With signal, the page and the mesh come from the server. A mesh kept on the phone
 * (OfflineStore) shows with none: each of the page's requests is answered from what's kept, and one kept without its
 * textures shows as a wireframe (?notex).
 */
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun ViewerScreen(a: MainActivity, s: Screen.Viewer, go: (Screen) -> Unit) {
    var failed by remember { mutableStateOf<String?>(null) }
    val server = a.store.server.trimEnd('/')
    // kept: the copy to show from, when the server can't be reached; decided before the page is opened
    val kept by produceState<Pair<Boolean, sh.boulderme.core.OfflineIndex?>?>(null, s.mesh) {
        value = withContext(Dispatchers.IO) { a.store.offline.index(s.mesh).let { k -> if (k == null || a.api.reachable()) true to null else false to k } }
    }
    Column(Modifier.fillMaxSize()) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(horizontal = 16.dp)) {
            TextButton(onClick = { go(s.back) }) { Text("‹ Back") }
            Text(s.name, style = MaterialTheme.typography.titleMedium, maxLines = 1)
        }
        val decided = kept ?: run { Text("Opening…", modifier = Modifier.padding(16.dp)); return@Column }
        val copy = decided.second
        if (copy != null) Text(if (copy.textures) "No signal: the copy kept on this phone" else "No signal: the copy kept on this phone, without its textures",
            style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(horizontal = 16.dp))
        failed?.let { Text("The mesh couldn't be shown: $it. " + if (copy == null) "Viewing needs signal, or a copy kept on the phone (Get meshes)." else "",
            color = BAD, modifier = Modifier.padding(16.dp)) }
        AndroidView(modifier = Modifier.fillMaxWidth().weight(1f), factory = { ctx ->
            WebView(ctx).apply {
                // not the holder's default wrap_content: a WebView whose height wraps lays its page out 0 high (so that
                // the content's height can size it), and the site's page, 100vh high with nothing over, then shows blank
                layoutParams = android.view.ViewGroup.LayoutParams(android.view.ViewGroup.LayoutParams.MATCH_PARENT, android.view.ViewGroup.LayoutParams.MATCH_PARENT)
                settings.javaScriptEnabled = true
                settings.domStorageEnabled = true
                webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                        val url = request.url.toString()
                        if (url.startsWith("$server/") || url == server) return false
                        runCatching { ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }      // elsewhere: the phone's browser
                        return true
                    }
                    override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): android.webkit.WebResourceResponse? =
                        copy?.let { a.store.offline.answer(it, server, request) }
                    override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                        if (request.isForMainFrame) failed = error.description?.toString() ?: "no connection"
                    }
                }
                // a WebView has no navigator.share: the page's Share sends its links to the phone's share sheet through this;
                // and the page's beta changed with no signal is kept by the app through it (BetaDrafts)
                addJavascriptInterface(ShareSheet(ctx, a.store), "BoulderMeshApp")
                // the page keeps its token in the site's own storage: put this phone's there, then open the mesh, in the
                // site's viewer alone (app)
                val page = "$server/m/${s.mesh}?app=android" + if (copy != null && !copy.textures) "&notex=1" else ""
                val keep = if (a.store.token.isEmpty()) "localStorage.removeItem('bm_token')" else "localStorage.setItem('bm_token',${JsonPrimitive(a.store.token)})"
                loadDataWithBaseURL("$server/", "<!doctype html><script>try{$keep}catch(e){};location.replace(${JsonPrimitive(page)})</script>", "text/html", "utf-8", null)
            }
        }, onRelease = { it.destroy() })
    }
}

/**
 * What the viewer's page may ask of the app (as window.BoulderMeshApp): a link, to the phone's share sheet; and to keep
 * the sets of beta it changed with no signal (BetaDrafts), and give them back.
 */
class ShareSheet(private val ctx: android.content.Context, private val store: Store) {
    private val mesh = Regex("[0-9a-f]{6,32}")

    @JavascriptInterface
    fun keptBeta(mesh: String): String = if (this.mesh.matches(mesh)) sh.boulderme.core.BetaDrafts.write(store.drafts(mesh)) else "[]"

    @JavascriptInterface
    fun keepBeta(mesh: String, drafts: String) {
        if (!this.mesh.matches(mesh) || drafts.length > 2_000_000) return
        store.saveDrafts(mesh, sh.boulderme.core.BetaDrafts.parse(drafts))
    }

    @JavascriptInterface
    fun share(title: String, url: String) {
        if (!url.startsWith("https://") && !url.startsWith("http://")) return
        val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, url).putExtra(Intent.EXTRA_SUBJECT, title)
        runCatching { ctx.startActivity(Intent.createChooser(send, null).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
    }
}

/** Keeps an upload's mesh for the viewer with no signal: asks how (with textures or without) once the server says what each takes. */
@Composable
fun KeepMesh(a: MainActivity, mesh: String, name: String) {
    var kept by remember { mutableStateOf(a.store.offline.index(mesh)) }
    var bundle by remember { mutableStateOf<sh.boulderme.core.OfflineBundle?>(null) }
    var busy by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var got by remember { mutableStateOf<Double?>(null) }      // how much has come while keeping it; -1 while waiting for Wi-Fi
    val scope = androidx.compose.runtime.rememberCoroutineScope()
    val k = kept
    if (k != null) Text(if (k.textures) "Kept on this phone, for viewing with no signal" else "Kept on this phone without its textures, for viewing with no signal",
        style = MaterialTheme.typography.bodySmall, color = GOOD)
    else if (busy != null && got != null) {
        val got = got ?: 0.0
        Text(if (got < 0) "Waiting for Wi-Fi: meshes are got only over Wi-Fi (Settings)" else "Keeping… ${(got * 100).toInt()}%",
            style = MaterialTheme.typography.bodySmall, color = if (got < 0) WAIT else Color.Unspecified)
        if (got >= 0) androidx.compose.material3.LinearProgressIndicator(progress = { got.toFloat() }, modifier = Modifier.fillMaxWidth())
    }
    else TextButton(enabled = busy == null, onClick = {
        busy = "Asking…"; error = null
        scope.launch { runCatching { withContext(Dispatchers.IO) { a.api.offline(mesh) } }.fold({ bundle = it }, { error = it.message }); busy = null }
    }) { Text(busy ?: "Keep for viewing with no signal") }
    error?.let { Text(it, color = BAD, style = MaterialTheme.typography.bodySmall) }
    bundle?.let { b ->
        KeepChoice(name, listOf(b.bytes), dismiss = { bundle = null }) { textures ->
            bundle = null; busy = "Keeping…"; got = 0.0
            scope.launch {
                runCatching {
                    withContext(Dispatchers.IO) {
                        a.store.offline.fetch(a.api, a.store, mesh, textures, b, progress = { got = it },
                            wait = { val was = got; a.store.awaitDownloads({ isActive }) { got = -1.0 }; if (got == -1.0) got = was })
                    }
                }.fold({ kept = it }, { if (it is kotlinx.coroutines.CancellationException) throw it; error = it.message })
                busy = null; got = null
            }
        }
    }
}
