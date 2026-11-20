package sh.boulderme.app

import android.app.Activity
import android.content.Context
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
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
import sh.boulderme.core.Locate
import sh.boulderme.core.Locator
import sh.boulderme.core.PhoneLocate
import sh.boulderme.core.Axes
import sh.boulderme.core.Blobs
import sh.boulderme.core.HoldModel
import sh.boulderme.core.Picture
import sh.boulderme.core.CameraPose
import sh.boulderme.core.CaptureFlow
import sh.boulderme.core.Reopen
import sh.boulderme.core.Features
import sh.boulderme.core.LICENCE
import sh.boulderme.core.CaptureSession
import sh.boulderme.core.CaptureUi
import sh.boulderme.core.GuideMesh
import sh.boulderme.core.ImuClock
import sh.boulderme.core.Lens
import sh.boulderme.core.Plan
import sh.boulderme.core.Quality
import sh.boulderme.core.Quat
import sh.boulderme.core.ShotIn
import sh.boulderme.core.Vec3
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.Executors
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10

/**
 * A capture on the phone: ARCore tracks where the phone is, the shared CaptureFlow says what to draw, what to say and
 * when to take a photo, and judges each one; this feeds it ARCore's frames and taps, draws the mesh and its targets
 * over the camera's picture, and saves the photos, each with its readings in its EXIF.
 *
 * Photos are the frames ARCore itself reads from the camera (it gives no larger while it tracks): 1920 x 1080 on most
 * phones, so a hold needs photographing from nearer than a phone's full-size photo would.
 */
class ArController(
    private val activity: Activity, private val store: Store, private val sensors: PhoneSensors,
    private val plan: Plan?, guide: GuideMesh?, private val name: String, set: String?,
    reopen: String? = null,         // the id of a capture finished earlier, to carry on (Reopen)
    private val locators: List<Pair<String, Locator>> = emptyList(),   // what places frames against the mesh, by name ("phone", "server")
    gym: Boolean = false,           // a gym climb: its holds found by their colour (CaptureFlow's gym)
    private val onUi: (CaptureUi) -> Unit,
) : GLSurfaceView.Renderer {
    val id: String = reopen ?: (java.lang.Long.toString(System.currentTimeMillis() / 1000, 36) + (100..999).random())
    private val dir = store.captureDir(id)
    init { plan?.let { store.keepPlan(id, it.mesh) } }
    private val guide = guide.takeIf { plan != null }
    private val core = CaptureSession(plan, this.guide, id, BuildInfo.APP, "${Build.MANUFACTURER} ${Build.MODEL}", System.currentTimeMillis() / 1000.0)
    private val flow = CaptureFlow(core.apply { licence = LICENCE; credit = store.credit.ifBlank { null } }, set, store.anonFirst, store.levelLimit, store.levelTilts, gym = gym && plan == null,
        panorama = !(gym && plan == null) || store.gymPanorama, sceneryOn = !(gym && plan == null) || store.gymScenery)
        .apply { onSettings = { limit, tilts -> store.levelLimit = limit; store.levelTilts = tilts } }
    init {
        if (reopen != null) flow.reopen(Reopen.record(File(dir, "capture.json").readText()) ?: error("the record of $reopen can't be read"),
            File(dir, "points.bin").takeIf { it.exists() }?.readBytes(), File(dir, "capture-log.jsonl").takeIf { it.exists() }?.readText() ?: "")
    }
    init { flow.canLocate = plan != null && locators.isNotEmpty() }
    private val locating = locators.map { Executors.newSingleThreadExecutor() }      // one thread each, and a frame only to one that's free
    private val locBusy = locators.map { java.util.concurrent.atomic.AtomicBoolean(false) }
    private val lock = Any()        // round every use of the flow: frames are drawn on one thread, photos judged on another
    private val gymOn = flow.isGym
    private val holdFinder: HoldFinder? = if (gymOn) HoldFinder.load(activity) else null      // B, where the APK has the model
    init { flow.gymModel = holdFinder != null }
    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private val sounds = Sounds()
    private val dingKept = store.dingKept; private val tickSame = store.tickSame
    private var session: Session? = null
    private var installAsked = false
    private var background: Background? = null
    private var guideRenderer: GuideRenderer? = null
    private var markerRenderer: MarkerRenderer? = null
    private val view = FloatArray(16); private val proj = FloatArray(16); private val viewProj = FloatArray(16); private val mvp = FloatArray(16)
    private var width = 1; private var height = 1
    private var sensorOrientation = 90
    private val arSession = flow.arBase ?: ("a" + (System.nanoTime() % 100000))
    @Volatile private var pendingTap: FloatArray? = null

    /** How the screen is turned from the way the phone is naturally held, in quarter turns (Surface.ROTATION_*). */
    @Suppress("DEPRECATION")
    private fun rotation(): Int = activity.windowManager.defaultDisplay.rotation

    /** How far clockwise the camera's picture is turned to be shown upright on the screen as it is now. */
    private fun uprightDeg(): Int = ((sensorOrientation - rotation() * 90) % 360 + 360) % 360
    private var lastUi = 0.0
    private var frames = 0
    private val imu = ImuClock()        // the accelerometer's readings, lined up with the frames: drawing thread only
    private var clockSaid = 0
    private var clockUsed = 0.0
    private var exposure = UNKNOWN_EXPOSURE_S      // the shutter time last read: asked for now and then, as each asking holds something of ARCore's

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
                    updateMode = Config.UpdateMode.BLOCKING    // a frame drawn for each the camera gives, and none twice
                    planeFindingMode = Config.PlaneFindingMode.HORIZONTAL_AND_VERTICAL
                    if (s.isDepthModeSupported(Config.DepthMode.AUTOMATIC)) depthMode = Config.DepthMode.AUTOMATIC     // taps then land on bare rock
                })
                val cfg = s.cameraConfig
                synchronized(lock) {
                    flow.event("camera", "device" to "${Build.MANUFACTURER} ${Build.MODEL}", "android" to Build.VERSION.SDK_INT, "app" to BuildInfo.APP,
                        "image" to "${cfg.imageSize.width}x${cfg.imageSize.height}", "texture" to "${cfg.textureSize.width}x${cfg.textureSize.height}",
                        "fps" to "${cfg.fpsRange.lower}-${cfg.fpsRange.upper}", "depth_sensor" to cfg.depthSensorUsage.name,
                        "depth" to s.isDepthModeSupported(Config.DepthMode.AUTOMATIC), "configs" to s.getSupportedCameraConfigs(CameraConfigFilter(s)).size)
                }
                sensorOrientation = runCatching {
                    (activity.getSystemService(Context.CAMERA_SERVICE) as CameraManager).getCameraCharacteristics(s.cameraConfig.cameraId).get(CameraCharacteristics.SENSOR_ORIENTATION)
                }.getOrNull() ?: 90
                session = s
            }
            session?.resume()
            return null
        } catch (e: Exception) {
            synchronized(lock) { flow.event("start_failed", "what" to e.javaClass.simpleName, "message" to e.message) }
            return when (e.javaClass.simpleName) {
                "UnavailableDeviceNotCompatibleException" -> "This phone can't track its own position (it doesn't support ARCore), which guided capture needs."
                "UnavailableUserDeclinedInstallationException", "UnavailableArcoreNotInstalledException" -> "Guided capture needs Google Play Services for AR installed."
                "CameraNotAvailableException" -> "The camera is in use by something else."
                else -> "Couldn't start tracking: ${e.message ?: e.javaClass.simpleName}"
            }
        }
    }

    fun pause() { session?.pause() }
    /** Lets the camera go. Closing ARCore's session takes seconds, so it's done after the photos still being saved, off the main thread. */
    fun close() {
        val s = session
        session = null
        worker.execute { s?.close(); sounds.release() }
        worker.shutdown()
        locating.forEach { it.shutdown() }
    }

    fun tap(x: Float, y: Float) { pendingTap = floatArrayOf(x, y) }     // met with the world on the drawing thread, in its next frame

    // the small model turned, zoomed and moved by fingers, as the site's viewer does a mesh: on the drawing thread too,
    // where the screen's rays are known
    private val gestures = java.util.concurrent.ConcurrentLinkedQueue<() -> Unit>()
    fun modelShown() = synchronized(lock) { flow.modelShown }
    /** One finger dragged by dx, dy pixels: a drag the screen's height is a whole turn. */
    fun orbit(dx: Float, dy: Float) { gestures.add { if (height > 0) flow.orbitGhost(360.0 * dx / height, 360.0 * dy / height) } }
    fun zoom(factor: Float, x: Float, y: Float) { gestures.add { val (from, along) = ray(x, y); flow.zoomGhost(factor.toDouble(), from, along) } }
    /** Two fingers whose middle moved from x0, y0 to x1, y1. */
    fun pan(x0: Float, y0: Float, x1: Float, y1: Float) { gestures.add { val (from, now) = ray(x1, y1); flow.panGhost(from, ray(x0, y0).second, now) } }
    fun skipLevel() = synchronized(lock) { flow.skipLevel() }
    fun skipWarning(): String = synchronized(lock) { flow.skipWarning() }
    fun looser() = synchronized(lock) { flow.looser() }
    fun stricter() = synchronized(lock) { flow.stricter() }
    fun turnAgain() = synchronized(lock) { flow.turnAgain() }
    fun dismissDownNote() = synchronized(lock) { flow.dismissDownNote() }
    fun shutter() = synchronized(lock) { flow.shutter() }
    fun setAuto(on: Boolean) = synchronized(lock) { flow.setAuto(on) }
    fun skip() = synchronized(lock) { flow.skip() }
    fun toggleMarking() = synchronized(lock) { flow.toggleMarking() }
    fun gymTap(mode: String) = synchronized(lock) { flow.gymTap(mode) }
    fun gymWider() = synchronized(lock) { flow.gymWider() }
    fun gymNarrower() = synchronized(lock) { flow.gymNarrower() }
    fun gymFinder(f: String) = synchronized(lock) { flow.gymFinder(f) }
    fun toggleWide() = synchronized(lock) { flow.toggleWide() }
    fun toggleCard() = synchronized(lock) { flow.toggleCard() }
    val sceneryOn: Boolean get() = flow.sceneryOn
    fun startScenery() = synchronized(lock) { flow.startScenery()?.let { flow.say(it) } }
    fun endScenery() = synchronized(lock) { flow.endScenery() }
    fun more(yes: Boolean) = synchronized(lock) { flow.more(yes) }
    fun turnGhost(degrees: Double) = synchronized(lock) { flow.turnGhost(degrees) }
    fun bringGhost() = synchronized(lock) { flow.bringGhost() }
    fun confirmFit() = synchronized(lock) { flow.confirmFit() }
    fun restartAlign() = synchronized(lock) { flow.restartAlign() }
    fun addSpot() = synchronized(lock) { flow.addSpot() }
    fun blockSide() = synchronized(lock) { flow.blockSide() }
    fun newRing() = synchronized(lock) { flow.newRing() }

    /** Writes the capture's record and what's to upload beside its photos: after each photo kept, so that one cut short isn't lost. */
    private fun save(last: Boolean = false): Int = synchronized(lock) {
        core.place = sensors.placed()
        File(dir, "capture.json").writeText(core.record(System.currentTimeMillis() / 1000.0).json())
        File(dir, "capture-log.jsonl").writeText(flow.logText())
        // and the points ARCore tracked, for whatever places the photos on the server: now and then, and at the end
        if (last || core.shotCount % 8 == 1) File(dir, "points.bin").writeBytes(flow.points())
        store.saveUpload(id, (store.upload(id) ?: Store.Upload(name = name)).copy(name = name, mesh = plan?.mesh, photos = core.shotCount))     // a capture carried on keeps its project
        core.shotCount
    }

    /** Ends the capture. Returns how many photos were kept. */
    fun finish(): Int = save(last = true)

    // ---- drawing, on the GL thread -----------------------------------------------------------------------------------
    override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
        GLES20.glClearColor(0.05f, 0.05f, 0.05f, 1f)
        background = Background()
        guideRenderer = guide?.let { GuideRenderer(it) }
        markerRenderer = MarkerRenderer()
    }

    override fun onSurfaceChanged(gl: GL10?, w: Int, h: Int) {
        width = w; height = h
        GLES20.glViewport(0, 0, w, h)
        session?.setDisplayGeometry(rotation(), w, h)
    }

    override fun onDrawFrame(gl: GL10?) {
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT or GLES20.GL_DEPTH_BUFFER_BIT)
        val s = session ?: return
        val bg = background ?: return
        val frame = try { s.setCameraTextureName(bg.texture); s.setDisplayGeometry(rotation(), width, height); s.update() } catch (e: Exception) { return }
        bg.draw(frame)
        val camera = frame.camera
        val tracking = camera.trackingState == TrackingState.TRACKING
        val now = sensors.now()
        val pose = corePose(camera)
        val tap = pendingTap
        pendingTap = null
        if (tracking) {
            camera.getViewMatrix(view, 0)
            camera.getProjectionMatrix(proj, 0, 0.05f, 100f)
            Matrix.multiplyMM(viewProj, 0, proj, 0, view, 0)
            collect(frame)
        }
        if (frames++ % 10 == 0) runCatching { frame.imageMetadata.getLong(ImageMetadata.SENSOR_EXPOSURE_TIME) / 1e9 }.getOrNull()?.takeIf { it > 0 }?.let { exposure = it }
        val exposure = exposure
        val gyro = sensors.gyro?.norm ?: 0.0
        // the accelerometer as it read when this frame's picture was taken, not as it reads now
        sensors.drain(imu)
        val shotAt = runCatching { frame.androidCameraTimestamp / 1e9 }.getOrNull()?.takeIf { kotlin.math.abs(it - now) < 1.0 } ?: now
        val clocked = if (tracking) imu.frame(shotAt, pose.rot) else null
        val accel = imu.accelAt(shotAt) ?: sensors.accel
        val out = synchronized(lock) {
            // the first few tries, and then each time what's used changes: not a line every two seconds
            clocked?.takeIf { clockSaid++ < 5 || kotlin.math.abs(it.usedS - clockUsed) > 0.002 }?.let { clockUsed = it.usedS; flow.event("imu_clock", "offset_s" to it.offsetS, "fit_rad_s" to it.fitRadS, "motion_rad_s" to it.motionRadS, "taken" to it.taken,
                "used_s" to it.usedS, "newest_after_s" to it.lagS, "frame_age_s" to now - shotAt) }
            if (tap != null && tracking) { val (from, along) = ray(tap[0], tap[1]); flow.tap(from, along, if (gymOn) ({ picture(frame, pose) }) else null) { hit(frame, tap) } }
            while (true) { val g = gestures.poll() ?: break; if (tracking) g() }
            // the slow half of tightening the fit holds no lock: frames go on being drawn meanwhile
            flow.refineDue(now)?.let { work -> worker.execute { val found = flow.tighter(work); synchronized(lock) { flow.tightened(work, found) } } }
            flow.frame(pose, tracking, gyro, exposure, now, uprightDeg(), accel, sensorOrientation)
        }
        out.levels?.let { guideRenderer?.setLevels(it) }
        out.guide?.let { fit ->
            Matrix.multiplyMM(mvp, 0, viewProj, 0, fit.matrix(), 0)
            guideRenderer?.draw(mvp, out.alpha, out.solid)
        }
        markerRenderer?.draw(out.markers, viewProj, height * proj[5], plan)       // a metre across at a metre off, in pixels
        if (out.tick && tickSame) sounds.tick()
        if (out.shoot) takeStill(frame, pose, exposure, gyro, now)
        else out.grab?.let { if (it == "locate") locateGrab(frame, pose) else grabFrame(frame, pose, exposure, gyro, now, it) }
        if (now - lastUi > 0.12) { lastUi = now; main.post { onUi(out.ui) } }
    }

    private fun corePose(camera: com.google.ar.core.Camera): CameraPose {
        val p = camera.pose
        val q = p.rotationQuaternion       // x, y, z, w
        val k = camera.imageIntrinsics
        val f = k.focalLength; val c = k.principalPoint; val d = k.imageDimensions
        return CameraPose.fromGl(Vec3(p.tx().toDouble(), p.ty().toDouble(), p.tz().toDouble()), Quat(q[3].toDouble(), q[0].toDouble(), q[1].toDouble(), q[2].toDouble()),
            Lens(f[0].toDouble(), f[1].toDouble(), c[0].toDouble(), c[1].toDouble(), d[0], d[1]))
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

    /** The camera's picture now, small and in colour, upright as a photo is stored, with the camera as that picture has it: for a tap on a gym climb's hold. */
    private fun picture(frame: Frame, pose: CameraPose): Pair<Picture, CameraPose>? {
        val image: Image = try { frame.acquireCameraImage() } catch (e: Exception) { return null }
        val w = image.width; val h = image.height
        val nv21 = try { toNv21(image) } finally { image.close() }
        val turn = Axes.uprightTurn(Axes.downInImage(pose), uprightDeg())
        return Picture.ofYuv(nv21, w, nv21, w * h, w, w, h, vFirst = true, turn = turn) to pose.turned(turn)
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

    /** Hands the points ARCore tracks to the flow: for tightening the mesh's fit against, and to send with the capture. */
    private fun collect(frame: Frame) {
        runCatching {
            frame.acquirePointCloud().use { pc ->
                val b = pc.points
                synchronized(lock) {
                    var n = 0
                    while (b.remaining() >= 4) {
                        val x = b.get(); val y = b.get(); val z = b.get(); val conf = b.get()
                        if (conf >= 0.3f) { flow.addPoint(x.toDouble(), y.toDouble(), z.toDouble()); n++ }
                    }
                    flow.tracked(n)
                }
            }
        }
    }

    // ---- a photo -----------------------------------------------------------------------------------------------------
    private fun takeStill(frame: Frame, pose: CameraPose, exposure: Double, gyro: Double, now: Double) {
        val image: Image = try { frame.acquireCameraImage() } catch (e: Exception) {
            synchronized(lock) { flow.noShot(if (e is NotYetAvailableException) null else "The camera gave no picture: ${e.message}") }
            return
        }
        val w = image.width; val h = image.height
        val nv21 = try { toNv21(image) } finally { image.close() }
        val sample = sensors.sample()
        val meta = runCatching { frame.imageMetadata }.getOrNull()
        val shutter = meta?.let { m -> runCatching { m.getLong(ImageMetadata.SENSOR_EXPOSURE_TIME) / 1e9 }.getOrNull() }?.takeIf { it > 0 }?.also { this.exposure = it } ?: exposure
        val iso = meta?.let { m -> runCatching { m.getInt(ImageMetadata.SENSOR_SENSITIVITY).toDouble() }.getOrNull() }
        val focus = meta?.let { m -> runCatching { m.getFloat(ImageMetadata.LENS_FOCUS_DISTANCE).toDouble() }.getOrNull() }?.takeIf { it > 0 }?.let { 1 / it }
        val upright = uprightDeg()
        val unix = System.currentTimeMillis() / 1000.0
        worker.execute {
            try {
                val stats = Quality.imageStats(nv21, w, h, w)       // NV21 starts with its brightness, a byte a pixel
                // stored upright (the sensor reads its picture out on its side), with the camera and its lens as that picture has them
                val turn = Axes.uprightTurn(Axes.downInImage(pose), upright)
                val stored = pose.turned(turn)
                val features = runCatching { Features.of(nv21, w, h, w, turn) }.getOrNull()     // for a warning if it won't match the photos near it
                // a gym climb's: its patches of one colour (A), and those within the holds the model finds (B), to find the climb's holds in
                val pic = if (gymOn) runCatching { Picture.ofYuv(nv21, w, nv21, w * h, w, w, h, vFirst = true, turn = turn) }.getOrNull() else null
                val blobs = pic?.let { runCatching { Blobs.find(it) }.getOrNull() }
                val modelBlobs = pic?.let { p -> holdFinder?.let { HoldModel.blobs(p, it) } }
                val out = synchronized(lock) { flow.shot(ShotIn(stored, sample, stats, shutter, iso, focus, true, arSession, now, unix, sensorOrientation, gyro, turn,
                    features = features, blobs = blobs, modelBlobs = modelBlobs)) }
                if (out.kept) {
                    if (dingKept) sounds.ding()
                    val file = File(File(dir, "photos"), out.record.name)
                    val pixels = turnNv21(nv21, w, h, turn)
                    val (sw, sh) = if (turn == 90 || turn == 270) h to w else w to h
                    FileOutputStream(file).use { YuvImage(pixels, ImageFormat.NV21, sw, sh, null).compressToJpeg(Rect(0, 0, sw, sh), 93, it) }
                    writeExif(file, out.userComment, sample, stored.lens, unix)
                    save()
                }
            } catch (e: Exception) {
                synchronized(lock) { flow.say("A photo couldn't be saved: ${e.message}") }
            }
        }
    }

    /**
     * Keeps the frame the tracking has now for the scenery round the rock (CaptureFlow's grab): stored upright as a photo
     * is, in the capture's scenery/, never with its photos.
     */
    private fun grabFrame(frame: Frame, pose: CameraPose, exposure: Double, gyro: Double, now: Double, kind: String) {
        val image: Image = try { frame.acquireCameraImage() } catch (e: Exception) {
            synchronized(lock) { flow.noGrab(e.javaClass.simpleName) }
            return
        }
        val w = image.width; val h = image.height
        val nv21 = try { toNv21(image) } finally { image.close() }
        val upright = uprightDeg()
        val unix = System.currentTimeMillis() / 1000.0
        worker.execute {
            try {
                val stats = Quality.imageStats(nv21, w, h, w)
                val turn = Axes.uprightTurn(Axes.downInImage(pose), upright)
                val stored = pose.turned(turn)
                val shot = synchronized(lock) { flow.scenery(kind, stored, now, arSession, turn, stats, gyro, exposure) }
                val folder = File(dir, "scenery").apply { mkdirs() }
                val file = File(folder, shot.name)
                val pixels = turnNv21(nv21, w, h, turn)
                val (sw, sh) = if (turn == 90 || turn == 270) h to w else w to h
                FileOutputStream(file).use { YuvImage(pixels, ImageFormat.NV21, sw, sh, null).compressToJpeg(Rect(0, 0, sw, sh), SCENERY_QUALITY, it) }
                ExifInterface(file).apply {
                    setAttribute(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL.toString())
                    setAttribute(ExifInterface.TAG_MAKE, Build.MANUFACTURER); setAttribute(ExifInterface.TAG_MODEL, Build.MODEL)
                    setAttribute(ExifInterface.TAG_DATETIME_ORIGINAL, java.text.SimpleDateFormat("yyyy:MM:dd HH:mm:ss", java.util.Locale.US).format(java.util.Date((unix * 1000).toLong())))
                    saveAttributes()
                }
                if (kind == "walk" && synchronized(lock) { flow.session.sceneryCount } % 20 == 1) save()
            } catch (e: Exception) {
                synchronized(lock) { flow.noGrab("${e.javaClass.simpleName}: ${e.message}") }
            }
        }
    }

    /** Hands the frame the tracking has now to each Locator that isn't busy with another (CaptureFlow.locateFrame()). */
    private fun locateGrab(frame: Frame, pose: CameraPose) {
        val image: Image = try { frame.acquireCameraImage() } catch (e: Exception) {
            synchronized(lock) { flow.noGrab(e.javaClass.simpleName) }
            return
        }
        val w = image.width; val h = image.height
        val grey = try {
            val y = image.planes[0]; val buf = y.buffer; val stride = y.rowStride
            ByteArray(w * h).also { out -> for (r in 0 until h) { buf.position(r * stride); buf.get(out, r * w, w) } }
        } finally { image.close() }
        val (small, sw, sh) = PhoneLocate.shrink(grey, w, h)
        val f = synchronized(lock) { flow.locateFrame(small, sw, sh, pose) }
        val p = plan ?: return
        for ((i, named) in locators.withIndex()) {
            if (!locBusy[i].compareAndSet(false, true)) continue
            locating[i].execute {
                Locate.start(named.second, p, listOf(f)) { found ->
                    locBusy[i].set(false)
                    synchronized(lock) { flow.located(found, named.first) }
                }
            }
        }
    }

    /** NV21 pixels w wide and h high, turned clockwise by a quarter (90), a half or three quarters: then h wide and w high, for a quarter. */
    private fun turnNv21(src: ByteArray, w: Int, h: Int, deg: Int): ByteArray {
        if (deg != 90 && deg != 180 && deg != 270) return src
        val out = ByteArray(src.size)
        val nw = if (deg == 180) w else h
        val cw = w / 2; val ch = h / 2; val ncw = nw / 2
        val chroma = w * h      // where the colour starts: pairs of bytes, one pair to four pixels
        when (deg) {
            90 -> {     // (x, y) goes to (h - 1 - y, x)
                for (y in 0 until h) { val nx = h - 1 - y; var from = y * w; for (x in 0 until w) out[x * nw + nx] = src[from++] }
                for (y in 0 until ch) { val nx = ch - 1 - y; var from = chroma + y * cw * 2; for (x in 0 until cw) { val to = chroma + (x * ncw + nx) * 2; out[to] = src[from++]; out[to + 1] = src[from++] } }
            }
            180 -> {    // (x, y) goes to (w - 1 - x, h - 1 - y)
                for (i in 0 until w * h) out[w * h - 1 - i] = src[i]
                val pairs = cw * ch
                for (i in 0 until pairs) { val to = chroma + (pairs - 1 - i) * 2; out[to] = src[chroma + i * 2]; out[to + 1] = src[chroma + i * 2 + 1] }
            }
            else -> {   // (x, y) goes to (y, w - 1 - x)
                for (y in 0 until h) { var from = y * w; for (x in 0 until w) out[(w - 1 - x) * nw + y] = src[from++] }
                for (y in 0 until ch) { var from = chroma + y * cw * 2; for (x in 0 until cw) { val to = chroma + ((cw - 1 - x) * ncw + y) * 2; out[to] = src[from++]; out[to + 1] = src[from++] } }
            }
        }
        return out
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
        exif.setAttribute(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL.toString())     // its pixels are upright as stored
        exif.setAttribute(ExifInterface.TAG_MAKE, Build.MANUFACTURER); exif.setAttribute(ExifInterface.TAG_MODEL, Build.MODEL)
        exif.setAttribute(ExifInterface.TAG_SOFTWARE, "BoulderMesh ${BuildInfo.VERSION}")
        exif.setAttribute(ExifInterface.TAG_FOCAL_LENGTH_IN_35MM_FILM, Math.round(lens.fx / maxOf(lens.w, lens.h) * 36.0).toString())
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

    companion object {
        const val SCENERY_QUALITY = 85     // JPEG quality of the scenery's frames: a backdrop, not the rock
        const val UNKNOWN_EXPOSURE_S = 1.0 / 120     // until the camera says: about the longest it uses out of doors in daylight
    }
}
