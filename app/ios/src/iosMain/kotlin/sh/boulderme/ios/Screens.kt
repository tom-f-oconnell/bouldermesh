package sh.boulderme.ios

import platform.UIKit.UIProgressView
import platform.UIKit.UILabel
import platform.Foundation.NSDate
import platform.Foundation.timeIntervalSince1970
import platform.AudioToolbox.AudioServicesPlaySystemSound
import platform.UIKit.UIColor
import platform.UIKit.UIModalPresentationFullScreen
import platform.UIKit.UITextField
import platform.UIKit.secondaryLabelColor
import sh.boulderme.core.TERMS
import sh.boulderme.core.SignInHelp
import platform.Foundation.NSURL
import platform.UIKit.UIApplication
import sh.boulderme.core.TERMS_VERSION
import sh.boulderme.core.CaptureGuide
import sh.boulderme.core.GpsAverager
import platform.Foundation.NSAttributedString
import platform.Foundation.NSMutableAttributedString
import platform.Foundation.create
import platform.Foundation.appendAttributedString
import platform.UIKit.navigationItem
import platform.UIKit.NSFontAttributeName
import platform.UIKit.NSForegroundColorAttributeName
import platform.UIKit.NSLinkAttributeName
import platform.UIKit.UIBarButtonItem
import platform.UIKit.UIEdgeInsetsMake
import platform.UIKit.UIFont
import platform.UIKit.UILayoutConstraintAxisHorizontal
import platform.UIKit.UIStackView
import platform.UIKit.UITextView
import platform.UIKit.accessibilityLabel
import sh.boulderme.core.MagCalibration
import sh.boulderme.core.MeshRow
import sh.boulderme.core.Plan
import sh.boulderme.core.Reopen
import sh.boulderme.core.UploadState
import sh.boulderme.core.UploadStatus

private fun summary(p: Plan): String {
    val holds = p.targets.count { it.isHold }
    val todo = p.targets.count { it.prio >= 2 && !it.isHold }
    val high = ((p.frame.height * p.frame.metresPerUnit) * 10).toInt() / 10.0
    return "$holds holds and $todo parts on its climbs to photograph · about $high m high" + if (p.frame.scaleGuessed) " (a guess)" else ""
}

/** A line of text that ends in a link: `lead`, then `link`, which opens `url` when tapped. */
private fun linkText(lead: String, link: String, url: String): UITextView {
    val font = UIFont.systemFontOfSize(16.0)
    val s = NSMutableAttributedString()
    s.appendAttributedString(NSAttributedString.create(string = lead, attributes = mapOf<Any?, Any?>(NSFontAttributeName to font, NSForegroundColorAttributeName to UIColor.secondaryLabelColor)))
    s.appendAttributedString(NSAttributedString.create(string = link, attributes = mapOf<Any?, Any?>(NSFontAttributeName to font, NSLinkAttributeName to NSURL.URLWithString(url))))
    val v = UITextView()
    v.setAttributedText(s)
    v.setEditable(false)
    v.scrollEnabled = false
    v.backgroundColor = UIColor.clearColor
    v.setTextContainerInset(UIEdgeInsetsMake(0.0, 0.0, 0.0, 0.0))
    v.textContainer.lineFragmentPadding = 0.0
    return v
}

private fun beep() = AudioServicesPlaySystemSound(1113u)

/** How long ago a time was (Unix seconds), in words. */
private fun ago(t: Double): String {
    val s = (NSDate().timeIntervalSince1970 - t).toLong()
    return when { s < 90 -> "just now"; s < 5400 -> "${s / 60} minutes ago"; s < 129600 -> "${s / 3600} hours ago"; else -> "${s / 86400} days ago" }
}

// --------------------------------------------------------------------------------------------------------------- home
/** Said of a meshed capture, as the app shows meshes only once got from Get meshes. */
private const val VIEW_HINT = "To view its mesh, get it from Get meshes… above"

class Home(private val app: App) : Form("boulderme.sh") {
    override fun appear() {
        app.uploader.onChange = { rebuild() }
        app.statuses.refresh { rebuild() }      // each time Home comes up: whether what was sent is meshed yet
        app.meshUpdates.refresh { rebuild() }   // and whether the meshes kept here changed on the server
    }

    init {      // top right: Calibrate, and Settings as the site's gear, as tall as the buttons beside it
        val bar = UIStackView()
        bar.axis = UILayoutConstraintAxisHorizontal
        bar.spacing = 8.0
        bar.addArrangedSubview(makeButton("Calibrate") { push(Calibrate(app)) })
        val gear = makeButton("⚙\uFE0E", fontSize = 30.0) { push(Settings(app)) }.apply { accessibilityLabel = "Settings" }
        gear.widthAnchor.constraintEqualToAnchor(gear.heightAnchor, multiplier = 1.3).setActive(true)     // else the bar stretches it
        bar.addArrangedSubview(gear)
        screen.navigationItem.rightBarButtonItem = UIBarButtonItem(customView = bar)
    }

    /**
     * Starts a capture. Before the phone's first outdoor one, while the compass hasn't been calibrated, asks whether to
     * do that first (`compass`); then says how its kind goes (`guide`), unless that was said not to be shown again.
     */
    fun capture(mesh: String?, name: String, set: String? = null, guide: Boolean = true, gym: Boolean = false, compass: Boolean = true) {
        if (compass && !gym && !app.store.compassAsked && app.store.magNote.isEmpty()) {
            app.store.compassAsked = true
            return screen.confirm(CaptureGuide.COMPASS_TITLE, CaptureGuide.COMPASS_TEXT, "Calibrate first", "Not now", then = { push(Calibrate(app)) },
                otherwise = { capture(mesh, name, set, guide, gym, compass = false) })
        }
        val kind = CaptureGuide.kind(mesh, set, gym)
        if (guide && kind.key !in CaptureGuide.hidden(app.store.guidesHidden)) return screen.guide(CaptureGuide.title(kind), CaptureGuide.text(kind)) { hide ->
            if (hide) app.store.guidesHidden = CaptureGuide.hide(app.store.guidesHidden, kind)
            capture(mesh, name, set, false, gym, compass = false)
        }
        val c = CaptureController(app, mesh, name, set, gym = gym) { id -> if (id != null) push(Done(app, id)) }
        c.modalPresentationStyle = UIModalPresentationFullScreen
        screen.presentViewController(c, animated = true, completion = null)
    }

    private var code = ""
    private var said: Pair<String, Boolean>? = null
    private var signingIn = false
    private var showCaptures = false

    /**
     * On first opening, while the phone isn't signed in: how to sign it in from where the account was joined
     * (SignInHelp), a browser on this phone first. Signing in, or Not now, means it isn't shown again (Settings has the same).
     */
    private fun signInPrompt() {
        val server = app.store.server
        section("Sign in")
        text(SignInHelp.INTRO)
        section("Joined in a browser on this phone")
        text(SignInHelp.samePhone(server))
        button("Open ${SignInHelp.site(server)}") {
            NSURL.URLWithString(SignInHelp.link(server))?.let { UIApplication.sharedApplication.openURL(it, options = emptyMap<Any?, Any>(), completionHandler = null) }
        }
        text(SignInHelp.OPEN_NOTE, UIColor.secondaryLabelColor, small = true)
        section("Joined on a computer or another phone")
        text(SignInHelp.otherDevice(server, scan = false))
        field("Code from the other device", code) { code = it }
        button(if (signingIn) "Signing in…" else "Sign in", Look.FILLED, enabled = !signingIn) {
            if (code.isBlank()) return@button
            signingIn = true; said = null; rebuild()
            signIn(app, code) { words, ok ->
                signingIn = false
                said = words to ok
                if (ok) { code = ""; screen.tell("Signed in", words) }
                rebuild()
            }
        }
        said?.let { text(it.first, if (it.second) GOOD else BAD) }
        text(SignInHelp.NOT_JOINED, UIColor.secondaryLabelColor, small = true)
        section("Later")
        button("Not now", Look.PLAIN) { app.store.signInAsked = true; rebuild() }
        text(SignInHelp.LATER, UIColor.secondaryLabelColor, small = true)
    }

    override fun build() {
        if (app.store.terms < TERMS_VERSION) {      // the terms, before anything else: the app goes no further until they're accepted
            section("Before you start")
            text(TERMS)
            button("I agree", Look.FILLED) { app.store.terms = TERMS_VERSION; rebuild() }
            text("Without this the app can't be used.", small = true)
            return
        }
        if (app.store.token.isEmpty() && !app.store.signInAsked) return signInPrompt()
        text("Photograph a boulder so that its mesh comes out whole, level, the right size and in the right place. " +
            "The app says where to stand and takes each photo when it's worth taking.", UIColor.secondaryLabelColor)
        add(linkText("To browse existing meshes, visit the site at ", "boulderme.sh", "https://boulderme.sh"))

        section("Add to a mesh")
        val plans = app.store.downloaded()
        if (plans.isEmpty()) text("None downloaded yet. Get the meshes you'll visit while you have signal: they work with none afterwards.")
        for (p in plans) {
            text(p.name.ifEmpty { p.mesh }, bold = true)
            text(summary(p), small = true)
            val f = app.meshUpdates.state[p.mesh]
            f?.said(p.key)?.let { text(it, WARN, small = true) }
            if (f != null && f.stale(p.key)) button(app.meshUpdates.busy[p.mesh] ?: f.action, enabled = p.mesh !in app.meshUpdates.busy) { app.meshUpdates.update(p.mesh) { rebuild() } }
            row(makeButton("Capture more", Look.FILLED) { capture(p.mesh, p.name.ifEmpty { p.mesh }) }
                    .apply { setContentCompressionResistancePriority(1000f, forAxis = UILayoutConstraintAxisHorizontal) },     // on one line
                makeButton("View") { push(ViewerController(app, p.mesh, p.name.ifEmpty { p.mesh })) },
                makeButton("Remove", Look.PLAIN) { app.store.removePlan(p.mesh); rebuild() })
            // for one of its sets of beta: that set's holds from close, all round, as a replica of them needs
            if (p.setsWithHolds.isNotEmpty()) text("Or for a set of beta, its holds in detail:", small = true)
            for (set in p.setsWithHolds) button("Capture hold closeups${set.name?.takeIf { p.setsWithHolds.size > 1 }?.let { ": $it" } ?: ""} (${set.targets.size} hold${if (set.targets.size == 1) "" else "s"})") { capture(p.mesh, p.name.ifEmpty { p.mesh }, set.id) }
        }
        app.meshUpdates.error?.let { text(it, BAD, small = true) }
        button("Get meshes…") { push(Browse(app)) }

        section("From scratch")
        button("Outdoor capture…") { screen.ask("What is it?", "The rock's or climb's name", "Start") { capture(null, it.take(60)) } }
        // a gym climb: one of its holds tapped, the rest of its colour found in the photos
        button("Gym capture…") { screen.ask("Which climb?", "The gym and climb, e.g. \"Yellow V3, the cave\"", "Start") { capture(null, it.take(60), gym = true) } }

        val ids = app.store.captureIds()
        val uploads = ids.mapNotNull { id -> app.store.upload(id)?.let { id to it } }
        val sent = app.statuses.sent
        // what's been captured, on the phone and on the server: folded away until pressed
        if (uploads.isEmpty() && sent.isEmpty()) return
        button((if (showCaptures) "▾ " else "▸ ") + "Your captures (${uploads.size + sent.size})", Look.PLAIN) { showCaptures = !showCaptures; rebuild() }
        if (!showCaptures) return
        if (uploads.isNotEmpty()) section("Captures on this phone")
        fun check() {
            val at = app.store.statusesAt
            row(makeButton(if (app.statuses.busy) "Checking…" else "Check if meshed", enabled = !app.statuses.busy) { app.statuses.refresh(pressed = true) { rebuild() }; rebuild() },
                makeLabel(if (at > 0) "Last asked ${ago(at)}" else "", UIColor.secondaryLabelColor, size = 13.0))
            app.statuses.error?.let { text(it, WARN, small = true) }
        }
        val checkHere = uploads.any { it.second.meshAsked || it.second.done }
        if (checkHere) check()
        for ((id, u) in uploads) {
            val busy = app.uploader.busy(id)
            button("${u.name}  ›", Look.PLAIN) { push(UploadForm(app, id)) }       // what's in it
            val server = u.project?.let { app.statuses.state[it] }
            val (status, color) = uploadSaid(u, app.uploader.state[id]?.takeIf { busy }, server)
            val mb = (Files.treeSize(app.store.captureDir(id)) / 100_000) / 10.0
            text("$mb MB · $status", color, small = true)
            if (server?.area?.ask == true) text("Which area is it in? Open it to say", WAIT, small = true)
            if (server?.meshed != null) text(VIEW_HINT, UIColor.secondaryLabelColor, small = true)
            val delete = makeButton("Delete", Look.PLAIN, !busy) {
                screen.confirm("Delete this capture?", if (u.done) "Its photos are on the server; this takes them off the phone." else "Its ${u.photos} photos haven't all been sent, and can't be got back.",
                    "Delete", "Keep", danger = true) { app.store.deleteCapture(id); rebuild() }
            }
            val send = if (u.done) null else makeButton(if (u.sent.isEmpty()) "Upload" else "Carry on", Look.FILLED, !busy) { app.uploader.start(id); rebuild() }
            row(*listOfNotNull(send, delete).toTypedArray())
        }

        // the account's captures on the server that aren't on the phone: after the app was deleted, or sent from another
        if (sent.isNotEmpty()) {
            section("Your captures on the server")
            text("Not on this phone (sent from another, or before the app was reinstalled): their photos are kept on the server.",
                UIColor.secondaryLabelColor, small = true)
            if (!checkHere) check()
        }
        for (x in sent) {
            text(x.title, bold = true)
            val server = app.statuses.state[x.id] ?: UploadStatus(x.status, x.mesh, approved = true)
            val (status, tone) = server.said()
            text((if (x.created > 0) ago(x.created) + " · " else "") + "${x.photos} photo${if (x.photos == 1) "" else "s"} · $status", toneColor(tone), small = true)
            if (server.meshed != null) text(VIEW_HINT, UIColor.secondaryLabelColor, small = true)
        }
    }
}

// ------------------------------------------------------------------------------------------------------------- browse
class Browse(private val app: App, private var getFirst: Boolean = false) : Form("Get meshes") {
    private var rows: List<MeshRow>? = null
    private var error: String? = null
    private var search = ""
    private var shown = 40
    private val busy = HashSet<String>()
    private var asked = false
    private val got = HashMap<String, Double>()         // how much of each being got has come
    private var waiting: String? = null                 // the one waiting for Wi-Fi
    private val bars = HashMap<String, Pair<UILabel, UIProgressView>>()     // shown for those, to move without building it all again

    private fun metres(r: MeshRow): Double? {
        val here = app.sensors.fix; val lat = r.lat; val lng = r.lng
        return if (here != null && lat != null && lng != null) GpsAverager.metresApart(here.lat, here.lng, lat, lng) else null
    }

    /** One after another: the server makes each mesh's plan as it's first asked for; then what the viewer needs of it. */
    private fun get(list: List<MeshRow>, textures: Boolean) {
        val r = list.firstOrNull() ?: return
        busy.addAll(list.map { it.id })
        rebuild()
        got[r.id] = 0.0
        fun after(why: String?) {
            if (why != null) error = "${r.name}: $why"
            busy.remove(r.id); got.remove(r.id)
            if (list.size > 1) get(list.drop(1), textures) else rebuild()
        }
        fun wait(on: Boolean) { waiting = if (on) r.id else null; rebuild() }
        fun viewer() = app.api.offline(r.id) { bundle, why ->
            if (bundle == null) after("the viewer's copy: $why")
            else app.store.offline.fetch(app.api, r.id, textures, bundle, progress = { got[r.id] = it; show(r.id) }, waiting = ::wait) { _, whyNot ->
                after(whyNot?.let { "the viewer's copy: $it" })
            }
        }
        if (app.store.plan(r.id) != null) viewer()
        else Net.await({ app.store.downloadWifiOnly }, { wait(true) }) {
            if (waiting == r.id) wait(false)
            app.api.plan(r.id) { plan, guide, why ->
                if (plan != null && guide != null) { app.store.savePlan(r.id, plan, guide); fetchLocatePack(app.store, app.api, r.id); viewer() } else after(why)
            }
        }
    }

    /** Moves a mesh's bar to how much of it has come. */
    private fun show(id: String) {
        val (label, bar) = bars[id] ?: return
        val part = got[id] ?: return
        label.text = "Getting… ${(part * 100).toInt()}%"
        bar.progress = part.toFloat()
    }

    private fun ask(list: List<MeshRow>) = screen.keepChoice(if (list.size == 1) list[0].name.ifEmpty { "this mesh" } else "these ${list.size} meshes",
        list.map { it.bytes }) { textures -> get(list, textures) }

    override fun build() {
        if (!asked) {
            asked = true
            app.meshUpdates.refresh { rebuild() }
            app.api.meshes { got, why ->
                rows = got; error = why?.let { "Couldn't list the meshes: $it" }
                rebuild()
                if (getFirst) { getFirst = false; got?.firstOrNull()?.let { get(listOf(it), true) } }
            }
        }
        val all = rows
        error?.let { text(it, BAD) }
        if (all == null) { if (error == null) text("Listing them…"); return }
        // the field stays as it is while the list under it changes, so that typing isn't cut off
        lateinit var typing: UITextField
        typing = field("Search by rock or climb", search) { search = it; shown = 40; clear(after = typing); list(all) }
        list(all)
    }

    private fun list(all: List<MeshRow>) {
        val plans = app.store.downloaded().associateBy { it.mesh }
        val have = plans.keys
        val fresh = app.meshUpdates.state
        // a mesh here a rebuild replaced: listed by its newer one, which brings it up to date in its place
        val replaced = plans.keys.mapNotNull { old -> fresh[old]?.newer?.let { it to old } }.toMap()
        val match = all.filter { r -> search.isBlank() || r.name.contains(search, true) || r.climbs.any { it.contains(search, true) } }
            .sortedWith(compareBy<MeshRow> { metres(it) ?: Double.MAX_VALUE }.thenBy { it.name.lowercase() })
        bars.clear()
        if (app.store.downloadWifiOnly) text(if (waiting != null) "Waiting for Wi-Fi: meshes are got only over Wi-Fi (Settings)." else "Meshes are got only over Wi-Fi (Settings).",
            if (waiting != null) WAIT else UIColor.secondaryLabelColor, small = true)
        val near = match.filter { (metres(it) ?: 1e9) <= 2000 && it.id !in have && it.id !in busy }
        if (near.size > 1 && search.isBlank()) button("Get the ${minOf(near.size, 40)} within 2 km") { ask(near.take(40)) }
        text(if (app.sensors.fix == null) "No GPS fix yet: listed by name." else "Nearest first.", small = true)
        for (r in match.take(shown)) {
            val far = metres(r)?.let { if (it < 950) "${(it / 10).toInt() * 10} m" else "${(it / 100).toInt() / 10.0} km" }
            text(r.name.ifEmpty { r.id }, bold = true)
            text(listOfNotNull(far, if (r.holds > 0) "${r.holds} holds marked" else null, r.climbs.take(3).joinToString(", ").ifEmpty { null }).joinToString(" · "), UIColor.secondaryLabelColor, small = true)
            val kept = app.store.offline.index(r.id)
            val old = replaced[r.id]?.takeIf { r.id !in have }        // the phone has an older version of it
            val stale = plans[r.id]?.let { p -> fresh[r.id]?.stale(p.key) } == true
            when {
                waiting == r.id -> text("Waiting for Wi-Fi", WAIT, small = true)
                r.id in got -> { bars[r.id] = text("Getting…", small = true) to progress(got[r.id] ?: 0.0); show(r.id) }
                r.id in busy -> text("Queued", small = true)
                (old ?: r.id) in app.meshUpdates.busy -> text(app.meshUpdates.busy[old ?: r.id] ?: "Updating…", small = true)
                old != null -> button("Get, in place of the older one") { app.meshUpdates.update(old) { rebuild() } }
                stale -> button("Update") { app.meshUpdates.update(r.id) { rebuild() } }
                r.id in have && kept != null -> text(if (kept.textures) "On this phone" else "On this phone · wireframe", GOOD, small = true)
                r.id in have -> button("Keep for viewing") { ask(listOf(r)) }
                else -> button("Get") { ask(listOf(r)) }
            }
        }
        if (match.size > shown) button("Show more (${match.size - shown})", Look.PLAIN) { shown += 40; rebuild() }
    }
}

// ----------------------------------------------------------------------------------------------------------- settings
class Settings(private val app: App) : Form("Settings") {
    private var code = ""
    private var said: Pair<String, Boolean>? = null

    override fun build() {
        field("Server", app.store.server) { app.store.server = it.ifBlank { IosStore.DEFAULT_SERVER } }
        field("Credit my photos to", app.store.credit) { app.store.credit = it.take(80) }
        text("Photos you send are published under Creative Commons Attribution 4.0 (CC BY 4.0): the site and anyone else may use them, crediting this name.")
        section(CaptureGuide.TITLE)
        for (k in CaptureGuide.Kind.entries) button(k.title) { screen.tell(CaptureGuide.title(k), CaptureGuide.text(k)) }
        button("Show tips again") { app.store.guidesHidden = ""; app.store.compassAsked = false; screen.tell("Tips", "Each kind of capture starts with how it goes again, and the compass is asked about again.") }
        section("Sounds while capturing")
        button(if (app.store.dingKept) "A bell when a photo is kept: on" else "A bell when a photo is kept: off", Look.PLAIN) { app.store.dingKept = !app.store.dingKept; rebuild() }
        button(if (app.store.tickSame) "Ticking while there's nothing new to photograph from where the phone is: on" else "Ticking while there's nothing new to photograph from where the phone is: off", Look.PLAIN) {
            app.store.tickSame = !app.store.tickSame; rebuild()
        }
        text("For a phone out of sight, up on a pole with Auto on: the ticking stops when the phone is somewhere new, and the bell says the photo from there was kept.")
        section("Wi-Fi or mobile data")
        button(if (app.store.downloadWifiOnly) "Getting meshes: only over Wi-Fi" else "Getting meshes: over Wi-Fi or mobile data", Look.PLAIN) {
            app.store.downloadWifiOnly = !app.store.downloadWifiOnly; rebuild()
        }
        button(if (app.store.uploadWifiOnly) "Sending captures: only over Wi-Fi" else "Sending captures: over Wi-Fi or mobile data", Look.PLAIN) {
            app.store.uploadWifiOnly = !app.store.uploadWifiOnly; rebuild()
        }
        text("Only over Wi-Fi waits for one (not a phone's hotspot), and carries on by itself once there's one, while the app is open.", UIColor.secondaryLabelColor, small = true)
        section("Uploading")
        button(if (app.store.anonFirst) "Holds others marked privately: asked for from the start" else "Holds others marked privately: asked about at the end", Look.PLAIN) {
            app.store.anonFirst = !app.store.anonFirst; rebuild()
        }
        text("Sending captures needs an account, or the site's own token. Paste an invite link, a code from \"Sign in another device\" on the site, or the admin page's pairing link.")
        field("Token", app.store.token, secret = true) { app.store.token = it }
        field("Pairing link or code", code) { code = it }
        button("Pair", Look.FILLED) {
            if (code.isBlank()) return@button
            signIn(app, code) { words, ok ->
                said = words to ok
                if (ok) code = ""
                rebuild()
            }
        }
        said?.let { text(it.first, if (it.second) GOOD else BAD) }
        text("boulderme.sh ${IosStore.VERSION}", UIColor.secondaryLabelColor, small = true)
    }
}

// ---------------------------------------------------------------------------------------------------------- calibrate
/**
 * What's worth doing before a capture: the compass, which reads true only after being turned every which way. Which
 * way is down is found at the start of each capture (a turn on the spot), and GPS needs nothing but time under open sky.
 */
class Calibrate(private val app: App) : Form("Calibrate") {
    private var mag: MagCalibration? = null
    private var ticks = 0
    private var magSaid = app.store.magNote
    private var magOk = true

    override fun appear() = watch()

    private fun watch() = every(if (mag != null) 0.05 else 1.0) {
        val c = mag
        if (c == null) { rebuild(); return@every }
        app.sensors.field()?.let { c.add(it) }
        ticks++
        if (c.covered() >= 0.75 || ticks > 600) finish(c) else if (ticks % 5 == 0) rebuild()
    }

    private fun finish(c: MagCalibration) {
        val r = c.solve()
        val accuracy = (app.sensors.fieldAccuracy() ?: 0).coerceIn(0, 3)
        if (r == null || r.covered < 0.5) { magSaid = "Not turned enough ways to tell: try again."; magOk = false } else {
            val earth = r.fieldUt in 22.0..67.0
            magOk = earth && r.rmsUt < 0.12 * r.fieldUt && accuracy >= 2
            magSaid = "Field ${r.fieldUt.toInt()} µT, uneven by ${(r.rmsUt * 10).toInt() / 10.0} µT; the phone rates its compass ${listOf("unreliable", "low", "medium", "high")[accuracy]}. " +
                if (magOk) "Good." else if (!earth) "That isn't the Earth's field alone: move away from metal and try again." else "Try again, more slowly."
            if (magOk) app.store.magNote = magSaid
        }
        beep()
        mag = null
        watch()
        rebuild()
    }

    override fun build() {
        section("Compass · half a minute")
        text("Away from cars, bolts and crash pads' buckles: turn the phone every which way, a slow figure of 8 that also rolls it over, until the bar fills.")
        val c = mag
        if (c == null) button("Start", Look.FILLED) { mag = MagCalibration(); ticks = 0; magSaid = ""; watch(); rebuild() } else progress(c.covered() / 0.75)
        if (magSaid.isNotEmpty()) text(magSaid, if (magOk) GOOD else BAD)

        section("GPS")
        val place = app.sensors.place.result()
        text(if (app.sensors.locationRefused) "Location isn't allowed for this app: allow it in Settings for photos to say where they were taken, and for a compass from true north."
            else if (place == null) "No fix yet. Under open sky it takes up to a minute."
            else "${app.sensors.place.count} fixes so far, good to about ${(place.accM * 10).toInt() / 10.0} m together. Nothing to do: a capture averages its fixes, and each photo carries its own.")

        section("Which way is down")
        text("Nothing to do here: each capture starts by asking for one slow turn on the spot, which finds it to a fraction of a degree whatever this phone's sensors are off by.")
    }
}

// --------------------------------------------------------------------------------------------------------------- done
class Done(private val app: App, private val id: String) : Form("Captured") {
    override fun appear() { app.uploader.onChange = { rebuild() } }

    override fun build() {
        val u = app.store.upload(id)
        if (u == null) { text("This capture is gone."); return }
        val busy = app.uploader.busy(id)
        text(u.name, bold = true)
        text("${u.photos} photos kept, each with which way was down, where the phone was and which way it faced.")
        app.store.scenery(id).size.takeIf { it > 0 }?.let { text("And $it frames of the scenery round the rock, sent last: Wi-Fi is best for them.") }
        if (u.photos < 3) text("That's too few to mesh.", BAD)
        app.uploader.state[id]?.let { text(it, if (u.done) GOOD else if (it.startsWith("Stopped")) BAD else if (it == Uploader.WAITING) WAIT else null) }
        if (busy) progress(if (u.photos > 0) u.sent.size.toDouble() / u.photos else 0.0)
        if (!u.done) button(if (u.sent.isEmpty()) "Upload now" else "Carry on uploading", Look.FILLED, !busy && u.photos >= 3) { app.uploader.start(id); rebuild() }
        text("Uploading publishes them under CC BY 4.0, credited to ${app.store.credit.ifBlank { "nobody by name (set one in Settings)" }}.")
        if (app.store.token.isEmpty()) text("Uploading needs the site's editing token: see Settings.", WARN)
        text(if (u.done) "It's on the server." else "It stays on the phone until it's sent: go back to carry on later.", UIColor.secondaryLabelColor, small = true)
        reopenButton(app, id, u)
        button("What's in it") { push(UploadForm(app, id)) }
    }
}

/**
 * Adding to a capture finished but not sent, while still at the rock: scenery, a panorama or photos missed (Reopen).
 * Nothing once any of it has been sent; why not, if it can't be.
 */
fun Form.reopenButton(app: App, id: String, u: UploadState) {
    val sent = u.done || u.meshAsked || u.recordSent || u.pointsSent || u.logSent || u.sent.isNotEmpty() || u.scenerySent.isNotEmpty()
    if (sent || app.uploader.busy(id)) return
    val record = Reopen.record(Files.readText("${app.store.captureDir(id)}/capture.json"))
    Reopen.cannot(record, u.mesh?.let { app.store.plan(it)?.key }, sent)?.let { text(it, UIColor.secondaryLabelColor, small = true); return }
    button("Add more: scenery, a panorama or photos") {
        val c = CaptureController(app, u.mesh, u.name, record?.detail?.set, reopen = id) { rebuild() }
        c.modalPresentationStyle = UIModalPresentationFullScreen
        screen.presentViewController(c, animated = true, completion = null)
    }
    val here = app.sensors.place.result()
    Reopen.far(record, here?.lat, here?.lng)?.let { text(it, WARN, small = true) }
}
