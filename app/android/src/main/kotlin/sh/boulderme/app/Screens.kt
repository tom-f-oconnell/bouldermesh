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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import sh.boulderme.core.AccelCalibration
import sh.boulderme.core.GpsAverager
import sh.boulderme.core.MagCalibration
import sh.boulderme.core.MeshRow
import sh.boulderme.core.Plan
import sh.boulderme.core.StillMean
import sh.boulderme.core.Vec3

val GOOD = Color(0xFF58C27D)
val WARN = Color(0xFFF0A030)
val BAD = Color(0xFFE5564B)

@Composable
private fun Page(title: String, back: (() -> Unit)? = null, content: @Composable () -> Unit) {
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (back != null) TextButton(onClick = back) { Text("‹ Back") }
            Text(title, style = MaterialTheme.typography.headlineSmall, modifier = Modifier.padding(vertical = 12.dp))
        }
        content()
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun Section(text: String) = Text(text.uppercase(), style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
    modifier = Modifier.padding(top = 10.dp))

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
    val start: (Screen.Capture) -> Unit = { c ->
        if (a.has(Manifest.permission.CAMERA) && a.has(Manifest.permission.ACCESS_FINE_LOCATION)) go(c)
        else { wanted = c; ask.launch(arrayOf(Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)) }
    }
    LaunchedEffect(sending) { tick++ }

    Page("BoulderMesh") {
        Text("Photograph a boulder so that its mesh comes out whole, level, the right size and in the right place. " +
            "The app says where to stand and takes each photo when it's worth taking.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (refused) Text("Capturing needs the camera: allow it in the phone's settings for this app.", color = BAD)
        if (a.store.accelBias == null) Text("Not calibrated yet: a minute in Calibrate makes which-way-is-up more exact.", color = WARN)

        Section("Add to a mesh")
        if (plans.isEmpty()) Text("None downloaded yet. Get the meshes you'll visit while you have signal: they work with none afterwards.")
        for (p in plans) Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(p.name.ifEmpty { p.mesh }, fontWeight = FontWeight.SemiBold)
                Text(summary(p), style = MaterialTheme.typography.bodySmall)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = { start(Screen.Capture(p.mesh, p.name.ifEmpty { p.mesh })) }) { Text("Capture") }
                    TextButton(onClick = { a.store.removePlan(p.mesh); tick++ }) { Text("Remove") }
                }
            }
        }
        OutlinedButton(onClick = { go(Screen.Browse()) }, modifier = Modifier.fillMaxWidth()) { Text("Get meshes…") }

        Section("A rock with no mesh")
        OutlinedButton(onClick = { naming = true }, modifier = Modifier.fillMaxWidth()) { Text("Capture a new rock…") }

        if (captures.isNotEmpty()) Section("Captures on this phone")
        for ((id, u) in captures) Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text(u.name, fontWeight = FontWeight.SemiBold)
                val status = sending[id]?.takeIf { Uploader.busy(id) } ?: when {
                    u.done -> "Sent: the server is meshing it, or has"
                    u.note.isNotEmpty() -> "Stopped: ${u.note}"
                    u.sent.isNotEmpty() -> "${u.sent.size} of ${u.photos} photos sent"
                    else -> "${u.photos} photos, not sent yet"
                }
                Text(status, style = MaterialTheme.typography.bodySmall, color = if (u.done) GOOD else if (u.note.isNotEmpty() && !Uploader.busy(id)) BAD else Color.Unspecified)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (!u.done) Button(enabled = !Uploader.busy(id), onClick = { Uploader.start(a.store, a.api, id) }) { Text(if (u.sent.isEmpty()) "Upload" else "Carry on") }
                    TextButton(enabled = !Uploader.busy(id), onClick = { removing = id }) { Text("Delete") }
                }
            }
        }

        Section("This phone")
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { go(Screen.Calibrate) }) { Text("Calibrate") }
            OutlinedButton(onClick = { go(Screen.Settings) }) { Text("Settings") }
        }
    }

    if (naming) {
        var name by remember { mutableStateOf("") }
        AlertDialog(onDismissRequest = { naming = false }, title = { Text("What is it?") },
            text = { OutlinedTextField(name, { name = it.take(60) }, label = { Text("The rock's or climb's name") }, singleLine = true) },
            confirmButton = { TextButton(enabled = name.isNotBlank(), onClick = { naming = false; start(Screen.Capture(null, name.trim())) }) { Text("Start") } },
            dismissButton = { TextButton(onClick = { naming = false }) { Text("Cancel") } })
    }
    removing?.let { id ->
        val u = a.store.upload(id)
        AlertDialog(onDismissRequest = { removing = null }, title = { Text("Delete this capture?") },
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
    val scope = rememberCoroutineScope()
    LaunchedEffect(Unit) {
        try { rows = withContext(Dispatchers.IO) { a.api.meshes() } } catch (e: Exception) { error = "Couldn't list the meshes: ${e.message}" }
    }
    val here = a.sensors.fix
    fun metres(r: MeshRow): Double? { val lat = r.lat; val lng = r.lng; return if (here != null && lat != null && lng != null) GpsAverager.metresApart(here.lat, here.lng, lat, lng) else null }
    val shown = (rows ?: emptyList()).filter { r -> search.isBlank() || r.name.contains(search, true) || r.climbs.any { it.contains(search, true) } }
        .sortedWith(compareBy<MeshRow> { metres(it) ?: Double.MAX_VALUE }.thenBy { it.name.lowercase() })
    fun get(list: List<MeshRow>) {     // one after another: the server makes each mesh's plan as it's first asked for
        busy = busy + list.map { it.id }
        scope.launch {
            for (r in list) {
                try {
                    val (plan, guide) = withContext(Dispatchers.IO) { a.api.plan(r.id) }
                    a.store.savePlan(r.id, plan, guide)
                    have = have + r.id
                } catch (e: Exception) { error = "${r.name}: ${e.message}" }
                busy = busy - r.id
            }
        }
    }
    var gotFirst by remember { mutableStateOf(!getFirst) }
    LaunchedEffect(rows) { val first = rows?.firstOrNull(); if (!gotFirst && first != null) { gotFirst = true; get(listOf(first)) } }

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
            if (near.size > 1 && search.isBlank()) OutlinedButton(onClick = { get(near.take(40)) }) { Text("Get the ${minOf(near.size, 40)} within 2 km") }
            Text(if (here == null) "No GPS fix yet: listed by name." else "Nearest first.", style = MaterialTheme.typography.bodySmall)
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.weight(1f)) {
            items(shown, key = { it.id }) { r ->
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(r.name.ifEmpty { r.id })
                        val far = metres(r)?.let { if (it < 950) "${(it / 10).toInt() * 10} m" else "%.1f km".format(it / 1000) }
                        Text(listOfNotNull(far, if (r.holds > 0) "${r.holds} holds marked" else null, r.climbs.take(3).joinToString(", ").ifEmpty { null }).joinToString(" · "),
                            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    when (r.id) {
                        in busy -> Text("Getting…")
                        in have -> Text("On phone", color = GOOD)
                        else -> TextButton(onClick = { get(listOf(r)) }) { Text("Get") }
                    }
                }
            }
        }
    }
}

// ----------------------------------------------------------------------------------------------------------- settings
@Composable
fun SettingsScreen(a: MainActivity, go: (Screen) -> Unit) {
    var server by remember { mutableStateOf(a.store.server) }
    var token by remember { mutableStateOf(a.store.token) }
    var code by remember { mutableStateOf("") }
    var said by remember { mutableStateOf<Pair<String, Boolean>?>(null) }
    val scope = rememberCoroutineScope()
    Page("Settings", back = { go(Screen.Home) }) {
        OutlinedTextField(server, { server = it; a.store.server = it.ifBlank { Store.DEFAULT_SERVER } }, label = { Text("Server") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri))
        Section("Uploading")
        Text("Sending captures needs the site's editing token. Paste it, or paste the link or code from the admin page's \"Pair a phone\" QR, which gives it once.")
        OutlinedTextField(token, { token = it; a.store.token = it }, label = { Text("Editing token") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password))
        OutlinedTextField(code, { code = it }, label = { Text("Pairing link or code") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Button(enabled = code.isNotBlank(), onClick = {
            scope.launch {
                said = try {
                    token = withContext(Dispatchers.IO) { a.api.pair(code) }
                    a.store.token = token; code = ""
                    "Paired: this phone can upload now." to true
                } catch (e: Exception) { "Pairing failed: ${e.message}" to false }
            }
        }) { Text("Pair") }
        said?.let { Text(it.first, color = if (it.second) GOOD else BAD) }
        Text("BoulderMesh ${BuildInfo.VERSION}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

// ---------------------------------------------------------------------------------------------------------- calibrate
private fun beep() = runCatching { ToneGenerator(AudioManager.STREAM_NOTIFICATION, 90).startTone(ToneGenerator.TONE_PROP_BEEP, 200) }

/**
 * Two things worth a minute each, once for a phone: its accelerometer's offsets, which are most of what "down" is off
 * by in a photo taken while the phone isn't tracking; and its compass, which reads true only after being turned every
 * which way. GPS needs nothing done but time under open sky, which a capture gives it.
 */
@Composable
fun CalibrateScreen(a: MainActivity, go: (Screen) -> Unit) {
    val scope = rememberCoroutineScope()
    // level
    val accel = remember { AccelCalibration() }
    var step by remember { mutableIntStateOf(0) }           // 0 flat, 1 turned, 2 flipped, 3 done
    var count by remember { mutableIntStateOf(0) }          // seconds left before it reads; 0 not waiting
    var levelSaid by remember { mutableStateOf(a.store.accelBias?.let { "Calibrated before: offsets %.2f, %.2f, %.2f m/s².".format(it.x, it.y, it.z) } ?: "") }
    var levelOk by remember { mutableStateOf(true) }
    // compass
    var mag by remember { mutableStateOf<MagCalibration?>(null) }
    var covered by remember { mutableStateOf(0.0) }
    var magSaid by remember { mutableStateOf(a.store.magNote) }
    var magOk by remember { mutableStateOf(true) }
    var fixes by remember { mutableIntStateOf(0) }
    DisposableEffect(Unit) { onDispose { a.sensors.onAccel = null; a.sensors.onMag = null } }
    LaunchedEffect(Unit) { while (true) { fixes = a.sensors.place.count; delay(1000) } }

    fun read() {
        scope.launch {
            for (k in 3 downTo 1) { count = k; delay(1000) }
            count = 0
            val mean = StillMean()
            a.sensors.onAccel = { v: Vec3 -> mean.add(v) }
            delay(1500)
            a.sensors.onAccel = null
            val v = mean.mean()
            if (v == null || !mean.still()) { levelSaid = "The phone moved while it was read: try that one again."; levelOk = false; beep(); delay(250); beep(); return@launch }
            when (step) { 0 -> accel.flat = v; 1 -> accel.turned = v; else -> accel.flipped = v }
            beep()
            if (step < 2) { step++; levelSaid = ""; levelOk = true; return@launch }
            val bias = accel.bias()
            if (bias == null) {
                levelSaid = "Those don't fit together (was it flat, then turned half round, then face down?). Start again."; levelOk = false
            } else {
                a.store.accelBias = bias
                levelSaid = "Done: offsets %.2f, %.2f, %.2f m/s², worth %.1f° of level.".format(bias.x, bias.y, bias.z, accel.gainDeg() ?: 0.0); levelOk = true
            }
            accel.flat = null; accel.turned = null; accel.flipped = null
            step = 0
        }
    }

    Page("Calibrate", back = { go(Screen.Home) }) {
        Section("Level · about a minute")
        Text("Three readings with the phone lying still on something steady, like a table or a flat rock. It needn't be level. " +
            "Each starts 3 seconds after you press, and beeps when it's read.")
        Text(when (step) { 0 -> "1 of 3: lay it flat on its back, screen up."; 1 -> "2 of 3: turn it half a turn on the spot, still screen up."; else -> "3 of 3: flip it over, screen down." },
            fontWeight = FontWeight.SemiBold)
        Button(enabled = count == 0, onClick = { read() }) { Text(if (count > 0) "Reading in $count…" else "Read") }
        if (levelSaid.isNotEmpty()) Text(levelSaid, color = if (levelOk) GOOD else BAD)

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
        val status = sending[id]
        if (status != null) Text(status, color = if (u.done) GOOD else if (status.startsWith("Stopped")) BAD else Color.Unspecified)
        if (Uploader.busy(id)) LinearProgressIndicator(progress = { if (u.photos > 0) u.sent.size.toFloat() / u.photos else 0f }, modifier = Modifier.fillMaxWidth())
        if (!u.done) Button(enabled = !Uploader.busy(id) && u.photos >= 3, onClick = { Uploader.start(a.store, a.api, id) }) { Text(if (u.sent.isEmpty()) "Upload now" else "Carry on uploading") }
        if (a.store.token.isEmpty()) Text("Uploading needs the site's editing token: see Settings.", color = WARN)
        OutlinedButton(onClick = { go(Screen.Home) }) { Text(if (u.done) "Home" else "Later: it stays on the phone") }
    }
}
