package sh.boulderme.app

import android.app.Activity
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.content.Context
import android.media.Image
import android.opengl.GLES20
import android.opengl.GLSurfaceView
import android.opengl.Matrix
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.view.Surface
import androidx.exifinterface.media.ExifInterface
import com.google.ar.core.ArCoreApk
import com.google.ar.core.CameraConfigFilter
import com.google.ar.core.Config
import com.google.ar.core.DepthPoint
import com.google.ar.core.Frame
import com.google.ar.core.ImageMetadata
import com.google.ar.core.Plane
import com.google.ar.core.Point
import com.google.ar.core.Session
import com.google.ar.core.TrackingState
import com.google.ar.core.exceptions.NotYetAvailableException
import sh.boulderme.core.CameraPose
import sh.boulderme.core.CaptureSession
import sh.boulderme.core.Coverage
import sh.boulderme.core.FrameIn
import sh.boulderme.core.GuideMesh
import sh.boulderme.core.Lens
import sh.boulderme.core.Marker
import sh.boulderme.core.Plan
import sh.boulderme.core.Quality
import sh.boulderme.core.Quat
import sh.boulderme.core.RecordCalibration
import sh.boulderme.core.Scope
import sh.boulderme.core.ShotIn
import sh.boulderme.core.Similarity
import sh.boulderme.core.Vec3
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10
import kotlin.math.atan2

enum class Phase { STARTING, GHOST, ROCK, CONFIRM, CENTRE, CAPTURE, FAILED }

/** What the capture screen shows over the camera. */
data class CaptureUi(
    val phase: Phase = Phase.STARTING, val text: String = "Starting the camera…", val turnX: Double = 0.0, val turnY: Double = 0.0,
    val total: Int = 0, val done: Int = 0, val holds: Int = 0, val holdsDone: Int = 0, val kept: Int = 0, val rejected: Int = 0,
    val note: String = "", val tracking: Boolean = false, val auto: Boolean = true, val canSkip: Boolean = false, val wide: Boolean = false,
    val marking: Boolean = false, val pairs: Int = 0,
)

/**
 * A capture on the phone: ARCore tracks where the phone is, the shared CaptureSession says what to photograph next and
 * judges each photo, and this draws the mesh and its targets over the camera's picture and saves the photos, each with
 * its readings in its EXIF.
 *
 * Photos are the frames ARCore itself reads from the camera (it gives no larger while it tracks): 1920 x 1080 on most
 * phones, so a hold needs photographing from nearer than a phone's full-size photo would.
 */
class ArController(
    private val activity: Activity, private val store: Store, private val sensors: PhoneSensors,
    private val plan: Plan?, private val guide: GuideMesh?, private val name: String, private val onUi: (CaptureUi) -> Unit,
) : GLSurfaceView.Renderer {
    val id: String = java.lang.Long.toString(System.currentTimeMillis() / 1000, 36) + (100..999).random()
    private val dir = store.captureDir(id)
    private val core = CaptureSession(plan, guide, id, BuildInfo.APP, "${Build.MANUFACTURER} ${Build.MODEL}", System.currentTimeMillis() / 1000.0).apply {
        accelBias = store.accelBias
        calibration = store.accelBias?.let { RecordCalibration(accelBias = it.toList()) }
    }
    private val lock = Any()
    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private var session: Session? = null
    private var installAsked = false
    private var background: Background? = null
    private var guideRenderer: GuideRenderer? = null
    private var markerRenderer: MarkerRenderer? = null
    private val view = FloatArray(16); private val proj = FloatArray(16); private val viewProj = FloatArray(16); private val mvp = FloatArray(16)
    private var width = 1; private var height = 1
    private var sensorOrientation = 90
    private val arSession = "a" + (System.nanoTime() % 100000)

    @Volatile private var phase = if (plan != null) Phase.GHOST else Phase.CENTRE
    @Volatile private var ui = CaptureUi(phase = phase, auto = true)
    private var ghost: Similarity? = null
    private var ghostTurn = 0.0
    private var meshTap: Vec3? = null
    private val tapMarks = ArrayList<Marker>()
    @Volatile private var pendingTap: FloatArray? = null
    @Volatile private var wantShot = false
    @Volatile private var marking = false
    @Volatile private var wide = false
    private val busy = AtomicBoolean(false)
    private var rejected = 0
    private var note = ""
    @Volatile private var levelsDirty = true
    @Volatile private var markers: List<Marker> = emptyList()
    private val cloud = HashMap<Long, Vec3>()
    private var lastRefine = 0.0
    private var lastUi = 0.0
    private var lastPose: CameraPose? = null
    private var lastTarget: String? = null

    // ---- from the screen ---------------------------------------------------------------------------------------------
    /** Starts or carries on tracking. Returns why it can't, in words, or null. */
    fun resume(): String? {
        try {
            if (session == null) {
                if (ArCoreApk.getInstance().requestInstall(activity, !installAsked) == ArCoreApk.InstallStatus.INSTALL_REQUESTED) { installAsked = true; return null }
                val s = Session(activity)
                // the largest picture ARCore will read out while it tracks
                s.getSupportedCameraConfigs(CameraConfigFilter(s)).maxByOrNull { it.imageSize.width.toLong() * it.imageSize.height }?.let { s.cameraConfig = it }
                s.configure(Config(s).apply {
                    focusMode = Config.FocusMode.AUTO          // holds are photographed from well under a metre
                    updateMode = Config.UpdateMode.BLOCKING           // a frame drawn for each the camera gives, and none twice
                    planeFindingMode = Config.PlaneFindingMode.HORIZONTAL_AND_VERTICAL
                    if (s.isDepthModeSupported(Config.DepthMode.AUTOMATIC)) depthMode = Config.DepthMode.AUTOMATIC     // taps then land on bare rock
                })
                sensorOrientation = runCatching {
                    (activity.getSystemService(Context.CAMERA_SERVICE) as CameraManager).getCameraCharacteristics(s.cameraConfig.cameraId).get(CameraCharacteristics.SENSOR_ORIENTATION)
                }.getOrNull() ?: 90
                session = s
            }
            session?.resume()
            return null
        } catch (e: Exception) {
            phase = Phase.FAILED
            return when (e.javaClass.simpleName) {
                "UnavailableDeviceNotCompatibleException" -> "This phone can't track its own position (it doesn't support ARCore), which guided capture needs."
                "UnavailableUserDeclinedInstallationException", "UnavailableArcoreNotInstalledException" -> "Guided capture needs Google Play Services for AR installed."
                "CameraNotAvailableException" -> "The camera is in use by something else."
                else -> "Couldn't start tracking: ${e.message ?: e.javaClass.simpleName}"
            }
        }
    }

    fun pause() { session?.pause() }
    fun close() { worker.shutdown(); session?.close(); session = null }

    fun tap(x: Float, y: Float) { pendingTap = floatArrayOf(x, y) }
    fun shutter() { wantShot = true }
    fun setAuto(on: Boolean) { synchronized(lock) { core.auto = on }; push(force = true) }
    fun skip() { synchronized(lock) { core.skipCurrent("skipped on the phone") }; levelsDirty = true }
    fun toggleMarking() { marking = !marking; push(force = true) }
    fun toggleWide() { wide = !wide; synchronized(lock) { core.setScope(Scope(minPrio = if (wide) 1 else 2)) }; levelsDirty = true }
    fun turnGhost(degrees: Double) { ghostTurn += degrees * Math.PI / 180; ghost = null }
    fun bringGhost() { ghost = null }
    fun confirmFit() { if (phase == Phase.CONFIRM) { phase = Phase.CAPTURE; levelsDirty = true } }
    fun restartAlign() { synchronized(lock) { core.clearTaps() }; tapMarks.clear(); meshTap = null; ghost = null; phase = Phase.GHOST }
    fun addSpot() { if (phase == Phase.CONFIRM) phase = Phase.GHOST }

    /** In a capture with no mesh: the side the hint is asking for can't be reached. */
    fun blockSide() {
        val pose = lastPose ?: return
        synchronized(lock) { core.orbit?.blockNext(pose) }
    }

    /** Writes the capture's record and what's to upload beside its photos: after each photo kept, so that one cut short isn't lost. */
    private fun save(): Int = synchronized(lock) {
        core.place = sensors.place.result()
        File(dir, "capture.json").writeText(core.record(System.currentTimeMillis() / 1000.0).json())
        store.saveUpload(id, Store.Upload(name = name, mesh = plan?.mesh, photos = core.shotCount))
        core.shotCount
    }

    /** Ends the capture. Returns how many photos were kept. */
    fun finish(): Int = save()

    // ---- drawing, on the GL thread -----------------------------------------------------------------------------------
    override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
        GLES20.glClearColor(0.05f, 0.05f, 0.05f, 1f)
        background = Background()
        guideRenderer = guide?.let { GuideRenderer(it) }
        levelsDirty = true
        markerRenderer = MarkerRenderer()
    }

    override fun onSurfaceChanged(gl: GL10?, w: Int, h: Int) {
        width = w; height = h
        GLES20.glViewport(0, 0, w, h)
        session?.setDisplayGeometry(Surface.ROTATION_0, w, h)
    }

    override fun onDrawFrame(gl: GL10?) {
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT or GLES20.GL_DEPTH_BUFFER_BIT)
        val s = session ?: return
        val bg = background ?: return
        val frame = try { s.setCameraTextureName(bg.texture); s.setDisplayGeometry(Surface.ROTATION_0, width, height); s.update() } catch (e: Exception) { return }
        bg.draw(frame)
        val camera = frame.camera
        val tracking = camera.trackingState == TrackingState.TRACKING
        val now = sensors.now()
        if (!tracking) {
            pendingTap = null
            push(text = "Hold on: the phone is finding where it is. Move it slowly, looking at the rock.", tracking = false, now = now)
            return
        }
        camera.getViewMatrix(view, 0)
        camera.getProjectionMatrix(proj, 0, 0.05f, 100f)
        Matrix.multiplyMM(viewProj, 0, proj, 0, view, 0)
        val pose = corePose(camera)
        lastPose = pose
        collect(frame)
        val tap = pendingTap
        pendingTap = null

        when (phase) {
            Phase.GHOST, Phase.ROCK -> {
                val g = ghost ?: placeGhost(pose).also { ghost = it }
                applyLevels()
                drawGuide(g, 2.5f, solid = true)
                if (tap != null) onAlignTap(frame, tap, g)
                markerRenderer?.draw(tapMarks, viewProj, pixelsPerMetre())
                val n = synchronized(lock) { core.taps.size }
                push(text = if (phase == Phase.ROCK) "Now tap the same spot on the real rock"
                else if (n >= 2) "Tap another spot on the small model that you can find on the rock, well to one side of the others.$note"
                else "Tap a spot on the small model that you can find on the rock (${n + 1} of 2). Pick two far apart, and not one above the other.$note",
                    tracking = true, now = now, pairs = n)
            }
            Phase.CONFIRM -> {
                refineSometimes(now)
                applyLevels()
                synchronized(lock) { core.fit }?.let { drawGuide(it, 0.8f) }
                push(text = "Is the coloured mesh sitting on the rock? It tightens by itself as you move.$note", tracking = true, now = now)
            }
            Phase.CENTRE -> {
                if (tap != null) hit(frame, tap)?.let { p -> synchronized(lock) { core.startOrbit(p) }; phase = Phase.CAPTURE }
                push(text = "Tap the middle of the rock", tracking = true, now = now)
            }
            Phase.CAPTURE -> capture(frame, pose, tap, now)
            else -> {}
        }
    }

    private fun capture(frame: Frame, pose: CameraPose, tap: FloatArray?, now: Double) {
        if (tap != null && marking) hit(frame, tap)?.let { p -> synchronized(lock) { core.orbit?.addSpot(p) }; marking = false; levelsDirty = true }
        refineSometimes(now)
        val exposure = runCatching { frame.imageMetadata.getLong(ImageMetadata.SENSOR_EXPOSURE_TIME) / 1e9 }.getOrNull() ?: (1.0 / 60)
        val gyro = sensors.gyro?.norm ?: 0.0
        val out = synchronized(lock) { core.frame(FrameIn(pose, gyro, exposure, true, now, sensorOrientation)) }
        if (out.hint.targetId != lastTarget) { lastTarget = out.hint.targetId; levelsDirty = true }      // the ring round what's asked for moves
        applyLevels()
        synchronized(lock) { core.fit }?.let { drawGuide(it, 0.75f) }
        markerRenderer?.draw(markers, viewProj, pixelsPerMetre())
        if ((out.shoot || wantShot) && busy.compareAndSet(false, true)) {
            wantShot = false
            takeStill(frame, pose, exposure, gyro, now)
        }
        val p = out.progress
        ui = ui.copy(total = p.total, done = p.done + p.skipped, holds = p.holds, holdsDone = p.holdsDone, turnX = out.hint.turnX, turnY = out.hint.turnY,
            canSkip = out.hint.targetId != null && plan != null)
        push(text = out.hint.text, tracking = true, now = now)
    }

    /** Colours the mesh and its rings afresh, if anything has changed how done its parts are. */
    private fun applyLevels() {
        if (!levelsDirty) return
        levelsDirty = false
        synchronized(lock) { core.coverage?.patchLevels()?.let { guideRenderer?.setLevels(it) }; markers = core.markers() }
    }

    private fun corePose(camera: com.google.ar.core.Camera): CameraPose {
        val p = camera.pose
        val q = p.rotationQuaternion       // x, y, z, w
        val k = camera.imageIntrinsics
        val f = k.focalLength; val c = k.principalPoint; val d = k.imageDimensions
        return CameraPose.fromGl(Vec3(p.tx().toDouble(), p.ty().toDouble(), p.tz().toDouble()), Quat(q[3].toDouble(), q[0].toDouble(), q[1].toDouble(), q[2].toDouble()),
            Lens(f[0].toDouble(), f[1].toDouble(), c[0].toDouble(), c[1].toDouble(), d[0], d[1]))
    }

    private fun pixelsPerMetre() = height * proj[5]      // a metre's diameter at a metre off

    private fun drawGuide(fit: Similarity, alpha: Float, solid: Boolean = false) {
        Matrix.multiplyMM(mvp, 0, viewProj, 0, fit.matrix(), 0)
        guideRenderer?.draw(mvp, alpha, solid)
    }

    /** The small model, a metre in front, the side its photos were taken from towards the phone. */
    private fun placeGhost(pose: CameraPose): Similarity {
        val g = guide!!; val pl = plan!!
        val (lo, hi) = g.bounds()
        val middle = (lo + hi) / 2.0
        val k = 0.6 / pl.frame.size
        val ahead = pose.forward.let { Vec3(it.x, 0.0, it.z) }.unit()
        val at = pose.centre + ahead * 1.1 + Vec3(0.0, -0.25, 0.0)
        var from = Vec3.ZERO
        for (c in pl.cameras) from += Vec3.of(c.c) - middle
        val turn = if (from.norm > 1e-6) atan2(-ahead.x, -ahead.z) - atan2(from.x, from.z) else 0.0
        val rot = Quat.yaw(turn + ghostTurn)
        return Similarity(k, rot, at - rot.rotate(middle * k))
    }

    private fun onAlignTap(frame: Frame, tap: FloatArray, g: Similarity) {
        if (phase == Phase.GHOST) {
            val (origin, direction) = ray(tap[0], tap[1])
            val inv = g.inverse()
            val o = inv.apply(origin); val d = inv.dir(direction).unit()
            val t = guide!!.raycast(o, d)
            if (t == null) { note = " That tap missed the model."; return }
            note = ""
            val spot = o + d * t
            meshTap = spot
            tapMarks.add(Marker("m${tapMarks.size}", g.apply(spot), 0.03, Coverage.LEVEL_PART, "hold", true))
            phase = Phase.ROCK
        } else {
            val world = hit(frame, tap) ?: run { note = " The phone hasn't made out the rock there yet: move a little and tap again."; return }
            val spot = meshTap ?: return
            tapMarks.add(Marker("w${tapMarks.size}", world, 0.05, Coverage.LEVEL_DONE, "hold", false))
            val fit = synchronized(lock) { core.tap(spot, world) }
            val n = synchronized(lock) { core.taps.size }
            note = ""
            phase = when {
                fit != null -> { tapMarks.clear(); levelsDirty = true; Phase.CONFIRM }
                n >= 2 -> { note = " Those are too nearly one above the other to tell which way it's turned."; Phase.GHOST }
                else -> Phase.GHOST
            }
        }
    }

    /** Where a tap on the screen meets the world, by ARCore: on a plane it's found, a point it tracks, or its depth map. */
    private fun hit(frame: Frame, tap: FloatArray): Vec3? {
        for (h in frame.hitTest(tap[0], tap[1])) {
            val t = h.trackable
            val ok = (t is Plane && t.isPoseInPolygon(h.hitPose)) || t is Point || t is DepthPoint
            if (ok) return h.hitPose.let { Vec3(it.tx().toDouble(), it.ty().toDouble(), it.tz().toDouble()) }
        }
        return null
    }

    /** The ray a point of the screen looks along, in the world. */
    private fun ray(x: Float, y: Float): Pair<Vec3, Vec3> {
        val inv = FloatArray(16)
        Matrix.invertM(inv, 0, viewProj, 0)
        fun unproject(z: Float): Vec3 {
            val v = floatArrayOf(2 * x / width - 1, 1 - 2 * y / height, z, 1f); val o = FloatArray(4)
            Matrix.multiplyMV(o, 0, inv, 0, v, 0)
            return Vec3((o[0] / o[3]).toDouble(), (o[1] / o[3]).toDouble(), (o[2] / o[3]).toDouble())
        }
        val near = unproject(-1f); val far = unproject(1f)
        return near to (far - near).unit()
    }

    /** Keeps the points ARCore tracks, one to each 5 cm of the world, for tightening the mesh's fit against. */
    private fun collect(frame: Frame) {
        if (plan == null || cloud.size > 6000) return
        runCatching {
            frame.acquirePointCloud().use { pc ->
                val b = pc.points
                while (b.remaining() >= 4) {
                    val x = b.get(); val y = b.get(); val z = b.get(); val conf = b.get()
                    if (conf < 0.3f) continue
                    val key = (Math.round(x / 0.05f).toLong() and 0x1fffff shl 42) or (Math.round(y / 0.05f).toLong() and 0x1fffff shl 21) or (Math.round(z / 0.05f).toLong() and 0x1fffff)
                    if (!cloud.containsKey(key)) cloud[key] = Vec3(x.toDouble(), y.toDouble(), z.toDouble())
                }
            }
        }
    }

    private fun refineSometimes(now: Double) {
        if (plan == null || now - lastRefine < (if (phase == Phase.CAPTURE) 6.0 else 3.0) || cloud.size < 150) return
        lastRefine = now
        val every = cloud.size / REFINE_POINTS + 1      // enough of them to fit to, few enough not to hold a frame up
        val points = cloud.values.filterIndexed { i, _ -> i % every == 0 }
        worker.execute {
            val r = synchronized(lock) { core.refine(points) }
            if (r != null) { if (phase == Phase.CONFIRM) note = " Fitted to ${r.matched} points the phone tracks, ${"%.1f".format(r.rmsM * 100)} cm off."; levelsDirty = true }
        }
    }

    // ---- a photo -----------------------------------------------------------------------------------------------------
    private fun takeStill(frame: Frame, pose: CameraPose, exposure: Double, gyro: Double, now: Double) {
        val image: Image = try { frame.acquireCameraImage() } catch (e: NotYetAvailableException) { busy.set(false); return } catch (e: Exception) { busy.set(false); return }
        val w = image.width; val h = image.height
        val nv21 = try { toNv21(image) } finally { image.close() }
        val sample = sensors.sample()
        val iso = runCatching { frame.imageMetadata.getInt(ImageMetadata.SENSOR_SENSITIVITY).toDouble() }.getOrNull()
        val focus = runCatching { frame.imageMetadata.getFloat(ImageMetadata.LENS_FOCUS_DISTANCE).toDouble() }.getOrNull()?.takeIf { it > 0 }?.let { 1 / it }
        val unix = System.currentTimeMillis() / 1000.0
        worker.execute {
            try {
                val stats = Quality.imageStats(nv21, w, h, w)       // NV21 starts with its brightness, a byte a pixel
                val out = synchronized(lock) { core.shot(ShotIn(pose, sample, stats, exposure, iso, focus, true, arSession, now, unix, sensorOrientation, gyro)) }
                if (out.kept) {
                    val file = File(File(dir, "photos"), out.record.name)
                    FileOutputStream(file).use { YuvImage(nv21, ImageFormat.NV21, w, h, null).compressToJpeg(Rect(0, 0, w, h), 93, it) }
                    writeExif(file, out.userComment, sample, pose.lens, unix)
                    save()
                    note = ""
                    levelsDirty = true
                } else {
                    rejected++
                    note = " Not kept: ${out.why.joinToString("; ")}."
                }
            } catch (e: Exception) {
                note = " A photo couldn't be saved: ${e.message}"
            } finally { busy.set(false) }
        }
    }

    private fun toNv21(image: Image): ByteArray {
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

    private fun writeExif(file: File, comment: String, s: sh.boulderme.core.SensorSample, lens: Lens, unix: Double) {
        val exif = ExifInterface(file)
        exif.setAttribute(ExifInterface.TAG_USER_COMMENT, comment)
        // the pixels are as the sensor read them, which is what the server's tools use; this says how to show them upright
        exif.setAttribute(ExifInterface.TAG_ORIENTATION, (if (sensorOrientation == 270) ExifInterface.ORIENTATION_ROTATE_270 else ExifInterface.ORIENTATION_ROTATE_90).toString())
        exif.setAttribute(ExifInterface.TAG_MAKE, Build.MANUFACTURER); exif.setAttribute(ExifInterface.TAG_MODEL, Build.MODEL)
        exif.setAttribute(ExifInterface.TAG_SOFTWARE, "BoulderMesh ${BuildInfo.VERSION}")
        exif.setAttribute(ExifInterface.TAG_FOCAL_LENGTH_IN_35MM_FILM, Math.round(lens.fx / lens.w * 36.0).toString())
        exif.setAttribute(ExifInterface.TAG_DATETIME_ORIGINAL, java.text.SimpleDateFormat("yyyy:MM:dd HH:mm:ss", java.util.Locale.US).format(java.util.Date((unix * 1000).toLong())))
        s.gps?.let { g ->
            exif.setLatLong(g.lat, g.lng)
            g.alt?.let { exif.setAltitude(it) }
            exif.setAttribute(ExifInterface.TAG_GPS_H_POSITIONING_ERROR, "${Math.round(g.accM * 100)}/100")
        }
        s.headingDeg?.let {
            exif.setAttribute(ExifInterface.TAG_GPS_IMG_DIRECTION, "${Math.round(it * 100)}/100")
            exif.setAttribute(ExifInterface.TAG_GPS_IMG_DIRECTION_REF, "T")
        }
        exif.saveAttributes()
    }

    companion object { const val REFINE_POINTS = 2000 }

    private fun push(text: String? = null, tracking: Boolean? = null, now: Double = sensors.now(), pairs: Int? = null, force: Boolean = false) {
        val (kept, auto) = synchronized(lock) { core.shotCount to core.auto }
        ui = ui.copy(phase = phase, text = (text ?: ui.text), tracking = tracking ?: ui.tracking, kept = kept, rejected = rejected, note = note.trim(),
            auto = auto, wide = wide, marking = marking, pairs = pairs ?: ui.pairs)
        if (force || now - lastUi > 0.12) { lastUi = now; val snapshot = ui; main.post { onUi(snapshot) } }
    }
}
