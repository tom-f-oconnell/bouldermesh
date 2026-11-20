package sh.boulderme.app

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.BitmapRegionDecoder
import android.graphics.Paint
import android.graphics.Rect
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import sh.boulderme.core.CaptureRecord
import sh.boulderme.core.HoldReview
import sh.boulderme.core.HoldView
import java.io.File
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/** A capture's record (capture.json), or null if it can't be read. */
fun captureRecord(a: MainActivity, id: String): CaptureRecord? =
    HoldReview.parse(runCatching { File(a.store.captureDir(id), "capture.json").readText() }.getOrNull())

/**
 * Before uploading: the holds the app found by itself, each with where it came from and a piece of the photo it's
 * seen best in, ringed, to keep or leave out. Those tapped are listed too, kept. Done writes the record (HoldReview.keep).
 */
@Composable
fun HoldsScreen(a: MainActivity, id: String, go: (Screen) -> Unit) {
    val record = remember(id) { captureRecord(a, id) }
    val holds = remember(record) { record?.let { HoldReview.holds(it) } ?: emptyList() }
    var keep by remember(record) { mutableStateOf(holds.filter { it.found }.map { it.id }.toSet()) }
    val dir = a.store.captureDir(id)
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = { go(Screen.Done(id)) }) { Text("‹ Back") }
            Text("Check its holds", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(vertical = 12.dp))
        }
        if (record == null) { Text("This capture's record can't be read."); return }
        val found = holds.count { it.found }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.weight(1f)) {
            item {
                HoldReview.said(record)?.let { Text(it) }
                if (found > 0) Text("Those the app found are only guesses: untick any that isn't a hold of the climb (the ring shows which). " +
                    "Only those ticked are uploaded and added to the mesh.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { keep = holds.filter { it.found }.map { it.id }.toSet() }) { Text("Tick all") }
                    OutlinedButton(onClick = { keep = emptySet() }) { Text("Untick all") }
                }
            }
            items(holds, key = { it.id }) { h ->
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp),
                    modifier = Modifier.fillMaxWidth().clickable(enabled = h.found) { keep = if (h.id in keep) keep - h.id else keep + h.id }) {
                    Box(Modifier.size(110.dp).background(MaterialTheme.colorScheme.surfaceVariant)) {
                        h.view?.let { v -> HoldCrop(File(dir, "photos/" + v.photo), v) } ?: Text("Not in view in any photo", style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.align(Alignment.Center).padding(6.dp))
                    }
                    Column(Modifier.weight(1f)) {
                        Text(h.id, fontWeight = FontWeight.SemiBold)
                        Text(h.how, style = MaterialTheme.typography.bodySmall)
                    }
                    if (h.found) Checkbox(checked = h.id in keep, onCheckedChange = { keep = if (it) keep + h.id else keep - h.id })
                    else Text("Kept", style = MaterialTheme.typography.bodySmall, color = GOOD)
                }
            }
        }
        val dropped = found - keep.size
        Button(modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp), onClick = {
            File(dir, "capture.json").writeText(HoldReview.keep(record, keep).json())
            go(Screen.Done(id))
        }) { Text(if (dropped == 0) "Keep all ${holds.size}" else "Keep ${holds.size - dropped}, leave out $dropped") }
    }
}

/** The piece of a photo round a hold, a few times its size across, with the hold ringed. */
@Composable
private fun HoldCrop(file: File, v: HoldView) {
    val bitmap by produceState<Bitmap?>(null, file, v) { value = withContext(Dispatchers.IO) { crop(file, v) } }
    bitmap?.let { Image(it.asImageBitmap(), null, Modifier.fillMaxSize()) }
}

private fun crop(file: File, v: HoldView): Bitmap? = runCatching {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeFile(file.path, bounds)
    val w = bounds.outWidth; val h = bounds.outHeight
    if (w <= 0 || h <= 0) return null
    val k = w.toDouble() / v.w         // the lens is for the picture as stored; scaled, should the file be another size
    val u = v.u * k; val y = v.v * k; val r = v.rPx * k
    val side = min(min(w, h).toDouble(), max(r * 6, min(w, h) * 0.2))
    val x0 = (u - side / 2).coerceIn(0.0, w - side); val y0 = (y - side / 2).coerceIn(0.0, h - side)
    val rect = Rect(x0.roundToInt(), y0.roundToInt(), (x0 + side).roundToInt(), (y0 + side).roundToInt())
    var sample = 1
    while (side / (sample * 2) >= 320) sample *= 2
    @Suppress("DEPRECATION") val region = BitmapRegionDecoder.newInstance(file.path, false) ?: return null
    val got = try { region.decodeRegion(rect, BitmapFactory.Options().apply { inSampleSize = sample }) } finally { region.recycle() } ?: return null
    val out = got.copy(Bitmap.Config.ARGB_8888, true)
    val s = out.width.toDouble() / rect.width()
    val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = max(2f, out.width / 60f); color = android.graphics.Color.MAGENTA }
    android.graphics.Canvas(out).drawCircle(((u - rect.left) * s).toFloat(), ((y - rect.top) * s).toFloat(), (max(r * 1.3, 6.0) * s).toFloat(), ring)
    out
}.getOrNull()
