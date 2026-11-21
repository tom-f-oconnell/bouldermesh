package sh.boulderme.app

import android.Manifest
import android.media.Image
import android.opengl.GLES20
import android.opengl.GLSurfaceView
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Slider
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
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import com.google.ar.core.ArCoreApk
import com.google.ar.core.Config
import com.google.ar.core.Coordinates2d
import com.google.ar.core.Session
import sh.boulderme.core.Gym
import sh.boulderme.core.HoldColour
import sh.boulderme.core.HoldProbe
import sh.boulderme.core.Picture
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10

/** What the live look draws: each patch's box on the screen, whether it's of the tapped hold's colour or the one tapped. */
data class LiveBox(val x0: Float, val y0: Float, val x1: Float, val y1: Float, val on: Boolean, val picked: Boolean)
data class LiveUi(val boxes: List<LiveBox> = emptyList(), val rgb: Int? = null, val said: String = "Starting the camera…", val ms: Long = 0)

/**
 * The owner's live look at how holds are found (Settings' debug tools): the camera's picture, with the chosen finder's
 * patches over it a few times a second (HoldProbe), and those of a tapped hold's colour outlined in it. A tap starts
 * again from the hold there. ARCore only shows the camera here: nothing is tracked, kept or sent.
 */
class HoldLiveController(private val activity: MainActivity, private val onUi: (LiveUi) -> Unit) : GLSurfaceView.Renderer {
    @Volatile var finder = Gym.FINDER_COLOUR
    @Volatile var tolerance = 1.0
    @Volatile var margin = 0f
    @Volatile private var tap: FloatArray? = null       // on the screen, until the next frame turns it into the picture's pixels
    @Volatile private var tapAt: DoubleArray? = null     // in the camera image's pixels
    @Volatile private var found: Pair<HoldProbe.Result, Double>? = null     // the last result, and its picture's step
    private val model = HoldFinder.load(activity)
    val hasModel get() = model != null
    private var session: Session? = null
    private var background: Background? = null
    private var width = 1; private var height = 1
    private val worker = Executors.newSingleThreadExecutor()
    private val busy = AtomicBoolean(false)
    private var lastRun = 0L
    private var installAsked = false

    fun resume(): String? = try {
        if (session == null) {
            if (ArCoreApk.getInstance().requestInstall(activity, !installAsked) == ArCoreApk.InstallStatus.INSTALL_REQUESTED) { installAsked = true; null }
            else Session(activity).also { s ->
                s.configure(Config(s).apply { focusMode = Config.FocusMode.AUTO; updateMode = Config.UpdateMode.LATEST_CAMERA_IMAGE })
                session = s
            }
        }
        session?.resume(); null
    } catch (e: Exception) { "Couldn't start the camera: ${e.message ?: e.javaClass.simpleName}" }

    fun pause() { session?.pause() }
    fun close() { val s = session; session = null; worker.execute { s?.close() }; worker.shutdown() }
    fun tap(x: Float, y: Float) { tap = floatArrayOf(x, y) }
    fun clearTap() { tapAt = null }

    override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) { GLES20.glClearColor(0f, 0f, 0f, 1f); background = Background() }
    override fun onSurfaceChanged(gl: GL10?, w: Int, h: Int) { width = w; height = h; GLES20.glViewport(0, 0, w, h) }

    @Suppress("DEPRECATION")
    override fun onDrawFrame(gl: GL10?) {
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT)
        val s = session ?: return
        val bg = background ?: return
        val frame = try { s.setCameraTextureName(bg.texture); s.setDisplayGeometry(activity.windowManager.defaultDisplay.rotation, width, height); s.update() } catch (e: Exception) { return }
        bg.draw(frame)
        tap?.let { t -> tap = null; FloatArray(2).also { frame.transformCoordinates2d(Coordinates2d.VIEW, t, Coordinates2d.IMAGE_PIXELS, it) }.let { tapAt = doubleArrayOf(it[0].toDouble(), it[1].toDouble()) } }
        val now = System.currentTimeMillis()
        if (now - lastRun > 250 && busy.compareAndSet(false, true)) {
            lastRun = now
            val image: Image? = try { frame.acquireCameraImage() } catch (e: Exception) { null }
            if (image == null) busy.set(false) else {
                val w = image.width; val h = image.height
                val nv21 = try { nv21(image) } finally { image.close() }
                val at = tapAt; val f = finder; val tol = tolerance; val m = margin
                worker.execute {
                    try {
                        val began = System.currentTimeMillis()
                        val p = Picture.ofYuv(nv21, w, nv21, w * h, w, w, h, vFirst = true)
                        val r = HoldProbe.run(p, model, f, at?.get(0), at?.get(1), tol, m)
                        found = r to p.step
                        lastMs = System.currentTimeMillis() - began
                    } catch (_: Exception) {
                    } finally { busy.set(false) }
                }
            }
        }
        // the last result's boxes, from the camera image's pixels to the screen's, as this frame shows the picture
        val (r, _) = found ?: return
        val all = if (finder == Gym.FINDER_COLOUR) r.colour else r.model.orEmpty()
        val matched = r.matches.toHashSet()
        val pts = FloatArray(all.size * 4)
        for ((i, b) in all.withIndex()) { pts[4 * i] = b.x0.toFloat(); pts[4 * i + 1] = b.y0.toFloat(); pts[4 * i + 2] = b.x1.toFloat(); pts[4 * i + 3] = b.y1.toFloat() }
        val view = FloatArray(pts.size)
        if (pts.isNotEmpty()) frame.transformCoordinates2d(Coordinates2d.IMAGE_PIXELS, pts, Coordinates2d.VIEW, view)
        val boxes = all.indices.map { i ->
            LiveBox(minOf(view[4 * i], view[4 * i + 2]), minOf(view[4 * i + 1], view[4 * i + 3]), maxOf(view[4 * i], view[4 * i + 2]), maxOf(view[4 * i + 1], view[4 * i + 3]),
                all[i] in matched, all[i] == r.picked)
        }
        val said = when {
            finder != Gym.FINDER_COLOUR && r.model == null -> "The model couldn't be run"
            r.picked == null && tapAt != null -> "Nothing found where you tapped: tap right on a hold"
            r.picked == null -> "${all.size} patches. Tap a hold"
            else -> "${r.name}: ${r.matches.size} of ${all.size} patches of its colour"
        }
        activity.runOnUiThread { onUi(LiveUi(boxes, r.rgb, said, lastMs)) }
    }
    @Volatile private var lastMs = 0L

    private fun nv21(image: Image): ByteArray {
        val w = image.width; val h = image.height
        val out = ByteArray(w * h * 3 / 2)
        val y = image.planes[0]; val u = image.planes[1]; val v = image.planes[2]
        val yb = y.buffer
        for (row in 0 until h) { yb.position(row * y.rowStride); yb.get(out, row * w, w) }
        val ub = u.buffer; val vb = v.buffer
        var at = w * h
        for (row in 0 until h / 2) for (col in 0 until w / 2) {
            out[at++] = vb.get(row * v.rowStride + col * v.pixelStride)
            out[at++] = ub.get(row * u.rowStride + col * u.pixelStride)
        }
        return out
    }
}

/** HoldLiveController's screen: the camera, the boxes over it, and the finder's settings at the bottom. */
@Composable
fun HoldLiveScreen(a: MainActivity, go: (Screen) -> Unit) {
    var ui by remember { mutableStateOf(LiveUi()) }
    var failed by remember { mutableStateOf<String?>(null) }
    var finder by remember { mutableStateOf(Gym.FINDER_COLOUR) }
    var tol by remember { mutableStateOf(HoldColour.TOLERANCES.indexOf(1.0)) }
    var margin by remember { mutableStateOf(0f) }
    var allowed by remember { mutableStateOf(a.has(Manifest.permission.CAMERA)) }
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { allowed = it }
    BackHandler { go(Screen.HoldLab) }
    if (!allowed) {
        Column(Modifier.fillMaxSize().safeDrawingPadding().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("This needs the camera.")
            Button(onClick = { ask.launch(Manifest.permission.CAMERA) }) { Text("Allow the camera") }
            TextButton(onClick = { go(Screen.HoldLab) }) { Text("Back") }
        }
        return
    }
    val controller = remember { HoldLiveController(a) { ui = it } }
    val view = remember {
        GLSurfaceView(a).apply {
            preserveEGLContextOnPause = true
            setEGLContextClientVersion(2)
            setRenderer(controller)
            renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY
            keepScreenOn = true
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
        a.lifecycle.addObserver(watch)
        onDispose { a.lifecycle.removeObserver(watch); view.onPause(); controller.pause(); controller.close() }
    }
    Box(Modifier.fillMaxSize()) {
        AndroidView(factory = { view }, modifier = Modifier.fillMaxSize())
        Canvas(Modifier.fillMaxSize().pointerInput(Unit) { detectTapGestures { o -> controller.tap(o.x, o.y) } }) {
            val hold = ui.rgb?.let { Color(it or 0xff000000.toInt()) } ?: Color.White
            for (b in ui.boxes) drawRect(if (b.on) hold else Color(0x77FFFFFF), Offset(b.x0, b.y0), Size(b.x1 - b.x0, b.y1 - b.y0),
                style = Stroke(if (b.picked) 7f else if (b.on) 4f else 1.5f))
        }
        Column(Modifier.align(Alignment.TopStart).safeDrawingPadding().padding(8.dp).background(Color(0x99000000)).padding(8.dp)) {
            Text((failed ?: ui.said) + if (ui.ms > 0) " · ${ui.ms} ms" else "", color = Color.White)
        }
        Column(Modifier.align(Alignment.BottomCenter).fillMaxWidth().safeDrawingPadding().background(Color(0xAA000000)).padding(8.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                FilterChip(selected = finder == Gym.FINDER_COLOUR, onClick = { finder = Gym.FINDER_COLOUR; controller.finder = finder }, label = { Text("Colour (A)") })
                FilterChip(selected = finder != Gym.FINDER_COLOUR, enabled = controller.hasModel, onClick = { finder = Gym.FINDER_MODEL; controller.finder = finder },
                    label = { Text(if (controller.hasModel) "Model (B)" else "No model here") })
                TextButton(onClick = { controller.clearTap() }) { Text("Clear the tap") }
                TextButton(onClick = { go(Screen.HoldLab) }) { Text("Back") }
            }
            Text("Colour tolerance ×${HoldColour.TOLERANCES[tol]}", color = Color.White)
            Slider(value = tol.toFloat(), onValueChange = { tol = it.toInt(); controller.tolerance = HoldColour.TOLERANCES[tol] },
                valueRange = 0f..(HoldColour.TOLERANCES.size - 1).toFloat(), steps = HoldColour.TOLERANCES.size - 2)
            if (controller.hasModel && finder != Gym.FINDER_COLOUR) {
                Text("Model margin ${"%.1f".format(margin)}", color = Color.White)
                Slider(value = margin, onValueChange = { margin = (it * 2).toInt() / 2f; controller.margin = margin }, valueRange = -2f..4f)
            }
        }
    }
}
