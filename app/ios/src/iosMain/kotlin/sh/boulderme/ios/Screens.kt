package sh.boulderme.ios

import platform.AudioToolbox.AudioServicesPlaySystemSound
import platform.UIKit.UIColor
import platform.UIKit.UIModalPresentationFullScreen
import platform.UIKit.secondaryLabelColor
import sh.boulderme.core.GpsAverager
import sh.boulderme.core.MagCalibration
import sh.boulderme.core.MeshRow
import sh.boulderme.core.Plan

private fun summary(p: Plan): String {
    val holds = p.targets.count { it.isHold }
    val todo = p.targets.count { it.prio >= 2 && !it.isHold }
    val high = ((p.frame.height * p.frame.metresPerUnit) * 10).toInt() / 10.0
    return "$holds holds and $todo parts on its climbs to photograph · about $high m high" + if (p.frame.scaleGuessed) " (a guess)" else ""
}

private fun beep() = AudioServicesPlaySystemSound(1113u)

// --------------------------------------------------------------------------------------------------------------- home
class Home(private val app: App) : Form("BoulderMesh") {
    override fun appear() { app.uploader.onChange = { rebuild() } }

    private fun capture(mesh: String?, name: String) {
        val c = CaptureController(app, mesh, name) { id -> if (id != null) push(Done(app, id)) }
        c.modalPresentationStyle = UIModalPresentationFullScreen
        screen.presentViewController(c, animated = true, completion = null)
    }

    override fun build() {
        text("Photograph a boulder so that its mesh comes out whole, level, the right size and in the right place. " +
            "The app says where to stand and takes each photo when it's worth taking.", UIColor.secondaryLabelColor)

        section("Add to a mesh")
        val plans = app.store.downloaded()
        if (plans.isEmpty()) text("None downloaded yet. Get the meshes you'll visit while you have signal: they work with none afterwards.")
        for (p in plans) {
            text(p.name.ifEmpty { p.mesh }, bold = true)
            text(summary(p), small = true)
            row(makeButton("Capture", Look.FILLED) { capture(p.mesh, p.name.ifEmpty { p.mesh }) }, makeButton("Remove", Look.PLAIN) { app.store.removePlan(p.mesh); rebuild() })
        }
        button("Get meshes…") { push(Browse(app)) }

        section("A rock with no mesh")
        button("Capture a new rock…") { screen.ask("What is it?", "The rock's or climb's name", "Start") { capture(null, it.take(60)) } }

        val ids = app.store.captureIds()
        if (ids.isNotEmpty()) section("Captures on this phone")
        for (id in ids) {
            val u = app.store.upload(id) ?: continue
            val busy = app.uploader.busy(id)
            text(u.name, bold = true)
            val status = app.uploader.state[id]?.takeIf { busy } ?: when {
                u.done -> "Sent: the server is meshing it, or has"
                u.note.isNotEmpty() -> "Stopped: ${u.note}"
                u.sent.isNotEmpty() -> "${u.sent.size} of ${u.photos} photos sent"
                else -> "${u.photos} photos, not sent yet"
            }
            text(status, if (u.done) GOOD else if (u.note.isNotEmpty() && !busy) BAD else null, small = true)
            val delete = makeButton("Delete", Look.PLAIN, !busy) {
                screen.confirm("Delete this capture?", if (u.done) "Its photos are on the server; this takes them off the phone." else "Its ${u.photos} photos haven't all been sent, and can't be got back.",
                    "Delete", "Keep", danger = true) { app.store.deleteCapture(id); rebuild() }
            }
            if (u.done) row(delete) else row(makeButton(if (u.sent.isEmpty()) "Upload" else "Carry on", Look.FILLED, !busy) { app.uploader.start(id); rebuild() }, delete)
        }

        section("This phone")
        row(makeButton("Calibrate") { push(Calibrate(app)) }, makeButton("Settings") { push(Settings(app)) })
    }
}

// ------------------------------------------------------------------------------------------------------------- browse
class Browse(private val app: App) : Form("Get meshes") {
    private var rows: List<MeshRow>? = null
    private var error: String? = null
    private var search = ""
    private var shown = 40
    private val busy = HashSet<String>()
    private var asked = false

    private fun metres(r: MeshRow): Double? {
        val here = app.sensors.fix; val lat = r.lat; val lng = r.lng
        return if (here != null && lat != null && lng != null) GpsAverager.metresApart(here.lat, here.lng, lat, lng) else null
    }

    /** One after another: the server makes each mesh's plan as it's first asked for. */
    private fun get(list: List<MeshRow>) {
        val r = list.firstOrNull() ?: return
        busy.addAll(list.map { it.id })
        rebuild()
        app.api.plan(r.id) { plan, guide, why ->
            if (plan != null && guide != null) app.store.savePlan(r.id, plan, guide) else error = "${r.name}: $why"
            busy.remove(r.id)
            if (list.size > 1) get(list.drop(1)) else rebuild()
        }
    }

    override fun build() {
        if (!asked) {
            asked = true
            app.api.meshes { got, why -> rows = got; error = why?.let { "Couldn't list the meshes: $it" }; rebuild() }
        }
        val all = rows
        error?.let { text(it, BAD) }
        if (all == null) { if (error == null) text("Listing them…"); return }
        val have = app.store.downloaded().map { it.mesh }.toSet()
        val typing = field("Search by rock or climb", search) { search = it; shown = 40; rebuild() }
        if (search.isNotEmpty()) typing.becomeFirstResponder()      // the list is built again at each letter: typing carries on
        val match = all.filter { r -> search.isBlank() || r.name.contains(search, true) || r.climbs.any { it.contains(search, true) } }
            .sortedWith(compareBy<MeshRow> { metres(it) ?: Double.MAX_VALUE }.thenBy { it.name.lowercase() })
        val near = match.filter { (metres(it) ?: 1e9) <= 2000 && it.id !in have && it.id !in busy }
        if (near.size > 1 && search.isBlank()) button("Get the ${minOf(near.size, 40)} within 2 km") { get(near.take(40)) }
        text(if (app.sensors.fix == null) "No GPS fix yet: listed by name." else "Nearest first.", small = true)
        for (r in match.take(shown)) {
            val far = metres(r)?.let { if (it < 950) "${(it / 10).toInt() * 10} m" else "${(it / 100).toInt() / 10.0} km" }
            text(r.name.ifEmpty { r.id }, bold = true)
            text(listOfNotNull(far, if (r.holds > 0) "${r.holds} holds marked" else null, r.climbs.take(3).joinToString(", ").ifEmpty { null }).joinToString(" · "), UIColor.secondaryLabelColor, small = true)
            when (r.id) {
                in busy -> text("Getting…", small = true)
                in have -> text("On this phone", GOOD, small = true)
                else -> button("Get") { get(listOf(r)) }
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
        section("Uploading")
        text("Sending captures needs the site's editing token. Paste it, or paste the link or code from the admin page's \"Pair a phone\" QR, which gives it once.")
        field("Editing token", app.store.token, secret = true) { app.store.token = it }
        field("Pairing link or code", code) { code = it }
        button("Pair", Look.FILLED) {
            if (code.isBlank()) return@button
            app.api.pair(code) { token, why ->
                said = if (token != null) { app.store.token = token; code = ""; "Paired: this phone can upload now." to true } else "Pairing failed: $why" to false
                rebuild()
            }
        }
        said?.let { text(it.first, if (it.second) GOOD else BAD) }
        text("BoulderMesh ${IosStore.VERSION}", UIColor.secondaryLabelColor, small = true)
    }
}

// ---------------------------------------------------------------------------------------------------------- calibrate
/**
 * An iPhone keeps its own accelerometer and gyroscope trimmed, so which way is down needs nothing done. Its compass
 * reads true only after being turned every which way, which is worth half a minute before a capture; and GPS needs
 * nothing but time under open sky.
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
        section("Level")
        text("Nothing to do: an iPhone keeps its own motion sensors trimmed, and each photo records which way was down from the phone's tracking.")

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
        if (u.photos < 3) text("That's too few to mesh.", BAD)
        app.uploader.state[id]?.let { text(it, if (u.done) GOOD else if (it.startsWith("Stopped")) BAD else null) }
        if (busy) progress(if (u.photos > 0) u.sent.size.toDouble() / u.photos else 0.0)
        if (!u.done) button(if (u.sent.isEmpty()) "Upload now" else "Carry on uploading", Look.FILLED, !busy && u.photos >= 3) { app.uploader.start(id); rebuild() }
        if (app.store.token.isEmpty()) text("Uploading needs the site's editing token: see Settings.", WARN)
        text(if (u.done) "It's on the server." else "It stays on the phone until it's sent: go back to carry on later.", UIColor.secondaryLabelColor, small = true)
    }
}
