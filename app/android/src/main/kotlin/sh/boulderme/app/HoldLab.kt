package sh.boulderme.app

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Checkbox
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import androidx.exifinterface.media.ExifInterface
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import sh.boulderme.core.Gym
import sh.boulderme.core.HoldColour
import sh.boulderme.core.HoldProbe
import sh.boulderme.core.Picture
import java.io.File

/**
 * The owner's look at how a gym climb's holds are found (Settings' debug tools), in one photo: one of a capture's on the
 * phone, or one taken now. Every patch of the chosen finder (colour, A; or the model, B) is outlined faintly; tap a hold
 * and those of its colour are outlined in it, the one tapped thicker, as a gym capture would take them. A new tap
 * starts again from that hold. The colour's tolerance, the model's margin and its mask are on the screen.
 */
@OptIn(ExperimentalLayoutApi::class)
@androidx.compose.runtime.Composable
fun HoldLabScreen(a: MainActivity, go: (Screen) -> Unit) {
    var file by remember { mutableStateOf<File?>(null) }
    var capture by remember { mutableStateOf<String?>(null) }       // whose photos are listed
    var finder by remember { mutableStateOf(Gym.FINDER_COLOUR) }
    var tol by remember { mutableStateOf(HoldColour.TOLERANCES.indexOf(1.0)) }
    var margin by remember { mutableStateOf(0f) }
    var showMask by remember { mutableStateOf(false) }
    var tap by remember { mutableStateOf<Pair<Double, Double>?>(null) }
    var busy by remember { mutableStateOf(false) }
    var said by remember { mutableStateOf<String?>(null) }
    val finderModel = remember { HoldFinder.load(a) }
    val shot = remember { File(a.cacheDir, "lab").apply { mkdirs() }.let { File(it, "taken.jpg") } }
    val take = rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { ok -> if (ok) { file = shot; tap = null } }
    // the photo, as the finders see it: its longer side Picture.SIDE, upright
    var pic by remember { mutableStateOf<Pair<Bitmap, Picture>?>(null) }
    LaunchedEffect(file) {
        pic = file?.let { f -> withContext(Dispatchers.IO) { runCatching { load(f) }.getOrNull() } }
        if (file != null && pic == null) said = "That photo couldn't be read."
    }
    var result by remember { mutableStateOf<HoldProbe.Result?>(null) }
    LaunchedEffect(pic, finder, tol, margin, tap) {
        val p = pic ?: return@LaunchedEffect
        busy = true
        result = withContext(Dispatchers.Default) {
            runCatching { HoldProbe.run(p.second, finderModel, finder, tap?.first, tap?.second, HoldColour.TOLERANCES[tol], margin) }.getOrNull()
        }
        busy = false
    }
    Page("Hold finding", back = { go(Screen.Settings) }) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { runCatching { take.launch(FileProvider.getUriForFile(a, "${a.packageName}.files", shot)) }.onFailure { said = it.message } }) { Text("Take a photo") }
            OutlinedButton(onClick = { capture = ""; file = null }) { Text("A capture's photo…") }
            OutlinedButton(onClick = { go(Screen.HoldLive) }) { Text("Live") }
        }
        said?.let { Text(it, color = BAD) }
        val c = capture
        if (c != null && file == null) {
            if (c.isEmpty()) {
                val ids = remember { a.store.captureIds().filter { (File(a.store.captureDir(it), "photos").list()?.size ?: 0) > 0 } }
                if (ids.isEmpty()) Text("No captures with photos on this phone.")
                for (id in ids) TextButton(onClick = { capture = id }) { Text("${a.store.upload(id)?.name ?: id} (${File(a.store.captureDir(id), "photos").list()?.size ?: 0} photos)") }
            } else {
                val photos = remember(c) { (File(a.store.captureDir(c), "photos").listFiles() ?: emptyArray()).filter { it.name.endsWith(".jpg") }.sortedBy { it.name } }
                TextButton(onClick = { capture = "" }) { Text("‹ Other captures") }
                FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    for (f in photos.take(120)) Thumb(f) { file = f; tap = null; capture = null }
                }
            }
        }
        val p = pic
        if (p != null) {
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                FilterChip(selected = finder == Gym.FINDER_COLOUR, onClick = { finder = Gym.FINDER_COLOUR }, label = { Text("Colour (A)") })
                FilterChip(selected = finder != Gym.FINDER_COLOUR, enabled = finderModel != null, onClick = { finder = Gym.FINDER_MODEL }, label = { Text(if (finderModel != null) "Model (B)" else "No model here") })
                if (busy) Text("…")
            }
            val r = result
            Text(when {
                r == null -> "Finding…"
                r.picked == null && tap != null -> "No ${if (finder == Gym.FINDER_COLOUR) "colour patch" else "hold the model found"} there: tap right on a hold."
                r.picked == null -> "Tap a hold. ${r.colour.size} colour patches, ${r.model?.size ?: "no"} within the model's holds."
                else -> "${r.name}: ${r.matches.size} of ${(if (finder == Gym.FINDER_COLOUR) r.colour else r.model.orEmpty()).size} patches of its colour"
            }, style = MaterialTheme.typography.bodySmall)
            Box(Modifier.fillMaxWidth().aspectRatio(p.first.width.toFloat() / p.first.height)) {
                Image(p.first.asImageBitmap(), null, Modifier.fillMaxSize(), contentScale = ContentScale.FillBounds)
                Canvas(Modifier.fillMaxSize().pointerInput(p) {
                    detectTapGestures { o -> tap = o.x / size.width * p.second.w * p.second.step to o.y / size.height * p.second.h * p.second.step }
                }) {
                    val k = size.width / (p.second.w * p.second.step).toFloat()
                    if (showMask) r?.mask?.let { m ->
                        val cell = size.width / p.second.w
                        for (y in 0 until p.second.h) for (x in 0 until p.second.w) if (m[y * p.second.w + x])
                            drawRect(Color(0x5500E5FF), Offset(x * cell, y * cell), Size(cell, cell))
                    }
                    val all = if (finder == Gym.FINDER_COLOUR) r?.colour else r?.model
                    val hold = r?.rgb?.let { Color(it or 0xff000000.toInt()) } ?: Color.White
                    for (b in all.orEmpty()) {
                        val on = r?.matches?.contains(b) == true
                        drawRect(if (on) hold else Color(0x66FFFFFF), Offset((b.x0 * k).toFloat(), (b.y0 * k).toFloat()),
                            Size(((b.x1 - b.x0) * k).toFloat(), ((b.y1 - b.y0) * k).toFloat()),
                            style = Stroke(if (b == r?.picked) 6f else if (on) 3f else 1f))
                    }
                    tap?.let { (u, v) -> drawCircle(Color.White, 10f, Offset((u * k).toFloat(), (v * k).toFloat()), style = Stroke(2f)) }
                }
            }
            if (finder == Gym.FINDER_COLOUR || tap != null) {
                Text("Colour tolerance: ×${HoldColour.TOLERANCES[tol]} (a gym capture's Narrower / Wider)", style = MaterialTheme.typography.bodySmall)
                Slider(value = tol.toFloat(), onValueChange = { tol = it.toInt() }, valueRange = 0f..(HoldColour.TOLERANCES.size - 1).toFloat(), steps = HoldColour.TOLERANCES.size - 2)
            }
            if (finderModel != null) {
                Text("Model margin: ${"%.1f".format(margin)} (how much its hold score must beat the others'; captures use 0)", style = MaterialTheme.typography.bodySmall)
                Slider(value = margin, onValueChange = { margin = (it * 2).toInt() / 2f }, valueRange = -2f..4f)
                Row(verticalAlignment = Alignment.CenterVertically) { Checkbox(checked = showMask, onCheckedChange = { showMask = it }); Text("Show the model's mask") }
            }
            TextButton(onClick = { tap = null }) { Text("Clear the tap") }
        }
    }
}

@Composable
private fun Thumb(f: File, pick: () -> Unit) {
    val bmp = remember(f) { runCatching { BitmapFactory.decodeFile(f.path, BitmapFactory.Options().apply { inSampleSize = 16 }) }.getOrNull() }
    if (bmp != null) Image(bmp.asImageBitmap(), f.name, Modifier.size(72.dp).clickable(onClick = pick), contentScale = ContentScale.Crop)
    else TextButton(onClick = pick) { Text(f.name) }
}

/** A photo, upright and with its longer side Picture.SIDE, and as the finders see it. */
private fun load(f: File): Pair<Bitmap, Picture> {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeFile(f.path, bounds)
    var sample = 1
    while (maxOf(bounds.outWidth, bounds.outHeight) / (sample * 2) >= Picture.SIDE) sample *= 2
    var bmp = BitmapFactory.decodeFile(f.path, BitmapFactory.Options().apply { inSampleSize = sample })!!
    val turn = when (runCatching { ExifInterface(f.path).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL) }.getOrDefault(1)) {
        ExifInterface.ORIENTATION_ROTATE_90 -> 90f; ExifInterface.ORIENTATION_ROTATE_180 -> 180f; ExifInterface.ORIENTATION_ROTATE_270 -> 270f; else -> 0f
    }
    val scale = Picture.SIDE.toFloat() / maxOf(bmp.width, bmp.height)
    bmp = Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height, Matrix().apply { if (scale < 1f) postScale(scale, scale); postRotate(turn) }, true)
    val px = IntArray(bmp.width * bmp.height).also { bmp.getPixels(it, 0, bmp.width, 0, 0, bmp.width, bmp.height) }
    return bmp to Picture.ofRgb(px, bmp.width, bmp.height)
}
