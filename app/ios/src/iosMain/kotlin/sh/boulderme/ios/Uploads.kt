package sh.boulderme.ios

import kotlinx.cinterop.ExperimentalForeignApi
import kotlinx.cinterop.readValue
import kotlinx.cinterop.useContents
import kotlinx.serialization.json.JsonPrimitive
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
import sh.boulderme.core.UploadStatus

/** Waiting: yellow, as nothing else is. */
val WAIT: UIColor get() = UIColor.systemYellowColor

fun toneColor(tone: String): UIColor? = when (tone) { "good" -> GOOD; "bad" -> BAD; "warn" -> WARN; "wait" -> WAIT; else -> null }

/**
 * What the server says of this phone's uploads (IosStore.statuses, kept for when there's no signal), asked again each
 * time the Home screen comes up and when its Check is pressed.
 */
class Statuses(private val store: IosStore, private val api: IosApi) {
    var state: Map<String, UploadStatus> = store.statuses
    var error: String? = null
    var busy = false

    fun refresh(then: () -> Unit) {
        val asked = store.captureIds().mapNotNull { store.upload(it) }.filter { it.meshAsked || it.done }.mapNotNull { it.project }
        if (asked.isEmpty() || busy) return
        busy = true
        api.status(asked) { got, why ->
            busy = false
            if (got != null) { store.statuses = got; state = got; error = null } else error = "Couldn't ask the server: $why"
            then()
        }
    }
}

/** How a capture stands, in words and colour: on the phone, being sent, or what the server says of it. */
fun uploadSaid(u: UploadState, sending: String?, server: UploadStatus?): Pair<String, UIColor?> = when {
    sending != null -> sending to null
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
    override fun appear() { app.statuses.refresh { rebuild() } }

    override fun build() {
        val u = app.store.upload(id)
        if (u == null) { text("This capture is gone."); return }
        val server = u.project?.let { app.statuses.state[it] }
        text(u.name, bold = true)
        val (said, color) = uploadSaid(u, null, server)
        text(said, color)
        server?.mesh?.takeIf { server.status == "done" && !server.meshGone }?.let { mesh -> button("View its mesh", Look.FILLED) { push(ViewerController(app, mesh, u.name)) } }
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
        if (!main || text.startsWith("$server/") || text == server || text.startsWith("about:")) return decisionHandler(WKNavigationActionPolicy.WKNavigationActionPolicyAllow)
        if (url != null) UIApplication.sharedApplication.openURL(url, options = emptyMap<Any?, Any>(), completionHandler = null)
        decisionHandler(WKNavigationActionPolicy.WKNavigationActionPolicyCancel)
    }

    override fun webView(webView: WKWebView, didFailProvisionalNavigation: WKNavigation?, withError: NSError) = failed(withError.localizedDescription)
}

/**
 * A mesh in the site's own viewer (its /m/<id> page, in a web view), signed in as this phone is, so that an upload's
 * mesh nobody has approved yet shows too. Needs signal: the page and the mesh come from the server.
 */
@OptIn(ExperimentalForeignApi::class)
class ViewerController(private val app: App, private val mesh: String, private val name: String) : UIViewController(nibName = null, bundle = null) {
    private val server = app.store.server.trimEnd('/')
    private val note = UILabel()
    private val delegate = ViewerDelegate(server) { why ->       // held here: the web view holds its delegate weakly
        note.text = "The mesh couldn't be shown: $why. Viewing needs signal."
        note.hidden = false
    }

    override fun viewDidLoad() {
        super.viewDidLoad()
        title = name
        view.backgroundColor = UIColor.systemBackgroundColor
        val config = WKWebViewConfiguration()
        // the page keeps its token in the site's own storage: this phone's goes there before the page's own code runs
        val token = app.store.token
        val keep = if (token.isEmpty()) "localStorage.removeItem('bm_token')" else "localStorage.setItem('bm_token',${JsonPrimitive(token)})"
        config.userContentController.addUserScript(WKUserScript(
            "if (location.href.indexOf(${JsonPrimitive("$server/")}) === 0) { try { $keep } catch (e) {} }",
            WKUserScriptInjectionTime.WKUserScriptInjectionTimeAtDocumentStart, true))
        val web = WKWebView(frame = CGRectZero.readValue(), configuration = config)
        web.navigationDelegate = delegate
        web.translatesAutoresizingMaskIntoConstraints = false
        note.numberOfLines = 0
        note.textColor = BAD
        note.hidden = true
        val column = UIStackView()
        column.axis = platform.UIKit.UILayoutConstraintAxisVertical
        column.translatesAutoresizingMaskIntoConstraints = false
        column.addArrangedSubview(note)
        column.addArrangedSubview(web)
        view.addSubview(column)
        NSLayoutConstraint.activateConstraints(listOf(
            column.topAnchor.constraintEqualToAnchor(view.safeAreaLayoutGuide.topAnchor),
            column.bottomAnchor.constraintEqualToAnchor(view.bottomAnchor),
            column.leadingAnchor.constraintEqualToAnchor(view.leadingAnchor),
            column.trailingAnchor.constraintEqualToAnchor(view.trailingAnchor)))
        NSURL.URLWithString("$server/m/$mesh")?.let { web.loadRequest(NSURLRequest.requestWithURL(it)) }
    }
}
