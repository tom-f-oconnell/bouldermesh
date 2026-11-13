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
import sh.boulderme.core.CameraPose
import sh.boulderme.core.CaptureFlow
import sh.boulderme.core.CaptureSession
import sh.boulderme.core.CaptureUi
import sh.boulderme.core.GuideMesh
import sh.boulderme.core.Lens
import sh.boulderme.core.Plan
import sh.boulderme.core.Quality
import sh.boulderme.core.Quat
import sh.boulderme.core.RecordCalibration
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
    private val plan: Plan?, guide: GuideMesh?, private val name: String, private val onUi: (CaptureUi) -> Unit,
) : GLSurfaceView.Renderer {
    val id: String = java.lang.Long.toString(System.currentTimeMillis() / 1000, 36) + (100..999).random()
    private val dir = store.captureDir(id)
    private val guide = guide.takeIf { plan != null }
    private val core = CaptureSession(plan, this.guide, id, BuildInfo.APP, "${Build.MANUFACTURER} ${Build.MODEL}", System.currentTimeMillis() / 1000.0).apply {
        accelBias = store.accelBias
        calibration = store.accelBias?.let { RecordCalibration(accelBias = it.toList()) }
    }
    private val flow = CaptureFlow(core)
    private val lock = Any()        // round every use of the flow: frames are drawn on one thread, photos judged on another
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
    @Volatile private var pendingTap: FloatArray? = null

    /** How the screen is turned from the way the phone is naturally held, in quarter turns (Surface.ROTATION_*). */
    @Suppress("DEPRECATION")
    private fun rotation(): Int = activity.windowManager.defaultDisplay.rotation

    /** How far clockwise the camera's picture is turned to be shown upright on the screen as it is now. */
    private fun uprightDeg(): Int = ((sensorOrientation - rotation() * 90) % 360 + 360) % 360
    private var lastUi = 0.0

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
                sensorOrientation = runCatching {
                    (activity.getSystemService(Context.CAMERA_SERVICE) as CameraManager).getCameraCharacteristics(s.cameraConfig.cameraId).get(CameraCharacteristics.SENSOR_ORIENTATION)
                }.getOrNull() ?: 90
                session = s
            }
            session?.resume()
            return null
        } catch (e: Exception) {
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

    fun tap(x: Float, y: Float) { pendingTap = floatArrayOf(x, y) }     // met with the world on the drawing thread, in its next frame
    fun shutter() = synchronized(lock) { flow.shutter() }
    fun setAuto(on: Boolean) = synchronized(lock) { flow.setAuto(on) }
    fun skip() = synchronized(lock) { flow.skip() }
    fun toggleMarking() = synchronized(lock) { flow.toggleMarking() }
    fun toggleWide() = synchronized(lock) { flow.toggleWide() }
    fun turnGhost(degrees: Double) = synchronized(lock) { flow.turnGhost(degrees) }
    fun bringGhost() = synchronized(lock) { flow.bringGhost() }
    fun confirmFit() = synchronized(lock) { flow.confirmFit() }
    fun restartAlign() = synchronized(lock) { flow.restartAlign() }
    fun addSpot() = synchronized(lock) { flow.addSpot() }
    fun blockSide() = synchronized(lock) { flow.blockSide() }

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
        val exposure = runCatching { frame.imageMetadata.getLong(ImageMetadata.SENSOR_EXPOSURE_TIME) / 1e9 }.getOrNull() ?: (1.0 / 60)
        val gyro = sensors.gyro?.norm ?: 0.0
        val out = synchronized(lock) {
            if (tap != null && tracking) { val (from, along) = ray(tap[0], tap[1]); flow.tap(from, along) { hit(frame, tap) } }
            flow.refineDue(now)?.let { points -> worker.execute { synchronized(lock) { flow.refine(points) } } }
            flow.frame(pose, tracking, gyro, exposure, now, uprightDeg())
        }
        out.levels?.let { guideRenderer?.setLevels(it) }
        out.guide?.let { fit ->
            Matrix.multiplyMM(mvp, 0, viewProj, 0, fit.matrix(), 0)
            guideRenderer?.draw(mvp, out.alpha, out.solid)
        }
        markerRenderer?.draw(out.markers, viewProj, height * proj[5])       // a metre across at a metre off, in pixels
        if (out.shoot) takeStill(frame, pose, exposure, gyro, now)
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

    /** Hands the points ARCore tracks to the flow, for tightening the mesh's fit against. */
    private fun collect(frame: Frame) {
        if (plan == null) return
        runCatching {
            frame.acquirePointCloud().use { pc ->
                val b = pc.points
                synchronized(lock) {
                    while (b.remaining() >= 4) {
                        val x = b.get(); val y = b.get(); val z = b.get(); val conf = b.get()
                        if (conf >= 0.3f) flow.addPoint(x.toDouble(), y.toDouble(), z.toDouble())
                    }
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
        val iso = runCatching { frame.imageMetadata.getInt(ImageMetadata.SENSOR_SENSITIVITY).toDouble() }.getOrNull()
        val focus = runCatching { frame.imageMetadata.getFloat(ImageMetadata.LENS_FOCUS_DISTANCE).toDouble() }.getOrNull()?.takeIf { it > 0 }?.let { 1 / it }
        val unix = System.currentTimeMillis() / 1000.0
        worker.execute {
            try {
                val stats = Quality.imageStats(nv21, w, h, w)       // NV21 starts with its brightness, a byte a pixel
                val out = synchronized(lock) { flow.shot(ShotIn(pose, sample, stats, exposure, iso, focus, true, arSession, now, unix, sensorOrientation, gyro)) }
                if (out.kept) {
                    val file = File(File(dir, "photos"), out.record.name)
                    FileOutputStream(file).use { YuvImage(nv21, ImageFormat.NV21, w, h, null).compressToJpeg(Rect(0, 0, w, h), 93, it) }
                    writeExif(file, out.userComment, sample, pose.lens, unix)
                    save()
                }
            } catch (e: Exception) {
                synchronized(lock) { flow.say("A photo couldn't be saved: ${e.message}") }
            }
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
        exif.setAttribute(ExifInterface.TAG_ORIENTATION, when (uprightDeg()) {
            90 -> ExifInterface.ORIENTATION_ROTATE_90; 180 -> ExifInterface.ORIENTATION_ROTATE_180; 270 -> ExifInterface.ORIENTATION_ROTATE_270; else -> ExifInterface.ORIENTATION_NORMAL
        }.toString())
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

}
