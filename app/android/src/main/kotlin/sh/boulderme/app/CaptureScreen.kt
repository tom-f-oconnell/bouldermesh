package sh.boulderme.app

import android.annotation.SuppressLint
import android.opengl.GLSurfaceView
import android.view.MotionEvent
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import sh.boulderme.core.CaptureUi
import sh.boulderme.core.LevelUi
import sh.boulderme.core.Phase
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.sin

private val SHADE = Color(0xB0000000)

/**
 * How the turn on the spot is going: a ring of the directions round you, the way you face at the top (read ones green,
 * ones to face again orange); how still the phone is, against what it should stay under; and how well the directions
 * read so far agree.
 */
@Composable
private fun LevelGauges(l: LevelUi) {
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
        Canvas(Modifier.size(64.dp)) {
            val n = l.faced.size
            val step = 360f / n
            val w = size.width * 0.14f
            val inset = w / 2
            for (i in 0 until n) {
                // anticlockwise as seen from above is a turn to the left: on the ring, to the left of the top
                val mid = -90f - (i - l.facing.coerceAtLeast(0)) * step
                val color = when (l.faced[i]) { 2 -> GOOD; 3 -> WARN; 1 -> Color.White.copy(alpha = 0.6f); else -> Color.White.copy(alpha = 0.2f) }
                drawArc(color, mid - step / 2 + 2f, step - 4f, false, topLeft = Offset(inset, inset),
                    size = androidx.compose.ui.geometry.Size(size.width - w, size.height - w), style = Stroke(w))
            }
            // you, facing the top
            val c = Offset(size.width / 2, size.height / 2)
            drawPath(Path().apply { moveTo(c.x, c.y - size.height * 0.22f); lineTo(c.x - size.width * 0.1f, c.y + size.height * 0.08f)
                lineTo(c.x + size.width * 0.1f, c.y + size.height * 0.08f); close() }, Color.White)
        }
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
            // how still: the bar runs to three times what's acceptable, with a mark at it
            val top = l.wobbleOk * 3
            Canvas(Modifier.fillMaxWidth().height(8.dp)) {
                drawRect(Color.White.copy(alpha = 0.2f))
                val k = (l.wobbleNow / top).coerceIn(0.0, 1.0).toFloat()
                val color = if (l.tooFast || l.wobbleNow > 2 * l.wobbleOk) BAD else if (l.wobbleNow > l.wobbleOk) WARN else GOOD
                drawRect(color, size = androidx.compose.ui.geometry.Size(size.width * k, size.height))
                val avg = (l.wobbleTurn / top).coerceIn(0.0, 1.0).toFloat() * size.width
                drawLine(Color.White.copy(alpha = 0.7f), Offset(avg, 0f), Offset(avg, size.height), 2f)
                val ok = size.width / 3
                drawLine(Color.White, Offset(ok, -2f), Offset(ok, size.height + 2f), 3f)
            }
            Text(l.steadyText(), color = if (l.tooFast) WARN else Color.White, style = MaterialTheme.typography.bodySmall)
            Text(l.agreeText(), color = l.agreeDeg?.let { if (it <= l.agreeOk) GOOD else WARN } ?: Color.White, style = MaterialTheme.typography.bodySmall)
            l.redoText()?.let { Text(it, color = WARN, style = MaterialTheme.typography.bodySmall) }
        }
    }
}

/** The camera with the mesh and what's left to photograph drawn over it, the hint, and the few buttons a capture has. */
@OptIn(ExperimentalLayoutApi::class)
@SuppressLint("ClickableViewAccessibility")
@Composable
fun CaptureScreen(a: MainActivity, what: Screen.Capture, go: (Screen) -> Unit) {
    var ui by remember { mutableStateOf(CaptureUi()) }
    var failed by remember { mutableStateOf<String?>(null) }
    var leaving by remember { mutableStateOf(false) }
    val plan = remember { what.mesh?.let { a.store.plan(it) } }
    val guide = remember { what.mesh?.let { a.store.guide(it) } }
    if (what.mesh != null && (plan == null || guide == null)) {
        Column(Modifier.fillMaxSize().safeDrawingPadding().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("This mesh's download can't be read. Remove it and get it again.", color = BAD)
            Button(onClick = { go(Screen.Home) }) { Text("Back") }
        }
        return
    }
    val controller = remember { ArController(a, a.store, a.sensors, plan, guide, what.name + " (capture)", what.set) { ui = it } }
    val view = remember {
        GLSurfaceView(a).apply {
            preserveEGLContextOnPause = true
            setEGLContextClientVersion(2)
            setEGLConfigChooser(8, 8, 8, 8, 16, 0)
            setRenderer(controller)
            renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY
            keepScreenOn = true
            setOnTouchListener { _, e -> if (e.action == MotionEvent.ACTION_UP) controller.tap(e.x, e.y); true }
        }
    }
    DisposableEffect(Unit) {
        val watch = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_RESUME -> { failed = controller.resume(); if (failed == null) view.onResume() }
                Lifecycle.Event.ON_PAUSE -> { view.onPause(); controller.pause() }
                else -> {}
            }
        }
        a.lifecycle.addObserver(watch)      // which brings it up to the activity's state: resumed, so it starts now
        onDispose { a.lifecycle.removeObserver(watch); view.onPause(); controller.pause(); controller.close() }
    }
    BackHandler { leaving = true }

    Box(Modifier.fillMaxSize()) {
        AndroidView(factory = { view }, modifier = Modifier.fillMaxSize())
        // which way to turn, when what's wanted is out of the picture
        if ((ui.phase == Phase.CAPTURE || ui.phase == Phase.SCENERY) && hypot(ui.turnX, ui.turnY) > 0.3) Canvas(Modifier.align(Alignment.Center).size(120.dp)) {
            val angle = atan2(-ui.turnY, ui.turnX).toFloat()        // the screen's y runs down
            val c = Offset(size.width / 2, size.height / 2); val r = size.width * 0.45f
            fun at(da: Float, k: Float) = Offset(c.x + r * k * cos(angle + da), c.y + r * k * sin(angle + da))
            drawPath(Path().apply { val tip = at(0f, 1f); val l = at(2.5f, 0.6f); val rr = at(-2.5f, 0.6f); moveTo(tip.x, tip.y); lineTo(l.x, l.y); lineTo(rr.x, rr.y); close() }, Color.White.copy(alpha = 0.9f))
        }
        Column(Modifier.fillMaxSize().safeDrawingPadding().padding(10.dp), verticalArrangement = Arrangement.SpaceBetween) {
            Column(Modifier.fillMaxWidth().background(SHADE, RoundedCornerShape(10.dp)).padding(10.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(failed ?: ui.text, color = if (failed != null) BAD else Color.White, fontWeight = FontWeight.SemiBold, style = MaterialTheme.typography.titleMedium)
                if (ui.target.isNotEmpty()) Text(ui.target, color = Color.White, style = MaterialTheme.typography.bodySmall)
                if (ui.note.isNotEmpty()) Text(ui.note, color = if (ui.note.startsWith("Not kept")) WARN else Color.White, style = MaterialTheme.typography.bodySmall)
                if (ui.phase == Phase.LEVEL && failed == null && ui.tracking) {
                    LinearProgressIndicator(progress = { ui.done.toFloat() / ui.total.coerceAtLeast(1) }, modifier = Modifier.fillMaxWidth())
                    Text("${ui.done} of ${ui.total} directions faced", color = Color.White, style = MaterialTheme.typography.bodySmall)
                    ui.level?.let { LevelGauges(it) }
                }
                if (ui.phase == Phase.CAPTURE && ui.total > 0) {
                    LinearProgressIndicator(progress = { ui.done.toFloat() / ui.total }, modifier = Modifier.fillMaxWidth())
                    Text(listOfNotNull("${ui.done}/${ui.total} ${if (ui.guided) "parts" else "sides and spots"}", if (ui.holds > 0 && ui.guided) "${ui.holdsDone}/${ui.holds} holds" else null,
                        "${ui.kept} photos", if (ui.rejected > 0) "${ui.rejected} not kept" else null).joinToString(" · "), color = Color.White, style = MaterialTheme.typography.bodySmall)
                }
                if (ui.phase == Phase.SCENERY) {
                    LinearProgressIndicator(progress = { ui.done.toFloat() / ui.total.coerceAtLeast(1) }, modifier = Modifier.fillMaxWidth())
                    Text("${ui.done}/${ui.total} directions round the rock · ${ui.scenery} frames of the scenery (a backdrop: they needn't be perfect)",
                        color = Color.White, style = MaterialTheme.typography.bodySmall)
                }
                // what the last photo's readings lacked, which it was kept in spite of
                if (ui.phase == Phase.CAPTURE && ui.sensors.isNotEmpty()) Text(ui.sensors, color = WARN, style = MaterialTheme.typography.bodySmall)
            }
            FlowRow(Modifier.fillMaxWidth().background(SHADE, RoundedCornerShape(10.dp)).padding(8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.CenterHorizontally)) {
                when (ui.phase) {
                    Phase.LEVEL -> {
                        TextButton(onClick = { controller.skipLevel() }) { Text("Skip: trust the phone's own") }
                        TextButton(onClick = { leaving = true }) { Text("Leave") }
                    }
                    Phase.GHOST, Phase.ROCK -> {
                        FilledTonalButton(onClick = { controller.turnGhost(45.0) }) { Text("Turn it") }
                        FilledTonalButton(onClick = { controller.bringGhost() }) { Text("Bring it here") }
                        if (ui.pairs > 0 || ui.phase == Phase.ROCK) TextButton(onClick = { controller.restartAlign() }) { Text("Start again") }
                    }
                    Phase.CONFIRM -> {
                        Button(onClick = { controller.confirmFit() }) { Text("It fits: start") }
                        FilledTonalButton(onClick = { controller.addSpot() }) { Text("Add a spot") }
                        TextButton(onClick = { controller.restartAlign() }) { Text("Start again") }
                    }
                    Phase.CAPTURE -> {
                        if (ui.askMore > 0) {
                            Button(onClick = { controller.more(true) }) { Text("Photograph ${if (ui.askMore == 1) "it" else "them"} too") }
                            FilledTonalButton(onClick = { controller.more(false) }) { Text("No") }
                        }
                        Button(onClick = { controller.shutter() }) { Text(if (ui.waiting) "Hold still…" else "Take") }
                        FilledTonalButton(onClick = { controller.setAuto(!ui.auto) }) { Text(if (ui.auto) "Auto: on" else "Auto: off") }
                        if (plan != null) {
                            if (ui.canSkip) FilledTonalButton(onClick = { controller.skip() }) { Text("Skip this") }
                            FilledTonalButton(onClick = { controller.toggleWide() }) { Text("Doing: ${ui.reach}") }
                            if (ui.canCard || ui.card) FilledTonalButton(onClick = { controller.toggleCard() }) { Text(if (ui.card) "No card" else "Card photo") }
                        }
                        FilledTonalButton(onClick = { controller.toggleMarking() }) { Text(if (ui.marking) "Tap the hold…" else "Mark a hold") }
                        if (plan == null || ui.ring > 0) FilledTonalButton(onClick = { controller.blockSide() }) { Text("Can't reach that side") }
                        FilledTonalButton(onClick = { controller.newRing() }) { Text(if (ui.ring > 1) "Another ring (on ${ui.ring})" else "Another ring") }
                        FilledTonalButton(onClick = { controller.startScenery() }) { Text(if (ui.scenery > 0) "Scenery (${ui.scenery})" else "Scenery") }
                        Button(onClick = { leaving = true }, colors = ButtonDefaults.buttonColors(containerColor = GOOD)) { Text("Finish") }
                    }
                    Phase.SCENERY -> {
                        FilledTonalButton(onClick = { controller.endScenery() }) { Text("Back to the rock") }
                        Button(onClick = { leaving = true }, colors = ButtonDefaults.buttonColors(containerColor = GOOD)) { Text("Finish") }
                    }
                    else -> TextButton(onClick = { leaving = true }) { Text("Leave") }
                }
            }
        }
    }

    if (leaving) AlertDialog(onDismissRequest = { leaving = false },
        title = { KeepDialogInTopHalf(); Text(if (ui.kept > 0) "Finish this capture?" else "Leave?") },
        text = { Text(if (ui.kept > 0) "${ui.kept} photos kept" + (if (ui.total > 0 && ui.done < ui.total) ", with ${ui.total - ui.done} of ${ui.total} still to do. What's there is still worth sending." else ".") else "No photos have been kept.") },
        confirmButton = { TextButton(onClick = {
            leaving = false
            if (ui.kept > 0) { controller.finish(); go(Screen.Done(controller.id)) } else { a.store.deleteCapture(controller.id); go(Screen.Home) }
        }) { Text(if (ui.kept > 0) "Finish" else "Leave") } },
        dismissButton = { TextButton(onClick = { leaving = false }) { Text("Carry on") } })
}
