package sh.boulderme.app

import android.Manifest
import android.media.AudioManager
import android.media.ToneGenerator
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import sh.boulderme.core.CaptureGuide
import sh.boulderme.core.GpsAverager
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.RowScope
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withLink
import androidx.compose.ui.unit.sp
import sh.boulderme.core.MagCalibration
import sh.boulderme.core.MeshRow
import sh.boulderme.core.TERMS
import sh.boulderme.core.SignInHelp
import androidx.compose.foundation.layout.safeDrawingPadding
import sh.boulderme.core.Plan
import sh.boulderme.core.Reopen
import sh.boulderme.core.UploadStatus
import sh.boulderme.core.Vec3

val GOOD = Color(0xFF58C27D)
val WARN = Color(0xFFF0A030)
val BAD = Color(0xFFE5564B)

@Composable
internal fun Page(title: String, back: (() -> Unit)? = null, actions: (@Composable RowScope.() -> Unit)? = null, content: @Composable () -> Unit) {
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (back != null) TextButton(onClick = back) { Text("‹ Back") }
            Text(title, style = MaterialTheme.typography.headlineSmall, modifier = Modifier.padding(vertical = 12.dp).weight(1f))
            actions?.invoke(this)
        }
        content()
        Spacer(Modifier.height(24.dp))
    }
}

/**
 * How a kind of capture goes, step by step (CaptureGuide), with `ok` as its button. As a capture starts (`starting`),
 * with "Don't show again" at the bottom: go() is told whether it was ticked.
 */
@Composable
internal fun GuideDialog(kind: CaptureGuide.Kind, ok: String, starting: Boolean, dismiss: () -> Unit, go: (Boolean) -> Unit) {
    var hide by remember { mutableStateOf(false) }
    AlertDialog(onDismissRequest = dismiss, title = { Text(CaptureGuide.title(kind)) },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                for ((head, body) in CaptureGuide.steps(kind)) Column {
                    Text(head, fontWeight = FontWeight.SemiBold)
                    Text(body, style = MaterialTheme.typography.bodyMedium)
                }
                if (starting) Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(hide, { hide = it })
                    Text("Don't show again", Modifier.weight(1f))
                }
            }
        },
        confirmButton = { TextButton(onClick = { go(hide) }) { Text(ok) } },
        dismissButton = { if (starting) TextButton(onClick = dismiss) { Text("Cancel") } })
}

@Composable
internal fun Section(text: String) = Text(text.uppercase(), style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
    modifier = Modifier.padding(top = 10.dp))

/** Said of a meshed capture, as the app shows meshes only once got from Get meshes. */
private const val VIEW_HINT = "To view its mesh, get it from Get meshes… above"

private fun summary(p: Plan): String {
    val holds = p.targets.count { it.isHold }
    val todo = p.targets.count { it.prio >= 2 && !it.isHold }
    val size = "%.1f".format(p.frame.height * p.frame.metresPerUnit)
    return "$holds holds and $todo parts on its climbs to photograph · about $size m high" + if (p.frame.scaleGuessed) " (a guess)" else ""
}

// --------------------------------------------------------------------------------------------------------------- home
@Composable
fun HomeScreen(a: MainActivity, go: (Screen) -> Unit) {
    var tick by remember { mutableIntStateOf(0) }       // bumped to read the store again
    val plans = remember(tick) { a.store.downloaded() }
    val captures = remember(tick) { a.store.captureIds().mapNotNull { id -> a.store.upload(id)?.let { id to it } } }
    val sending by Uploader.state.collectAsState()
    var naming by remember { mutableStateOf(false) }
    var gym by remember { mutableStateOf(false) }      // what's being named is a gym climb
    var removing by remember { mutableStateOf<String?>(null) }
    var wanted by remember { mutableStateOf<Screen.Capture?>(null) }
    var refused by remember { mutableStateOf(false) }
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { got ->
        if (got[Manifest.permission.CAMERA] == true || a.has(Manifest.permission.CAMERA)) {
            a.sensors.stop(); a.sensors.start(a.has(Manifest.permission.ACCESS_FINE_LOCATION))
            wanted?.let(go)
        } else refused = true
        wanted = null
    }
    var guiding by remember { mutableStateOf<Screen.Capture?>(null) }
    var compass by remember { mutableStateOf<Screen.Capture?>(null) }
    var showCaptures by remember { mutableStateOf(false) }
    val begin: (Screen.Capture) -> Unit = { c ->
        if (a.has(Manifest.permission.CAMERA) && a.has(Manifest.permission.ACCESS_FINE_LOCATION)) go(c)
        else { wanted = c; ask.launch(arrayOf(Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)) }
    }
    // how its kind of capture goes, first, unless that was said not to be shown again
    val guide: (Screen.Capture) -> Unit = { c ->
        if (CaptureGuide.kind(c.mesh, c.set, c.gym).key in CaptureGuide.hidden(a.store.guidesHidden)) begin(c) else guiding = c
    }
    // the first outdoor capture, while the compass hasn't been calibrated: whether to do that first
    val start: (Screen.Capture) -> Unit = { c ->
        if (!c.gym && !a.store.compassAsked && a.store.magNote.isEmpty()) compass = c else guide(c)
    }
    LaunchedEffect(sending) { tick++ }
    val update by Updates.state.collectAsState()
    val statuses by Statuses.state.collectAsState()
    val checking by Statuses.busy.collectAsState()
    val checkFailed by Statuses.error.collectAsState()
    val sent by Statuses.sent.collectAsState()
    val scope = rememberCoroutineScope()
    LaunchedEffect(Unit) { Statuses.refresh(a.store, a.api) }      // each time Home comes up: whether what was sent is meshed yet
    LaunchedEffect(Unit) { MeshUpdates.refresh(a) }                 // and whether the meshes kept here changed on the server
    val fresh by MeshUpdates.state.collectAsState()
    val updating by MeshUpdates.busy.collectAsState()
    val updateFailed by MeshUpdates.error.collectAsState()

    Page("boulderme.sh", actions = {
        OutlinedButton(onClick = { go(Screen.Calibrate) }) { Text("Calibrate") }
        OutlinedButton(onClick = { go(Screen.Settings) }, contentPadding = PaddingValues(horizontal = 10.dp),
            modifier = Modifier.semantics { contentDescription = "Settings" }) { Text("⚙\uFE0E", fontSize = 24.sp, lineHeight = 24.sp) }
    }) {
        update.newer?.let { newer ->        // the app's newer build (Update.kt): installs over this one, keeping its captures
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("A newer version of the app is ready (build ${newer.build}).")
                    Button(enabled = update.busy == null, onClick = { Updates.state.value = update.copy(busy = "Starting…", error = null); Updates.install(a, newer) }) {
                        Text(update.busy ?: "Update")
                    }
                    update.error?.let { Text(it, color = BAD) }
                }
            }
        }
        Text("Photograph a boulder so that its mesh comes out whole, level, the right size and in the right place. " +
            "The app says where to stand and takes each photo when it's worth taking.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(buildAnnotatedString {
            append("To browse existing meshes, visit the site at ")
            withLink(LinkAnnotation.Url("https://boulderme.sh", TextLinkStyles(SpanStyle(color = MaterialTheme.colorScheme.primary, textDecoration = TextDecoration.Underline)))) { append("boulderme.sh") }
        }, color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (refused) Text("Capturing needs the camera: allow it in the phone's settings for this app.", color = BAD)

        Section("Add to a mesh")
        if (plans.isEmpty()) Text("None downloaded yet. Get the meshes you'll visit while you have signal: they work with none afterwards.")
        for (p in plans) Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(p.name.ifEmpty { p.mesh }, fontWeight = FontWeight.SemiBold)
                Text(summary(p), style = MaterialTheme.typography.bodySmall)
                val f = fresh[p.mesh]
                f?.said(p.key)?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = WARN) }
                if (f != null && f.stale(p.key)) OutlinedButton(enabled = p.mesh !in updating, onClick = { scope.launch { MeshUpdates.update(a, p.mesh); tick++ } }) {
                    Text(updating[p.mesh] ?: f.action)
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = { start(Screen.Capture(p.mesh, p.name.ifEmpty { p.mesh })) }) { Text("Capture more") }
                    OutlinedButton(onClick = { go(Screen.Viewer(p.mesh, p.name.ifEmpty { p.mesh })) }) { Text("View") }
                    TextButton(onClick = { a.store.removePlan(p.mesh); tick++ }) { Text("Remove") }
                }
                // for one of its sets of beta: that set's holds from close, all round, as a replica of them needs
                if (p.setsWithHolds.isNotEmpty()) Text("Or for a set of beta, its holds in detail:", style = MaterialTheme.typography.bodySmall)
                for (set in p.setsWithHolds) OutlinedButton(onClick = { start(Screen.Capture(p.mesh, p.name.ifEmpty { p.mesh }, set.id)) }) {
                    Text("Capture hold closeups${set.name?.takeIf { p.setsWithHolds.size > 1 }?.let { ": $it" } ?: ""} (${set.targets.size} hold${if (set.targets.size == 1) "" else "s"})")
                }
            }
        }
        updateFailed?.let { Text(it, color = BAD, style = MaterialTheme.typography.bodySmall) }
        OutlinedButton(onClick = { go(Screen.Browse()) }, modifier = Modifier.fillMaxWidth()) { Text("Get meshes…") }

        Section("From scratch")
        OutlinedButton(onClick = { gym = false; naming = true }, modifier = Modifier.fillMaxWidth()) { Text("Outdoor capture…") }
        OutlinedButton(onClick = { gym = true; naming = true }, modifier = Modifier.fillMaxWidth()) { Text("Gym capture…") }

        val check = @Composable {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(enabled = !checking, onClick = { scope.launch { Statuses.refresh(a.store, a.api, pressed = true) } }) { Text(if (checking) "Checking…" else "Check if meshed") }
                val at = a.store.statusesAt
                if (at > 0) Text("Last asked " + android.text.format.DateUtils.getRelativeTimeSpanString(at), style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            checkFailed?.let { Text(it, color = WARN, style = MaterialTheme.typography.bodySmall) }
        }
        val checkHere = captures.any { it.second.meshAsked || it.second.done }
        // what's been captured, on the phone and on the server: folded away until pressed
        val sizes = remember(tick) { captures.associate { (id, _) -> id to a.store.captureDir(id).walkTopDown().filter { it.isFile }.sumOf { it.length() } } }
        if (captures.isNotEmpty() || sent.isNotEmpty()) TextButton(onClick = { showCaptures = !showCaptures }, contentPadding = PaddingValues(0.dp)) {
            Text((if (showCaptures) "▾ " else "▸ ") + "Your captures (${captures.size + sent.size})".uppercase(), style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (showCaptures) {
            if (captures.isNotEmpty()) Section("Captures on this phone")
            if (checkHere) check()
            for ((id, u) in captures) Card(onClick = { go(Screen.Upload(id)) }, modifier = Modifier.fillMaxWidth()) {     // pressed: what's in it
                Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("${u.name}  ›", fontWeight = FontWeight.SemiBold)
                    val server = u.project?.let { statuses[it] }
                    val (status, color) = uploadSaid(u, sending[id]?.takeIf { Uploader.busy(id) }, server)
                    Text("%.1f MB · ".format((sizes[id] ?: 0L) / 1e6) + status, style = MaterialTheme.typography.bodySmall, color = color)
                    if (server?.area?.ask == true) Text("Which area is it in? Open it to say", style = MaterialTheme.typography.bodySmall, color = WAIT)
                    if (server?.meshed != null) Text(VIEW_HINT, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        if (!u.done) Button(enabled = !Uploader.busy(id), onClick = { Uploader.start(a.store, a.api, id) }) { Text(if (u.sent.isEmpty()) "Upload" else "Carry on") }
                        TextButton(enabled = !Uploader.busy(id), onClick = { removing = id }) { Text("Delete") }
                    }
                }
            }

            // the account's captures on the server that aren't on the phone: after the app was uninstalled, or sent from another
            if (sent.isNotEmpty()) {
                Section("Your captures on the server")
                Text("Not on this phone (sent from another, or before the app was reinstalled): their photos are kept on the server.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (!checkHere) check()
            }
            for (x in sent) Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(x.title, fontWeight = FontWeight.SemiBold)
                    val server = statuses[x.id] ?: UploadStatus(x.status, x.mesh, approved = true)
                    val (status, tone) = server.said()
                    val day = if (x.created > 0) android.text.format.DateUtils.formatDateTime(a, (x.created * 1000).toLong(), android.text.format.DateUtils.FORMAT_SHOW_DATE) + " · " else ""
                    Text("$day${x.photos} photo${if (x.photos == 1) "" else "s"} · $status", style = MaterialTheme.typography.bodySmall, color = toneColor(tone))
                    if (server.meshed != null) Text(VIEW_HINT, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
    guiding?.let { c ->
        GuideDialog(CaptureGuide.kind(c.mesh, c.set, c.gym), "Start", starting = true, dismiss = { guiding = null }) { hide ->
            guiding = null
            if (hide) a.store.guidesHidden = CaptureGuide.hide(a.store.guidesHidden, CaptureGuide.kind(c.mesh, c.set, c.gym))
            begin(c)
        }
    }
    compass?.let { c ->
        AlertDialog(onDismissRequest = { compass = null }, title = { Text(CaptureGuide.COMPASS_TITLE) }, text = { Text(CaptureGuide.COMPASS_TEXT) },
            confirmButton = { TextButton(onClick = { compass = null; a.store.compassAsked = true; go(Screen.Calibrate) }) { Text("Calibrate first") } },
            dismissButton = { TextButton(onClick = { compass = null; a.store.compassAsked = true; guide(c) }) { Text("Not now") } })
    }

    if (naming) {
        var name by remember { mutableStateOf("") }
        AlertDialog(onDismissRequest = { naming = false }, title = { KeepDialogInTopHalf(); Text(if (gym) "Which climb?" else "What is it?") },
            text = { Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (gym) Text("You'll tap one of its holds: the others of that colour are found in the photos and asked for close up. " +
                    "It goes by colour alone, so other climbs' holds of the same colour nearby are found too: leave those out as they come.",
                    style = MaterialTheme.typography.bodySmall)
                OutlinedTextField(name, { name = it.take(60) }, label = { Text(if (gym) "The gym and climb, e.g. \"Yellow V3, the cave\"" else "The rock's or climb's name") }, singleLine = true)
            } },
            confirmButton = { TextButton(enabled = name.isNotBlank(), onClick = { naming = false; start(Screen.Capture(null, name.trim(), gym = gym)) }) { Text("Start") } },
            dismissButton = { TextButton(onClick = { naming = false }) { Text("Cancel") } })
    }
    removing?.let { id ->
        val u = a.store.upload(id)
        AlertDialog(onDismissRequest = { removing = null }, title = { KeepDialogInTopHalf(); Text("Delete this capture?") },
            text = { Text(if (u?.done == true) "Its photos are on the server; this takes them off the phone." else "Its ${u?.photos ?: 0} photos haven't all been sent, and can't be got back.") },
            confirmButton = { TextButton(onClick = { a.store.deleteCapture(id); removing = null; tick++ }) { Text("Delete", color = BAD) } },
            dismissButton = { TextButton(onClick = { removing = null }) { Text("Keep") } })
    }
}

// ------------------------------------------------------------------------------------------------------------- browse
@Composable
fun BrowseScreen(a: MainActivity, getFirst: Boolean, go: (Screen) -> Unit) {
    var rows by remember { mutableStateOf<List<MeshRow>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var search by remember { mutableStateOf("") }
    var have by remember { mutableStateOf(a.store.downloaded().map { it.mesh }.toSet()) }
    var busy by remember { mutableStateOf(setOf<String>()) }
    var viewing by remember { mutableStateOf(rows.orEmpty().associate { it.id to a.store.offline.index(it.id) }) }
    var asking by remember { mutableStateOf<List<MeshRow>?>(null) }     // which to keep, once it's said how
    var progress by remember { mutableStateOf(mapOf<String, Double>()) }  // how much of each being got has come
    var waiting by remember { mutableStateOf<String?>(null) }           // the one waiting for Wi-Fi
    val scope = rememberCoroutineScope()
    LaunchedEffect(Unit) {
        try { rows = withContext(Dispatchers.IO) { a.api.meshes() } } catch (e: Exception) { error = "Couldn't list the meshes: ${e.message}" }
    }
    LaunchedEffect(Unit) { MeshUpdates.refresh(a) }
    val fresh by MeshUpdates.state.collectAsState()
    val updating by MeshUpdates.busy.collectAsState()
    val plans = remember(have, updating) { a.store.downloaded().associateBy { it.mesh } }
    // a mesh here a rebuild replaced: listed by its newer one, which brings it up to date in its place
    val replaced = remember(fresh, plans) { plans.keys.mapNotNull { old -> fresh[old]?.newer?.let { it to old } }.toMap() }
    LaunchedEffect(rows) { viewing = withContext(Dispatchers.IO) { rows.orEmpty().associate { it.id to a.store.offline.index(it.id) } } }
    val here = a.sensors.fix
    fun metres(r: MeshRow): Double? { val lat = r.lat; val lng = r.lng; return if (here != null && lat != null && lng != null) GpsAverager.metresApart(here.lat, here.lng, lat, lng) else null }
    val shown = (rows ?: emptyList()).filter { r -> search.isBlank() || r.name.contains(search, true) || r.climbs.any { it.contains(search, true) } }
        .sortedWith(compareBy<MeshRow> { metres(it) ?: Double.MAX_VALUE }.thenBy { it.name.lowercase() })
    // one after another: the server makes each mesh's plan as it's first asked for; then what the viewer needs of it
    fun get(list: List<MeshRow>, textures: Boolean) {
        busy = busy + list.map { it.id }
        scope.launch {
            for (r in list) {
                progress = progress + (r.id to 0.0)
                try {
                    withContext(Dispatchers.IO) {
                        val wait = { a.store.awaitDownloads({ isActive }) { waiting = r.id }; waiting = null }
                        if (r.id !in have) {
                            wait()
                            val (plan, guide) = a.api.plan(r.id)
                            a.store.savePlan(r.id, plan, guide)
                            runCatching { a.store.plan(r.id)?.let { p -> a.api.locatePack(p)?.let { a.store.saveLocatePack(r.id, it) } } }     // for lining up with no taps and no signal
                            have = have + r.id
                        }
                        val index = a.store.offline.fetch(a.api, a.store, r.id, textures, progress = { progress = progress + (r.id to it) }, wait = wait)
                        viewing = viewing + (r.id to index)
                    }
                } catch (e: kotlinx.coroutines.CancellationException) { throw e
                } catch (e: Exception) { error = "${r.name}: ${e.message}" }
                busy = busy - r.id; progress = progress - r.id
            }
        }
    }
    var gotFirst by remember { mutableStateOf(!getFirst) }
    LaunchedEffect(rows) { val first = rows?.firstOrNull(); if (!gotFirst && first != null) { gotFirst = true; get(listOf(first), true) } }
    asking?.let { list ->
        KeepChoice(if (list.size == 1) list[0].name.ifEmpty { "this mesh" } else "these ${list.size} meshes", list.map { it.bytes },
            dismiss = { asking = null }) { textures -> asking = null; get(list, textures) }
    }

    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = { go(Screen.Home) }) { Text("‹ Back") }
            Text("Get meshes", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.padding(vertical = 12.dp))
        }
        OutlinedTextField(search, { search = it }, label = { Text("Search by rock or climb") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        error?.let { Text(it, color = BAD) }
        if (rows == null && error == null) Text("Listing them…")
        if (rows != null) {
            val near = shown.filter { (metres(it) ?: 1e9) <= 2000 && it.id !in have }
            if (near.size > 1 && search.isBlank()) OutlinedButton(onClick = { asking = near.take(40) }) { Text("Get the ${minOf(near.size, 40)} within 2 km") }
            Text(if (here == null) "No GPS fix yet: listed by name." else "Nearest first.", style = MaterialTheme.typography.bodySmall)
        }
        if (a.store.downloadWifiOnly) Text(if (waiting != null) "Waiting for Wi-Fi: meshes are got only over Wi-Fi (Settings)." else "Meshes are got only over Wi-Fi (Settings).",
            style = MaterialTheme.typography.bodySmall, color = if (waiting != null) WAIT else MaterialTheme.colorScheme.onSurfaceVariant)
        LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.weight(1f)) {
            items(shown, key = { it.id }) { r ->
                Column {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(r.name.ifEmpty { r.id })
                        val far = metres(r)?.let { if (it < 950) "${(it / 10).toInt() * 10} m" else "%.1f km".format(it / 1000) }
                        Text(listOfNotNull(far, if (r.holds > 0) "${r.holds} holds marked" else null, r.climbs.take(3).joinToString(", ").ifEmpty { null }).joinToString(" · "),
                            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    val kept = viewing[r.id]
                    val got = progress[r.id]
                    val old = replaced[r.id]?.takeIf { r.id !in have }        // the phone has an older version of it
                    val stale = plans[r.id]?.let { p -> fresh[r.id]?.stale(p.key) } == true
                    when {
                        waiting == r.id -> Text("Waiting for Wi-Fi", color = WAIT)
                        got != null -> Text("Getting… ${(got * 100).toInt()}%")
                        r.id in busy -> Text("Queued")
                        (old ?: r.id) in updating -> Text(updating[old ?: r.id] ?: "Updating…")
                        old != null -> TextButton(onClick = { scope.launch { MeshUpdates.update(a, old); have = a.store.downloaded().map { it.mesh }.toSet() } }) { Text("Get, in place of the older one") }
                        stale -> TextButton(onClick = { scope.launch { MeshUpdates.update(a, r.id); viewing = viewing + (r.id to a.store.offline.index(r.id)) } }) { Text("Update") }
                        r.id in have && kept != null -> Text(if (kept.textures) "On phone" else "On phone · wireframe", color = GOOD)
                        r.id in have -> TextButton(onClick = { asking = listOf(r) }) { Text("Keep for viewing") }
                        else -> TextButton(onClick = { asking = listOf(r) }) { Text("Get") }
                    }
                }
                progress[r.id]?.let { got -> LinearProgressIndicator(progress = { got.toFloat() }, modifier = Modifier.fillMaxWidth()) }
                }
            }
        }
    }
}

// ----------------------------------------------------------------------------------------------------------- settings
/** The terms, before anything else: the app goes no further until they're accepted. */
@Composable
fun TermsScreen(accept: () -> Unit) {
    Column(Modifier.fillMaxSize().safeDrawingPadding().padding(20.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Text("Before you start", style = MaterialTheme.typography.headlineSmall)
        Text(TERMS)
        Button(onClick = accept, modifier = Modifier.fillMaxWidth()) { Text("I agree") }
        Text("Without this the app can't be used.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

// ------------------------------------------------------------------------------------------------------------ sign in
/**
 * On first opening, while the phone isn't signed in: how to sign it in from where the account was joined (SignInHelp),
 * a browser on this phone first. Leaving it, or signing in, means it isn't shown again (Settings has the same).
 */
@Composable
fun SignInScreen(a: MainActivity, go: (Screen) -> Unit) {
    var code by remember { mutableStateOf("") }
    var said by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val server = a.store.server
    fun signIn(text: String) = scope.launch {
        busy = true; said = null
        try {
            val name = withContext(Dispatchers.IO) { a.api.signIn(text) }
            a.store.signInAsked = true
            android.widget.Toast.makeText(a, if (name != null) "Signed in as $name." else "Paired: this phone can upload now.", android.widget.Toast.LENGTH_LONG).show()
            go(Screen.Home)
        } catch (e: Exception) { said = "Signing in failed: ${e.message}" }
        busy = false
    }
    Page("Sign in") {
        Text(SignInHelp.INTRO)
        Section("Joined in a browser on this phone")
        Text(SignInHelp.samePhone(server))
        OutlinedButton(onClick = {
            runCatching { a.startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW, android.net.Uri.parse(SignInHelp.link(server)))) }
                .onFailure { said = "No browser opened: open ${SignInHelp.site(server)} yourself." }
        }) { Text("Open ${SignInHelp.site(server)}") }
        Text(SignInHelp.OPEN_NOTE, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Section("Joined on a computer or another phone")
        Text(SignInHelp.otherDevice(server, scan = true))
        Button(enabled = !busy, onClick = { scanQr(a) { text, why -> if (text != null) signIn(text) else if (why != null) said = why } }) { Text("Scan") }
        OutlinedTextField(code, { code = it }, label = { Text("Code from the other device") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Button(enabled = !busy && code.isNotBlank(), onClick = { signIn(code) }) { Text(if (busy) "Signing in…" else "Sign in") }
        said?.let { Text(it, color = BAD) }
        Text(SignInHelp.NOT_JOINED, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Section("Later")
        OutlinedButton(onClick = { a.store.signInAsked = true; go(Screen.Home) }) { Text("Not now") }
        Text(SignInHelp.LATER, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
fun SettingsScreen(a: MainActivity, go: (Screen) -> Unit) {
    var server by remember { mutableStateOf(a.store.server) }
    var credit by remember { mutableStateOf(a.store.credit) }
    var anonFirst by remember { mutableStateOf(a.store.anonFirst) }
    var dingKept by remember { mutableStateOf(a.store.dingKept) }
    var tickSame by remember { mutableStateOf(a.store.tickSame) }
    var downWifi by remember { mutableStateOf(a.store.downloadWifiOnly) }
    var upWifi by remember { mutableStateOf(a.store.uploadWifiOnly) }
    var token by remember { mutableStateOf(a.store.token) }
    var code by remember { mutableStateOf("") }
    var said by remember { mutableStateOf<Pair<String, Boolean>?>(null) }
    var other by remember { mutableStateOf<Pair<String?, String>?>(null) }     // a code for another device, and what to do with it
    var advanced by remember { mutableStateOf(false) }
    var guide by remember { mutableStateOf<CaptureGuide.Kind?>(null) }
    var hidden by remember { mutableStateOf(a.store.guidesHidden) }
    val update by Updates.state.collectAsState()
    val scope = rememberCoroutineScope()
    Page("Settings", back = { go(Screen.Home) }) {
        Section("This app")
        Text("boulderme.sh ${BuildInfo.VERSION}, build ${BuildInfo.BUILD}, commit ${BuildInfo.COMMIT}")
        val newer = update.newer
        when {
            newer != null -> {
                Text("A newer build is ready: build ${newer.build}, commit ${newer.commit}.")
                Button(enabled = update.busy == null, onClick = { Updates.state.value = update.copy(busy = "Starting…", error = null); Updates.install(a, newer) }) {
                    Text(update.busy ?: "Update")
                }
                update.error?.let { Text(it, color = BAD) }
            }
            update.looking -> Text("Looking for a newer build…", color = MaterialTheme.colorScheme.onSurfaceVariant)
            update.failed != null -> Text("Couldn't look for a newer build: ${update.failed}", color = BAD)
            update.checked != null -> Text("This is the newest build.", color = GOOD)
        }
        if (newer == null) OutlinedButton(enabled = !update.looking, onClick = { Updates.look() }) { Text("Check for updates") }
        Section(CaptureGuide.TITLE)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            for (k in CaptureGuide.Kind.entries.take(2)) OutlinedButton(onClick = { guide = k }) { Text(k.title) }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            for (k in CaptureGuide.Kind.entries.drop(2)) OutlinedButton(onClick = { guide = k }) { Text(k.title) }
        }
        OutlinedButton(onClick = { a.store.guidesHidden = ""; a.store.compassAsked = false; hidden = "" }) { Text("Show tips again") }
        Text(if (hidden.isEmpty() && !a.store.compassAsked) "Each capture starts with how its kind goes, until you tick Don't show again there." else
            "Each kind of capture you ticked Don't show again for starts with how it goes again, and the compass is asked about again.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Section("Sounds while capturing")
        TextButton(onClick = { dingKept = !dingKept; a.store.dingKept = dingKept }) { Text(if (dingKept) "A bell when a photo is kept: on" else "A bell when a photo is kept: off") }
        TextButton(onClick = { tickSame = !tickSame; a.store.tickSame = tickSame }) {
            Text(if (tickSame) "Ticking while there's nothing new to photograph from where the phone is: on" else "Ticking while there's nothing new to photograph from where the phone is: off")
        }
        Text("For a phone out of sight, up on a pole with Auto on: the ticking stops when the phone is somewhere new, and the bell says the photo from there was kept.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Section("Wi-Fi or mobile data")
        TextButton(onClick = { downWifi = !downWifi; a.store.downloadWifiOnly = downWifi }) { Text(if (downWifi) "Getting meshes: only over Wi-Fi" else "Getting meshes: over Wi-Fi or mobile data") }
        TextButton(onClick = { upWifi = !upWifi; a.store.uploadWifiOnly = upWifi }) { Text(if (upWifi) "Sending captures: only over Wi-Fi" else "Sending captures: over Wi-Fi or mobile data") }
        Text("Only over Wi-Fi waits for one (not a phone's hotspot), and carries on by itself once there's one, while the app is open.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Section("Uploading")
        OutlinedTextField(credit, { credit = it.take(80); a.store.credit = credit }, label = { Text("Credit my photos to") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Text("Photos you send are published under Creative Commons Attribution 4.0 (CC BY 4.0): the site and anyone else may use them, crediting this name.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        TextButton(onClick = { anonFirst = !anonFirst; a.store.anonFirst = anonFirst }) {
            Text(if (anonFirst) "Holds others marked privately: asked for from the start" else "Holds others marked privately: asked about at the end")
        }
        Section("Signing in")
        Text("Sending captures needs your account. Where you're signed in to the site (a computer, or this phone's browser), press your " +
            "name at the top, then Sign in another device: Scan the QR it shows, or type its code below. An invite link can go below too.")
        fun signIn(text: String) = scope.launch {
            said = try {
                val name = withContext(Dispatchers.IO) { a.api.signIn(text) }
                token = a.store.token; credit = a.store.credit; code = ""
                (if (name != null) "Signed in as $name: this phone can upload now." else "Paired: this phone can upload now.") to true
            } catch (e: Exception) { "Signing in failed: ${e.message}" to false }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Button(onClick = { scanQr(a) { text, why -> if (text != null) signIn(text) else if (why != null) said = why to false } }) { Text("Scan") }
            Text("a QR from the site", style = MaterialTheme.typography.bodySmall)
        }
        OutlinedTextField(code, { code = it }, label = { Text("Code from another device, or an invite link") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        if ("invite=" in code) {    // a new account, made under this name: the credit above, here as well
            OutlinedTextField(credit, { credit = it.take(80); a.store.credit = credit }, label = { Text("Your name, for the new account") },
                singleLine = true, modifier = Modifier.fillMaxWidth())
            Text("Already joined, on the site or another phone? Then don't use the invite: sign in with a code from there, as above.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Button(enabled = code.isNotBlank() && ("invite=" !in code || credit.isNotBlank()), onClick = { signIn(code) }) {
            Text(if ("invite=" in code) "Join" else "Sign in")
        }
        said?.let { Text(it.first, color = if (it.second) GOOD else BAD) }
        OutlinedTextField(token, { token = it; a.store.token = it }, label = { Text("Token (set by signing in)") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password))
        if (token.isNotEmpty()) {
            Section("Your account elsewhere")
            Text("To sign in to your account on a computer or another phone, without a new invite:")
            OutlinedButton(onClick = {
                scope.launch {
                    other = try {
                        withContext(Dispatchers.IO) { a.api.pairCode() } to "On the other device, open ${a.store.server.removePrefix("https://")}, " +
                            "press Edit…, then Sign in with a code, and enter this. It works once, for 10 minutes."
                    } catch (e: Exception) { null to "No code: ${e.message}" }
                }
            }) { Text(if (other?.first != null) "A new code" else "Sign in another device") }
            other?.let { (code, words) ->
                Text(words, color = if (code == null) BAD else MaterialTheme.colorScheme.onSurface)
                code?.let { SelectionContainer { Text(it, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold) } }
            }
        }
        TextButton(onClick = { advanced = !advanced }) { Text(if (advanced) "▾ Advanced" else "▸ Advanced") }
        if (advanced) {
            OutlinedTextField(server, { server = it; a.store.server = it.ifBlank { Store.DEFAULT_SERVER } }, label = { Text("Server") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri))
            Text("Where captures are sent and meshes come from. Only change it to try a test server.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Section("Credits")
        Text(sh.boulderme.core.HoldModel.CREDITS_NOTE, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        for ((name, by, licence, url) in sh.boulderme.core.HoldModel.CREDITS) Text(buildAnnotatedString {
            withLink(LinkAnnotation.Url(url, TextLinkStyles(SpanStyle(color = MaterialTheme.colorScheme.primary, textDecoration = TextDecoration.Underline)))) { append(name) }
            append(" by $by ($licence)")
        }, style = MaterialTheme.typography.bodySmall)
    }
    guide?.let { k -> GuideDialog(k, "OK", starting = false, dismiss = { guide = null }) { guide = null } }
}

// ---------------------------------------------------------------------------------------------------------- calibrate
private fun beep() = runCatching { ToneGenerator(AudioManager.STREAM_NOTIFICATION, 90).startTone(ToneGenerator.TONE_PROP_BEEP, 200) }

/**
 * What's worth doing before a capture: the compass, which reads true only after being turned every which way. Which
 * way is down is found at the start of each capture (a turn on the spot), and GPS needs nothing done but time under
 * open sky, which a capture gives it.
 */
@Composable
fun CalibrateScreen(a: MainActivity, go: (Screen) -> Unit) {
    val scope = rememberCoroutineScope()
    // compass
    var mag by remember { mutableStateOf<MagCalibration?>(null) }
    var covered by remember { mutableStateOf(0.0) }
    var magSaid by remember { mutableStateOf(a.store.magNote) }
    var magOk by remember { mutableStateOf(true) }
    var fixes by remember { mutableIntStateOf(0) }
    DisposableEffect(Unit) { onDispose { a.sensors.onMag = null } }
    LaunchedEffect(Unit) { while (true) { fixes = a.sensors.place.count; delay(1000) } }

    Page("Calibrate", back = { go(Screen.Home) }) {
        Section("Compass · half a minute")
        Text("Away from cars, bolts and crash pads' buckles: turn the phone every which way, a slow figure of 8 that also rolls it over, until the bar fills.")
        val m = mag
        if (m == null) Button(onClick = {
            val c = MagCalibration(); mag = c; covered = 0.0; magSaid = ""
            a.sensors.onMag = { v: Vec3 -> c.add(v) }
            scope.launch {
                var waited = 0
                while (waited < 60 && c.covered() < 0.75) { delay(500); waited++; covered = c.covered() }
                a.sensors.onMag = null
                val r = c.solve()
                val accuracy = a.sensors.magAccuracy ?: 0
                if (r == null || r.covered < 0.5) { magSaid = "Not turned enough ways to tell: try again."; magOk = false }
                else {
                    val earth = r.fieldUt in 22.0..67.0
                    magOk = earth && r.rmsUt < 0.12 * r.fieldUt && accuracy >= 2
                    magSaid = "Field %.0f µT, uneven by %.1f µT; the phone rates its compass %s. ".format(r.fieldUt, r.rmsUt, listOf("unreliable", "low", "medium", "high")[accuracy.coerceIn(0, 3)]) +
                        if (magOk) "Good." else if (!earth) "That isn't the Earth's field alone: move away from metal and try again." else "Try again, more slowly."
                    if (magOk) a.store.magNote = magSaid
                }
                beep(); mag = null
            }
        }) { Text("Start") } else LinearProgressIndicator(progress = { (covered / 0.75).toFloat().coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth())
        if (magSaid.isNotEmpty()) Text(magSaid, color = if (magOk) GOOD else BAD)

        Section("GPS")
        val place = a.sensors.place.result()
        Text(if (!a.has(Manifest.permission.ACCESS_FINE_LOCATION)) "Location isn't allowed for this app yet: starting a capture asks for it."
            else if (place == null) "No fix yet. Under open sky it takes up to a minute."
            else "$fixes fixes so far, good to about %.1f m together. Nothing to do: a capture averages its fixes, and each photo carries its own.".format(place.accM))

        Section("Which way is down")
        Text("Nothing to do here: each capture starts by asking for one slow turn on the spot, which finds it to a fraction of a degree whatever this phone's sensors are off by.")
    }
}

// --------------------------------------------------------------------------------------------------------------- done
@Composable
fun DoneScreen(a: MainActivity, id: String, go: (Screen) -> Unit) {
    val sending by Uploader.state.collectAsState()
    val u = remember(sending) { a.store.upload(id) }
    Page("Captured") {
        if (u == null) { Text("This capture is gone."); Button(onClick = { go(Screen.Home) }) { Text("Home") }; return@Page }
        Text(u.name, fontWeight = FontWeight.SemiBold)
        Text("${u.photos} photos kept, each with which way was down, where the phone was and which way it faced.")
        if (u.photos < 3) Text("That's too few to mesh.", color = BAD)
        val scenery = (java.io.File(a.store.captureDir(id), "scenery").listFiles() ?: emptyArray()).filter { it.name.endsWith(".jpg") }
        if (scenery.isNotEmpty()) Text("And ${scenery.size} frames of the scenery round the rock (${scenery.sumOf { it.length() } / 1_000_000} MB), sent last: Wi-Fi is best for them.")
        val status = sending[id]
        if (status != null) Text(status, color = if (u.done) GOOD else if (status.startsWith("Stopped")) BAD else if (status == Uploader.WAITING) WAIT else Color.Unspecified)
        if (Uploader.busy(id)) LinearProgressIndicator(progress = { if (u.photos > 0) u.sent.size.toFloat() / u.photos else 0f }, modifier = Modifier.fillMaxWidth())
        if (!u.done) Button(enabled = !Uploader.busy(id) && u.photos >= 3, onClick = { Uploader.start(a.store, a.api, id) }) { Text(if (u.sent.isEmpty()) "Upload now" else "Carry on uploading") }
        Text("Uploading publishes them under CC BY 4.0, credited to ${a.store.credit.ifBlank { "nobody by name (set one in Settings)" }}.", style = MaterialTheme.typography.bodySmall)
        if (a.store.token.isEmpty()) Text("Uploading needs the site's editing token: see Settings.", color = WARN)
        ReopenButton(a, id, u, go)
        OutlinedButton(onClick = { go(Screen.Upload(id)) }) { Text("What's in it") }
        OutlinedButton(onClick = { go(Screen.Home) }) { Text(if (u.done) "Home" else "Later: it stays on the phone") }
    }
}

/**
 * Adding to a capture finished but not sent, while still at the rock: scenery, a panorama or photos missed (Reopen).
 * Nothing once any of it has been sent; why not, if it can't be.
 */
@Composable
fun ReopenButton(a: MainActivity, id: String, u: Store.Upload, go: (Screen) -> Unit) {
    val sent = u.done || u.meshAsked || u.recordSent || u.pointsSent || u.logSent || u.sent.isNotEmpty() || u.scenerySent.isNotEmpty()
    if (sent || Uploader.busy(id)) return
    val record = remember(id) { Reopen.record(runCatching { java.io.File(a.store.captureDir(id), "capture.json").readText() }.getOrNull()) }
    val why = Reopen.cannot(record, u.mesh?.let { a.store.plan(it)?.key }, sent)
    if (why != null) { Text(why, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant); return }
    OutlinedButton(onClick = { go(Screen.Capture(u.mesh, u.name, record?.detail?.set, reopen = id)) }) { Text("Add more: scenery, a panorama or photos") }
    val here = a.sensors.place.result()
    Reopen.far(record, here?.lat, here?.lng)?.let { Text(it, color = WARN, style = MaterialTheme.typography.bodySmall) }
}

/**
 * Reads a QR with Google's code scanner (Play services' own camera screen: no camera permission or screen of the app's),
 * giving its text, or null and why not (null for both if it was cancelled).
 */
fun scanQr(a: MainActivity, done: (String?, String?) -> Unit) {
    val options = com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions.Builder()
        .setBarcodeFormats(com.google.mlkit.vision.barcode.common.Barcode.FORMAT_QR_CODE).build()
    com.google.mlkit.vision.codescanner.GmsBarcodeScanning.getClient(a, options).startScan()
        .addOnSuccessListener { done(it.rawValue, if (it.rawValue == null) "That QR has no text in it." else null) }
        .addOnCanceledListener { done(null, null) }
        .addOnFailureListener { done(null, "The scanner couldn't start (${it.message}): type the code instead.") }
}
