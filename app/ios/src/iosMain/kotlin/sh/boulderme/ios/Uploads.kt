package sh.boulderme.ios

import platform.UIKit.UIProgressView
import kotlinx.cinterop.ExperimentalForeignApi
import kotlinx.cinterop.readValue
import kotlinx.cinterop.useContents
import kotlinx.serialization.json.JsonPrimitive
import sh.boulderme.core.Offline
import platform.CoreGraphics.CGRectZero
import platform.CoreGraphics.CGSizeMake
import platform.Foundation.NSError
import platform.Foundation.NSURL
import platform.Foundation.NSURLRequest
import platform.UIKit.NSLayoutConstraint
import platform.UIKit.UIApplication
import platform.UIKit.UIColor
import platform.UIKit.UIImage
import platform.UIKit.UIImageView
import platform.UIKit.UILabel
import platform.UIKit.UILayoutConstraintAxisHorizontal
import platform.UIKit.UIStackView
import platform.UIKit.UIStackViewDistributionFillEqually
import platform.UIKit.UIView
import platform.UIKit.UIViewContentMode
import platform.UIKit.UIViewController
import platform.UIKit.secondaryLabelColor
import platform.UIKit.secondarySystemBackgroundColor
import platform.UIKit.systemBackgroundColor
import platform.UIKit.systemYellowColor
import platform.WebKit.WKNavigation
import platform.WebKit.WKNavigationAction
import platform.WebKit.WKNavigationActionPolicy
import platform.WebKit.WKNavigationDelegateProtocol
import platform.WebKit.WKUserScript
import platform.WebKit.WKUserScriptInjectionTime
import platform.WebKit.WKWebView
import platform.WebKit.WKWebViewConfiguration
import platform.darwin.DISPATCH_QUEUE_PRIORITY_DEFAULT
import platform.darwin.NSObject
import platform.darwin.dispatch_async
import platform.darwin.dispatch_get_global_queue
import platform.darwin.dispatch_get_main_queue
import sh.boulderme.core.CaptureContents
import sh.boulderme.core.UploadState
import sh.boulderme.core.SentCapture
import sh.boulderme.core.UploadStatus

/** Waiting: yellow, as nothing else is. */
val WAIT: UIColor get() = UIColor.systemYellowColor

fun toneColor(tone: String): UIColor? = when (tone) { "good" -> GOOD; "bad" -> BAD; "warn" -> WARN; "wait" -> WAIT; else -> null }

/**
 * What the server says of this phone's uploads (IosStore.statuses, kept for when there's no signal), and of the
 * account's captures that aren't on the phone (IosStore.sent), asked again each time the Home screen comes up and when
 * its Check is pressed: never on its own, and with no connection only when pressed (to say there's none).
 */
class Statuses(private val store: IosStore, private val api: IosApi) {
    var state: Map<String, UploadStatus> = store.statuses
    var sent: List<SentCapture> = store.sent
    var error: String? = null
    var busy = false

    fun refresh(pressed: Boolean = false, then: () -> Unit) = Net.known {
        if (busy) return@known
        if (store.token.isEmpty() && sent.isNotEmpty()) { sent = emptyList(); store.sent = sent; then() }
        if (!Net.online) {
            if (pressed) { error = "No connection: this is what the server said last"; then() }
            return@known
        }
        val local = store.captureIds().mapNotNull { store.upload(it) }
        val asked = local.filter { it.meshAsked || it.done }.mapNotNull { it.project }
        if (asked.isEmpty() && store.token.isEmpty()) return@known
        busy = true
        api.accountProjects { all, why ->
            if (why != null) { busy = false; error = "Couldn't ask the server: $why"; return@accountProjects then() }
            val others = all?.let { SentCapture.notOnPhone(it, local.map { u -> u.project }) }.orEmpty()
            val ids = asked + others.map { it.id }
            fun keep(got: Map<String, UploadStatus>) {
                store.statuses = got; state = got; store.sent = others; sent = others; error = null
            }
            if (ids.isEmpty()) { busy = false; keep(emptyMap()); return@accountProjects then() }
            api.status(ids) { got, why2 ->
                busy = false
                if (got != null) keep(got) else error = "Couldn't ask the server: $why2"
                then()
            }
        }
    }
}

/**
 * Whether the meshes kept on the phone are as the server has them now (MeshFresh, by mesh), asked each time Home or Get
 * meshes comes up while there's a connection, never on its own; and bringing one up to date: its plan and the viewer's
 * copy got again, or for one rebuilt, the newer mesh got in its place (the old one then goes, unless a capture still to
 * be sent goes by it).
 */
class MeshUpdates(private val store: IosStore, private val api: IosApi) {
    var state: Map<String, sh.boulderme.core.MeshFresh> = emptyMap()
    val busy = HashMap<String, String>()        // those being brought up to date, with how far they've got
    var error: String? = null

    fun refresh(then: () -> Unit) = Net.known {
        val have = store.downloaded().map { it.mesh }
        if (have.isEmpty() || !Net.online) return@known
        api.fresh(have) { got, _ -> if (got != null) { state = got; then() } }     // with none, what it said last stands
    }

    fun update(mesh: String, then: () -> Unit) {
        val fresh = state[mesh] ?: return
        if (mesh in busy) return
        busy[mesh] = "Starting…"; error = null; then()
        val target = fresh.newer ?: mesh
        val textures = store.offline.index(mesh)?.textures
        fun after(why: String?) {
            busy.remove(mesh)
            if (why != null) error = "Couldn't update it: $why"
            else if (fresh.newer != null && sh.boulderme.core.MeshFresh.mayDrop(mesh, store.captureIds().mapNotNull { store.upload(it) }.filter { !it.done }.map { it.mesh }))
                store.removePlan(mesh)
            then()
            if (why == null) refresh(then)
        }
        Net.await({ store.downloadWifiOnly }, { busy[mesh] = "Waiting for Wi-Fi"; then() }) {
            api.plan(target) { plan, guide, why ->
                if (plan == null || guide == null) return@plan after(why)
                store.savePlan(target, plan, guide)
                fetchLocatePack(store, api, target)
                if (textures == null) return@plan after(null)
                api.offline(target) { bundle, whyNot ->
                    if (bundle == null) after("the viewer's copy: $whyNot")
                    else store.offline.fetch(api, target, textures, bundle, progress = { busy[mesh] = "Getting… ${(it * 100).toInt()}%"; then() }) { _, w ->
                        after(w?.let { "the viewer's copy: $it" })
                    }
                }
            }
        }
    }
}

/** How a capture stands, in words and colour: on the phone, being sent, or what the server says of it. */
fun uploadSaid(u: UploadState, sending: String?, server: UploadStatus?): Pair<String, UIColor?> = when {
    sending != null -> sending to if (sending == Uploader.WAITING) WAIT else null
    server != null && (u.meshAsked || u.done) -> server.said().let { (text, tone) -> text to toneColor(tone) }
    u.done -> "Sent: the server is meshing it, or has" to GOOD
    u.note.isNotEmpty() -> "Stopped: ${u.note}" to BAD
    u.sent.isNotEmpty() -> "${u.sent.size} of ${u.photos} photos sent" to null
    else -> "${u.photos} photos, not sent yet" to null
}

// ------------------------------------------------------------------------------------------------------------ uploads
private val thumbs = LinkedHashMap<String, UIImage>()      // read on the main thread only; the last 200 kept

/** A photo, made about `px` across at most, off the main thread; then `done` with it, on the main thread. */
@OptIn(ExperimentalForeignApi::class)
private fun load(path: String, px: Double, done: (UIImage?) -> Unit) {
    dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT.toLong(), 0u)) {
        val whole = UIImage.imageWithContentsOfFile(path)
        val small = whole?.let { img ->
            val (w, h) = img.size.useContents { width to height }
            val k = minOf(1.0, px / maxOf(w, h, 1.0))
            img.imageByPreparingThumbnailOfSize(CGSizeMake(w * k, h * k))
        }
        dispatch_async(dispatch_get_main_queue()) { done(small) }
    }
}

/** A square of a photo, ticked if it's been sent, that opens it when pressed. */
private fun thumb(path: String, sent: Boolean, open: () -> Unit): UIView {
    val b = makeButton("", Look.PLAIN, action = open)
    b.backgroundColor = UIColor.secondarySystemBackgroundColor
    b.clipsToBounds = true
    val image = UIImageView()
    image.contentMode = UIViewContentMode.UIViewContentModeScaleAspectFill
    image.clipsToBounds = true
    image.userInteractionEnabled = false
    image.translatesAutoresizingMaskIntoConstraints = false
    b.addSubview(image)
    val constraints = mutableListOf(
        b.heightAnchor.constraintEqualToAnchor(b.widthAnchor),
        image.topAnchor.constraintEqualToAnchor(b.topAnchor), image.bottomAnchor.constraintEqualToAnchor(b.bottomAnchor),
        image.leadingAnchor.constraintEqualToAnchor(b.leadingAnchor), image.trailingAnchor.constraintEqualToAnchor(b.trailingAnchor))
    if (sent) {
        val tick = makeLabel("✓", GOOD, bold = true, size = 14.0)
        tick.backgroundColor = UIColor.blackColor.colorWithAlphaComponent(0.6)
        tick.translatesAutoresizingMaskIntoConstraints = false
        b.addSubview(tick)
        constraints += listOf(tick.trailingAnchor.constraintEqualToAnchor(b.trailingAnchor), tick.bottomAnchor.constraintEqualToAnchor(b.bottomAnchor))
    }
    NSLayoutConstraint.activateConstraints(constraints)
    val kept = thumbs[path]
    if (kept != null) image.image = kept
    else load(path, 240.0) { got ->
        if (got != null) { thumbs[path] = got; if (thumbs.size > 200) thumbs.remove(thumbs.keys.first()) }
        image.image = got
    }
    return b
}

/** One capture: what it holds, grouped by what each part was for, with what's been sent ticked; each photo opens. */
class UploadForm(private val app: App, private val id: String) : Form("What's in it") {
    private var keeping: String? = null
    private var keepPart = 0.0          // how much of its mesh has come, while keeping it
    private var keepBar: Pair<UILabel, UIProgressView>? = null
    private var keepError: String? = null
    private var choosing = false        // an area chosen, being told the server
    private var areaError: String? = null
    override fun appear() { app.statuses.refresh { rebuild() } }

    override fun build() {
        val u = app.store.upload(id)
        if (u == null) { text("This capture is gone."); return }
        val server = u.project?.let { app.statuses.state[it] }
        text(u.name, bold = true)
        val (said, color) = uploadSaid(u, null, server)
        text(said, color)
        reopenButton(app, id, u)
        server?.area?.let { area ->     // the area it's in, by its GPS; and where several are close by, which, asked
            area.said()?.let { text(it, small = true) }
            if (area.ask && u.project != null) {
                text("Which area is it in? Where its photos were taken is close to ${area.choices.size} of them:", WAIT)
                area.choices.forEachIndexed { k, c ->
                    button(c.label() + if (k == 0) " · as it is" else "", Look.PLAIN, !choosing) {
                        choosing = true; areaError = null; rebuild()
                        app.api.chooseArea(u.project!!, k) { why ->
                            areaError = why?.let { "Couldn't say so: $it" }
                            app.statuses.refresh { choosing = false; rebuild() }
                        }
                    }
                }
                areaError?.let { text(it, BAD, small = true) }
            }
        }
        server?.meshed?.let { mesh ->
            button("View its mesh", Look.FILLED) { push(ViewerController(app, mesh, u.name)) }
            val kept = app.store.offline.index(mesh)
            if (kept != null) text(if (kept.textures) "Kept on this phone, for viewing with no signal" else "Kept on this phone without its textures, for viewing with no signal", GOOD, small = true)
            else if (keeping == "Keeping…") keepBar = text("Keeping… ${(keepPart * 100).toInt()}%", small = true) to progress(keepPart)
            else button(keeping ?: "Keep for viewing with no signal", Look.PLAIN) {
                if (keeping != null) return@button
                keeping = "Asking…"; rebuild()
                app.api.offline(mesh) { bundle, why ->
                    keeping = null
                    if (bundle == null) { keepError = why; rebuild(); return@offline }
                    rebuild()
                    screen.keepChoice(u.name, listOf(bundle.bytes)) { textures ->
                        keeping = "Keeping…"; keepPart = 0.0; rebuild()
                        app.store.offline.fetch(app.api, mesh, textures, bundle,
                            progress = { part -> keepPart = part; keepBar?.let { (label, bar) -> label.text = "Keeping… ${(part * 100).toInt()}%"; bar.progress = part.toFloat() } },
                            waiting = { on -> keeping = if (on) "Waiting for Wi-Fi: meshes are got only over Wi-Fi (Settings)" else "Keeping…"; rebuild() }) { _, whyNot ->
                            keeping = null; keepBar = null; keepError = whyNot; rebuild()
                        }
                    }
                }
            }
            keepError?.let { text(it, BAD, small = true) }
        }
        val dir = app.store.captureDir(id)
        for (g in app.store.contents(id).first) {
            section(g.title)
            val sentOf = if (g.scenery) u.scenerySent else u.sent
            val count = if (g.photos.isEmpty()) "" else {
                val n = g.photos.count { it in sentOf }
                " · ${g.photos.size} ${if (g.scenery) "frame" else "photo"}${if (g.photos.size == 1) "" else "s"}, " +
                    if (n == g.photos.size) "all sent" else if (n == 0) "not sent yet" else "$n sent"
            }
            text(g.about + count, UIColor.secondaryLabelColor, small = true)
            for (row in g.photos.chunked(4)) {
                val line = UIStackView()
                line.axis = UILayoutConstraintAxisHorizontal
                line.distribution = UIStackViewDistributionFillEqually
                line.spacing = 4.0
                for (name in row) {
                    val path = "$dir/${if (g.scenery) "scenery" else "photos"}/$name"
                    line.addArrangedSubview(thumb(path, name in sentOf) { push(PhotoForm(app, id, name, g.scenery, g.title)) })
                }
                repeat(4 - row.size) { line.addArrangedSubview(UIView()) }
                add(line)
            }
            for ((name, about) in g.files) {
                val sent = when (name) { "capture.json" -> u.recordSent; "points.bin" -> u.pointsSent; "capture-log.jsonl" -> u.logSent; else -> false }
                text(name + if (sent) "  ✓ sent" else "  · not sent yet")
                text(about, UIColor.secondaryLabelColor, small = true)
            }
        }
    }
}

/** One photo of a capture, whole, with what it was for and its readings. */
class PhotoForm(private val app: App, private val id: String, private val name: String, private val scenery: Boolean, private val group: String) : Form(name) {
    private var image: UIImage? = null
    private var asked = false

    override fun build() {
        val path = "${app.store.captureDir(id)}/${if (scenery) "scenery" else "photos"}/$name"
        if (!asked) { asked = true; load(path, 1600.0) { image = it; rebuild() } }
        val got = image
        if (got == null) text("Reading it…") else {
            val view = UIImageView(image = got)
            view.contentMode = UIViewContentMode.UIViewContentModeScaleAspectFit
            val ratio = got.size.useContents { if (width > 0) height / width else 1.0 }
            NSLayoutConstraint.activateConstraints(listOf(view.heightAnchor.constraintEqualToAnchor(view.widthAnchor, multiplier = ratio)))
            add(view)
        }
        text(group, bold = true)
        val u = app.store.upload(id)
        val sent = u != null && name in (if (scenery) u.scenerySent else u.sent)
        text(if (sent) "Sent" else "Not sent yet", if (sent) GOOD else null)
        val shot = app.store.contents(id).second?.shots?.firstOrNull { it.name == name }
        for (line in CaptureContents.readings(shot)) text(line, small = true)
    }
}

// ------------------------------------------------------------------------------------------------------------- viewer
/** Keeps the viewer on the site: a link elsewhere opens in Safari. Says when the page couldn't be reached. */
class ViewerDelegate(private val server: String, private val failed: (String) -> Unit) : NSObject(), WKNavigationDelegateProtocol {
    override fun webView(webView: WKWebView, decidePolicyForNavigationAction: WKNavigationAction, decisionHandler: (WKNavigationActionPolicy) -> Unit) {
        val url = decidePolicyForNavigationAction.request.URL
        val text = url?.absoluteString ?: ""
        val main = decidePolicyForNavigationAction.targetFrame?.mainFrame ?: true
        if (!main || text.startsWith("$server/") || text == server || text.startsWith("about:") || text.startsWith("${Offline.SCHEME}://")) return decisionHandler(WKNavigationActionPolicy.WKNavigationActionPolicyAllow)
        if (url != null) UIApplication.sharedApplication.openURL(url, options = emptyMap<Any?, Any>(), completionHandler = null)
        decisionHandler(WKNavigationActionPolicy.WKNavigationActionPolicyCancel)
    }

    override fun webView(webView: WKWebView, didFailProvisionalNavigation: WKNavigation?, withError: NSError) = failed(withError.localizedDescription)
}

/**
 * A mesh in the site's own viewer (its /m/<id> page, in a web view), signed in as this phone is, so that an upload's
 * mesh nobody has approved yet shows too. With signal, the page and the mesh come from the server. A mesh kept on the
 * phone (IosOffline) shows with none, under the kept pages' own scheme (OfflinePages); one kept without its textures as a
 * wireframe (?notex).
 */
@OptIn(ExperimentalForeignApi::class)
class ViewerController(private val app: App, private val mesh: String, private val name: String) : UIViewController(nibName = null, bundle = null) {
    private val server = app.store.server.trimEnd('/')
    private val note = UILabel()
    private val column = UIStackView()
    private var pages: OfflinePages? = null     // held here: the web view holds its scheme's handler, but not past this
    private val bridge = BetaBridge(app.store)
    private val delegate = ViewerDelegate(server) { why ->       // held here: the web view holds its delegate weakly
        note.text = "The mesh couldn't be shown: $why. " + if (pages == null) "Viewing needs signal, or a copy kept on the phone (Get meshes)." else ""
        note.textColor = BAD
        note.hidden = false
    }

    override fun viewDidLoad() {
        super.viewDidLoad()
        title = name
        view.backgroundColor = UIColor.systemBackgroundColor
        note.numberOfLines = 0
        note.text = "Opening…"
        column.axis = platform.UIKit.UILayoutConstraintAxisVertical
        column.translatesAutoresizingMaskIntoConstraints = false
        column.addArrangedSubview(note)
        view.addSubview(column)
        NSLayoutConstraint.activateConstraints(listOf(
            column.topAnchor.constraintEqualToAnchor(view.safeAreaLayoutGuide.topAnchor),
            column.bottomAnchor.constraintEqualToAnchor(view.bottomAnchor),
            column.leadingAnchor.constraintEqualToAnchor(view.leadingAnchor),
            column.trailingAnchor.constraintEqualToAnchor(view.trailingAnchor)))
        val kept = app.store.offline.index(mesh)
        if (kept == null) open(null) else app.store.offline.reachable { up -> open(if (up) null else kept) }
    }

    private fun open(kept: sh.boulderme.core.OfflineIndex?) {
        val config = WKWebViewConfiguration()
        // the page keeps its token in the site's own storage: this phone's goes there before the page's own code runs
        val token = app.store.token
        val keep = if (token.isEmpty()) "localStorage.removeItem('bm_token')" else "localStorage.setItem('bm_token',${JsonPrimitive(token)})"
        val own = if (kept != null) "${Offline.SCHEME}://${Offline.HOST}/" else "$server/"
        config.userContentController.addUserScript(WKUserScript(
            "if (location.href.indexOf(${JsonPrimitive(own)}) === 0) { try { $keep } catch (e) {} }",
            WKUserScriptInjectionTime.WKUserScriptInjectionTimeAtDocumentStart, true))
        // beta changed with no signal: the page hands it to the app (bmBeta), and starts with what the app keeps (BetaDrafts)
        config.userContentController.addScriptMessageHandler(bridge, name = "bmBeta")
        config.userContentController.addUserScript(WKUserScript(bridge.script(mesh), WKUserScriptInjectionTime.WKUserScriptInjectionTimeAtDocumentStart, true))
        if (kept != null) OfflinePages(app.store.offline, kept, server).also { pages = it; config.setURLSchemeHandler(it, forURLScheme = Offline.SCHEME) }
        val web = WKWebView(frame = CGRectZero.readValue(), configuration = config)
        web.navigationDelegate = delegate
        web.translatesAutoresizingMaskIntoConstraints = false
        note.textColor = UIColor.secondaryLabelColor
        note.text = if (kept == null) "" else if (kept.textures) "No signal: the copy kept on this phone" else "No signal: the copy kept on this phone, without its textures"
        note.hidden = kept == null
        column.addArrangedSubview(web)
        // the site's viewer alone (app)
        val address = if (kept != null) own + "m/$mesh?app=ios" + (if (kept.textures) "" else "&notex=1") else "$server/m/$mesh?app=ios"
        NSURL.URLWithString(address)?.let { web.loadRequest(NSURLRequest.requestWithURL(it)) }
    }
}
