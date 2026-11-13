package sh.boulderme.ios

import kotlinx.cinterop.CValue
import kotlinx.cinterop.cValue
import kotlinx.cinterop.get
import kotlinx.cinterop.readBytes
import kotlinx.cinterop.reinterpret
import kotlinx.cinterop.useContents
import kotlinx.cinterop.usePinned
import kotlinx.cinterop.addressOf
import kotlinx.cinterop.value
import kotlinx.cinterop.ObjCAction
import kotlinx.cinterop.memScoped
import kotlinx.cinterop.alloc
import kotlinx.cinterop.ptr
import kotlinx.cinterop.toKString
import platform.ARKit.ARCamera
import platform.ARKit.ARFrame
import platform.ARKit.ARPlaneDetectionHorizontal
import platform.ARKit.ARPlaneDetectionVertical
import platform.ARKit.ARRaycastResult
import platform.ARKit.ARRaycastTarget
import platform.ARKit.ARRaycastTargetAlignment
import platform.ARKit.ARSCNView
import platform.ARKit.ARSceneReconstructionMesh
import platform.ARKit.ARTrackingState
import platform.ARKit.ARWorldTrackingConfiguration
import platform.AVFoundation.AVCaptureDevice
import platform.AVFoundation.AVMediaTypeVideo
import platform.AVFoundation.authorizationStatusForMediaType
import platform.AVFoundation.setActiveMaxExposureDuration
import platform.CoreFoundation.CFDictionaryRef
import platform.CoreFoundation.CFRelease
import platform.CoreFoundation.CFRetain
import platform.CoreFoundation.CFStringCreateWithCString
import platform.CoreFoundation.CFStringRef
import platform.CoreFoundation.CFURLRef
import platform.CoreFoundation.kCFStringEncodingUTF8
import platform.CoreGraphics.CGAffineTransformMakeRotation
import platform.CoreGraphics.CGImageRelease
import platform.CoreGraphics.CGPointMake
import platform.CoreImage.CIContext
import platform.CoreImage.CIImage
import platform.CoreImage.createCGImage
import platform.CoreMedia.CMTimeMake
import platform.CoreVideo.CVPixelBufferGetBaseAddressOfPlane
import platform.CoreVideo.CVPixelBufferGetBytesPerRowOfPlane
import platform.CoreVideo.CVPixelBufferGetHeightOfPlane
import platform.CoreVideo.CVPixelBufferGetPlaneCount
import platform.CoreVideo.CVPixelBufferGetWidthOfPlane
import platform.CoreVideo.CVPixelBufferLockBaseAddress
import platform.CoreVideo.CVPixelBufferRef
import platform.CoreVideo.CVPixelBufferUnlockBaseAddress
import platform.CoreVideo.kCVPixelBufferLock_ReadOnly
import platform.Foundation.CFBridgingRelease
import platform.Foundation.CFBridgingRetain
import platform.Foundation.NSData
import platform.Foundation.NSDate
import platform.Foundation.NSDateFormatter
import platform.Foundation.NSDictionary
import platform.Foundation.NSNumber
import platform.Foundation.dictionaryWithDictionary
import platform.Foundation.numberWithDouble
import platform.Foundation.numberWithInt
import platform.Foundation.NSSelectorFromString
import platform.Foundation.NSTimer
import platform.Foundation.NSURL
import platform.Foundation.dataWithBytes
import platform.Foundation.dateWithTimeIntervalSince1970
import platform.Foundation.timeIntervalSince1970
import platform.ImageIO.CGImageDestinationAddImage
import platform.ImageIO.CGImageDestinationCreateWithURL
import platform.ImageIO.CGImageDestinationFinalize
import platform.ImageIO.kCGImageDestinationLossyCompressionQuality
import platform.ImageIO.kCGImagePropertyExifDateTimeOriginal
import platform.ImageIO.kCGImagePropertyExifDictionary
import platform.ImageIO.kCGImagePropertyExifExposureTime
import platform.ImageIO.kCGImagePropertyExifFocalLenIn35mmFilm
import platform.ImageIO.kCGImagePropertyExifUserComment
import platform.ImageIO.kCGImagePropertyGPSAltitude
import platform.ImageIO.kCGImagePropertyGPSAltitudeRef
import platform.ImageIO.kCGImagePropertyGPSDictionary
import platform.ImageIO.kCGImagePropertyGPSHPositioningError
import platform.ImageIO.kCGImagePropertyGPSImgDirection
import platform.ImageIO.kCGImagePropertyGPSImgDirectionRef
import platform.ImageIO.kCGImagePropertyGPSLatitude
import platform.ImageIO.kCGImagePropertyGPSLatitudeRef
import platform.ImageIO.kCGImagePropertyGPSLongitude
import platform.ImageIO.kCGImagePropertyGPSLongitudeRef
import platform.ImageIO.kCGImagePropertyOrientation
import platform.ImageIO.kCGImagePropertyTIFFDictionary
import platform.ImageIO.kCGImagePropertyTIFFMake
import platform.ImageIO.kCGImagePropertyTIFFModel
import platform.ImageIO.kCGImagePropertyTIFFSoftware
import platform.SceneKit.SCNBillboardConstraint
import platform.SceneKit.SCNGeometry
import platform.SceneKit.SCNGeometryElement
import platform.SceneKit.SCNGeometryPrimitiveTypeTriangles
import platform.SceneKit.SCNGeometrySource
import platform.SceneKit.SCNGeometrySourceSemanticColor
import platform.SceneKit.SCNGeometrySourceSemanticVertex
import platform.SceneKit.SCNLightingModelConstant
import platform.SceneKit.SCNMatrix4
import platform.SceneKit.SCNNode
import platform.SceneKit.SCNScene
import platform.SceneKit.SCNTorus
import platform.SceneKit.SCNVector3Make
import platform.UIKit.NSLayoutConstraint
import platform.UIKit.UIColor
import platform.UIKit.UIDevice
import platform.UIKit.UILabel
import platform.UIKit.UILayoutConstraintAxisVertical
import platform.UIKit.UIProgressView
import platform.UIKit.UIProgressViewStyle
import platform.UIKit.UIStackView
import platform.UIKit.UITapGestureRecognizer
import platform.UIKit.UIView
import platform.UIKit.UIViewController
import platform.UIKit.NSTextAlignmentCenter
import platform.UIKit.UIApplication
import platform.UIKit.UIFont
import platform.darwin.dispatch_async
import platform.darwin.dispatch_get_global_queue
import platform.darwin.dispatch_get_main_queue
import platform.darwin.DISPATCH_QUEUE_PRIORITY_DEFAULT
import platform.darwin.simd_float4x4
import platform.posix.uname
import platform.posix.utsname
import sh.boulderme.core.CameraPose
import sh.boulderme.core.CaptureFlow
import sh.boulderme.core.CaptureSession
import sh.boulderme.core.CaptureUi
import sh.boulderme.core.Coverage
import sh.boulderme.core.GuideMesh
import sh.boulderme.core.ImageStats
import sh.boulderme.core.Lens
import sh.boulderme.core.Marker
import sh.boulderme.core.Phase
import sh.boulderme.core.Quality
import sh.boulderme.core.Quat
import sh.boulderme.core.SensorSample
import sh.boulderme.core.ShotIn
import sh.boulderme.core.Similarity
import sh.boulderme.core.UploadState
import sh.boulderme.core.Vec3
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.hypot
import kotlin.math.roundToInt
import kotlin.native.runtime.GC
import kotlin.native.runtime.NativeRuntimeApi

/** RGB for each Coverage.LEVEL_*: grey for what doesn't count, red to do, amber part done, green done, blue-grey skipped. */
private val LEVEL_RGB = arrayOf(floatArrayOf(0.55f, 0.55f, 0.55f), floatArrayOf(0.90f, 0.22f, 0.18f), floatArrayOf(0.96f, 0.68f, 0.16f),
    floatArrayOf(0.20f, 0.78f, 0.38f), floatArrayOf(0.40f, 0.50f, 0.72f))

private fun CFStringRef?.text(): String = CFBridgingRelease(CFRetain(this)) as String

private fun CValue<simd_float4x4>.column(i: Int): Vec3 = useContents { columns[i].value.let { Vec3(it.getFloatAt(0).toDouble(), it.getFloatAt(1).toDouble(), it.getFloatAt(2).toDouble()) } }

/** A camera as ARKit has it (x right, y up, z towards the viewer, relative to the picture as read out), for the core. */
private fun poseOf(camera: ARCamera): CameraPose {
    val t = camera.transform
    val k = camera.intrinsics.useContents { doubleArrayOf(columns[0].value.getFloatAt(0).toDouble(), columns[1].value.getFloatAt(1).toDouble(), columns[2].value.getFloatAt(0).toDouble(), columns[2].value.getFloatAt(1).toDouble()) }
    val (w, h) = camera.imageResolution.useContents { width.toInt() to height.toInt() }
    return CameraPose.fromGl(t.column(3), Quat.fromAxes(t.column(0), t.column(1), t.column(2)), Lens(k[0], k[1], k[2], k[3], w, h))
}

/** The camera reads its picture out on its side: a quarter turn clockwise shows it upright, with the phone held upright. */
private const val UPRIGHT_DEG = 90

private fun machine(): String = memScoped { val u = alloc<utsname>(); uname(u.ptr); u.machine.toKString() }

/**
 * A capture on the phone: ARKit tracks where the phone is, the shared CaptureFlow says what to draw, what to say and
 * when to take a photo, and judges each one; this feeds it ARKit's frames and taps, draws the mesh and its targets over
 * the camera's picture with SceneKit, and saves the photos, each with its readings in its EXIF.
 *
 * ARKit's frames are read a dozen times a second by a timer rather than taken as it hands them out, and Kotlin's
 * memory is swept after each: a frame Kotlin has seen is kept until then, and ARKit stops giving pictures once many are.
 */
@OptIn(NativeRuntimeApi::class)
class CaptureController(private val app: App, mesh: String?, private val name: String, private val done: (String?) -> Unit) : UIViewController(nibName = null, bundle = null) {
    private val plan = mesh?.let { app.store.plan(it) }
    private val guide: GuideMesh? = if (plan != null && mesh != null) app.store.guide(mesh) else null
    val id: String = (NSDate().timeIntervalSince1970.toLong()).toString(36) + (100..999).random()
    private val dir = app.store.captureDir(id)
    private val core = CaptureSession(plan.takeIf { guide != null }, guide, id, IosStore.APP, machine(), NSDate().timeIntervalSince1970)
    private val flow = CaptureFlow(core)
    private val arSession = "a" + (100000..999999).random()
    private val sceneView = ARSCNView()
    private val scene = SCNScene()
    private val ciContext = CIContext.context()
    private val background = dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT.toLong(), 0u)
    private var timer: NSTimer? = null
    private var ticks = 0
    private var cameraSet = false
    // how wide the photos are, in pixels: wider than the frames ARKit tracks with where it gives full-size ones (guessed at
    // 12 megapixels until the first comes), so that what's asked for is what a photo will show, not what a frame does
    private var stillWidth: Int? = null
    private var failed: String? = if (mesh != null && guide == null) "This mesh's download can't be read. Remove it and get it again." else null

    // what's drawn in the world
    private val guideNode = SCNNode()
    private var levels: IntArray? = null
    private var positions: NSData? = null
    private val rings = HashMap<String, SCNNode>()
    private var drawn: List<Marker> = emptyList()

    // what's drawn over it
    private val hint = makeLabel("Starting the camera…", UIColor.whiteColor, bold = true, size = 18.0)
    private val note = makeLabel("", UIColor.whiteColor, size = 13.0)
    private val counts = makeLabel("", UIColor.whiteColor, size = 13.0)
    private val readings = makeLabel("", WARN, size = 13.0)       // what the last photo's readings lacked, which it was kept in spite of
    private val bar = UIProgressView(progressViewStyle = UIProgressViewStyle.UIProgressViewStyleDefault)
    private val arrow = makeLabel("➤", UIColor.whiteColor, bold = true, size = 72.0)
    private val buttons = UIStackView()
    private var buttonsFor = ""
    private var ui = CaptureUi()

    // ---- the screen ---------------------------------------------------------------------------------------------------
    override fun viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor.blackColor
        sceneView.translatesAutoresizingMaskIntoConstraints = false
        sceneView.automaticallyUpdatesLighting = false
        view.addSubview(sceneView)
        sceneView.addGestureRecognizer(UITapGestureRecognizer(target = this, action = NSSelectorFromString("tapped:")))
        sceneView.scene = scene
        scene.rootNode.addChildNode(guideNode)
        guideNode.hidden = true

        val top = UIStackView()
        top.axis = UILayoutConstraintAxisVertical
        top.spacing = 4.0
        for (v in listOf(hint, note, bar, counts, readings)) top.addArrangedSubview(v)
        buttons.axis = UILayoutConstraintAxisVertical
        buttons.spacing = 6.0
        arrow.textAlignment = NSTextAlignmentCenter
        arrow.hidden = true
        arrow.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(arrow)
        val safe = view.safeAreaLayoutGuide
        val constraints = ArrayList<NSLayoutConstraint>()
        constraints += listOf(sceneView.topAnchor.constraintEqualToAnchor(view.topAnchor), sceneView.bottomAnchor.constraintEqualToAnchor(view.bottomAnchor),
            sceneView.leadingAnchor.constraintEqualToAnchor(view.leadingAnchor), sceneView.trailingAnchor.constraintEqualToAnchor(view.trailingAnchor),
            arrow.centerXAnchor.constraintEqualToAnchor(view.centerXAnchor), arrow.centerYAnchor.constraintEqualToAnchor(view.centerYAnchor))
        for ((box, atTop) in listOf(top to true, buttons to false)) {
            val shade = UIView()         // a dark panel behind the words and the buttons, so that they read over any rock
            shade.backgroundColor = UIColor.blackColor.colorWithAlphaComponent(0.65)
            shade.layer.cornerRadius = 10.0
            shade.translatesAutoresizingMaskIntoConstraints = false
            box.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(shade)
            shade.addSubview(box)
            constraints += listOf(shade.leadingAnchor.constraintEqualToAnchor(safe.leadingAnchor, constant = 10.0), shade.trailingAnchor.constraintEqualToAnchor(safe.trailingAnchor, constant = -10.0),
                if (atTop) shade.topAnchor.constraintEqualToAnchor(safe.topAnchor, constant = 8.0) else shade.bottomAnchor.constraintEqualToAnchor(safe.bottomAnchor, constant = -8.0),
                box.topAnchor.constraintEqualToAnchor(shade.topAnchor, constant = 8.0), box.bottomAnchor.constraintEqualToAnchor(shade.bottomAnchor, constant = -8.0),
                box.leadingAnchor.constraintEqualToAnchor(shade.leadingAnchor, constant = 10.0), box.trailingAnchor.constraintEqualToAnchor(shade.trailingAnchor, constant = -10.0))
        }
        NSLayoutConstraint.activateConstraints(constraints)
        show(ui)
    }

    override fun viewWillAppear(animated: Boolean) {
        super.viewWillAppear(animated)
        UIApplication.sharedApplication.idleTimerDisabled = true
        if (failed == null && !ARWorldTrackingConfiguration.isSupported) failed = "This phone can't track its own position (it doesn't support ARKit's world tracking), which guided capture needs."
        if (failed == null) {
            val config = ARWorldTrackingConfiguration()
            // the camera's full-size pictures while it tracks, where the phone can give them
            ARWorldTrackingConfiguration.recommendedVideoFormatForHighResolutionFrameCapturing?.let { config.videoFormat = it; if (stillWidth == null) stillWidth = 4032 }
            config.planeDetection = ARPlaneDetectionHorizontal or ARPlaneDetectionVertical
            config.autoFocusEnabled = true         // holds are photographed from well under a metre
            if (ARWorldTrackingConfiguration.supportsSceneReconstruction(ARSceneReconstructionMesh)) config.sceneReconstruction = ARSceneReconstructionMesh   // with lidar, taps land on bare rock
            sceneView.session.runWithConfiguration(config)
            cameraSet = false
        }
        timer = NSTimer.scheduledTimerWithTimeInterval(1.0 / 12, repeats = true) { _ -> tick() }
    }

    override fun viewWillDisappear(animated: Boolean) {
        super.viewWillDisappear(animated)
        timer?.invalidate()
        timer = null
        sceneView.session.pause()
        UIApplication.sharedApplication.idleTimerDisabled = false
    }

    /**
     * The camera's own settings, once it's running: its shutter never open longer than 1/120 s, so that a hand's shake
     * smears a full-size photo by under a pixel; iOS turns the gain up instead where the light is poor.
     */
    private fun setCamera() {
        val device = ARWorldTrackingConfiguration.configurableCaptureDeviceForPrimaryCamera ?: return
        if (!device.lockForConfiguration(null)) return
        device.setActiveMaxExposureDuration(CMTimeMake(1, 120))
        device.unlockForConfiguration()
        cameraSet = true
    }

    // ---- each look at ARKit's latest frame -----------------------------------------------------------------------------
    private fun tick() {
        ticks++
        val refused = AVCaptureDevice.authorizationStatusForMediaType(AVMediaTypeVideo).let { it == 1L || it == 2L }
        if (refused) failed = "Capturing needs the camera: allow it for this app in the phone's Settings."
        val frame = if (failed == null) sceneView.session.currentFrame else null
        if (frame == null) { show(ui.copy(text = failed ?: "Starting the camera…", phase = if (failed != null) Phase.FAILED else Phase.STARTING)); return }
        if (!cameraSet && ticks > 12) setCamera()
        val camera = frame.camera
        val tracking = camera.trackingState == ARTrackingState.ARTrackingStateNormal
        val now = app.sensors.now()
        if (tracking && plan != null && ticks % 6 == 0) frame.rawFeaturePoints?.let { cloud ->
            val p = cloud.points
            if (p != null) for (i in 0 until cloud.count.toInt()) p[i].value.let { flow.addPoint(it.getFloatAt(0).toDouble(), it.getFloatAt(1).toDouble(), it.getFloatAt(2).toDouble()) }
        }
        flow.refineDue(now)?.let { flow.refine(it) }
        val seen = poseOf(camera)
        val pose = stillWidth?.takeIf { it != seen.lens.w }?.let { w -> seen.copy(lens = seen.lens.scaledTo(w, (w.toDouble() * seen.lens.h / seen.lens.w).roundToInt())) } ?: seen
        val out = flow.frame(pose, tracking, app.sensors.gyro(), camera.exposureDuration, now, UPRIGHT_DEG)
        out.levels?.let { levels = it; colour() }
        val fit = out.guide
        guideNode.hidden = fit == null
        if (fit != null) {
            if (guideNode.geometry == null) colour()
            guideNode.transform = matrix(fit)
            guideNode.opacity = if (out.solid) 0.95 else 0.45
            guideNode.geometry?.firstMaterial?.writesToDepthBuffer = out.solid
        }
        if (out.markers != drawn) { drawn = out.markers; mark(out.markers) }
        show(out.ui)
        if (out.shoot) takeStill()
        GC.collect()        // lets go of the frame just read
    }

    private fun matrix(fit: Similarity): CValue<SCNMatrix4> {
        val a = fit.matrix()
        return cValue {
            m11 = a[0]; m12 = a[1]; m13 = a[2]; m14 = a[3]; m21 = a[4]; m22 = a[5]; m23 = a[6]; m24 = a[7]
            m31 = a[8]; m32 = a[9]; m33 = a[10]; m34 = a[11]; m41 = a[12]; m42 = a[13]; m43 = a[14]; m44 = a[15]
        }
    }

    /** The guide mesh, each face coloured by how done its patch is and shaded by which way it faces. */
    private fun colour() {
        val g = guide ?: return
        val n = g.faces * 3
        val pos = positions ?: FloatArray(n * 3).let { a ->
            for (f in 0 until g.faces) for (k in 0 until 3) { val v = g.vertex(g.indices[3 * f + k]); val at = (3 * f + k) * 3; a[at] = v.x.toFloat(); a[at + 1] = v.y.toFloat(); a[at + 2] = v.z.toFloat() }
            a.usePinned { NSData.dataWithBytes(it.addressOf(0), (a.size * 4).toULong()) }
        }.also { positions = it }
        val rgba = FloatArray(n * 4)
        val lv = levels
        for (f in 0 until g.faces) {
            val c = LEVEL_RGB[lv?.getOrNull(g.patch[f]) ?: Coverage.LEVEL_OUT]
            val normal = g.normal(f)
            val shade = (0.72 + 0.28 * (0.5 * normal.x + 0.7 * normal.y + 0.5 * normal.z)).toFloat().coerceIn(0.45f, 1f)
            for (k in 0 until 3) { val at = (3 * f + k) * 4; rgba[at] = c[0] * shade; rgba[at + 1] = c[1] * shade; rgba[at + 2] = c[2] * shade; rgba[at + 3] = 1f }
        }
        val colours = rgba.usePinned { NSData.dataWithBytes(it.addressOf(0), (rgba.size * 4).toULong()) }
        val index = IntArray(n) { it }
        val indices = index.usePinned { NSData.dataWithBytes(it.addressOf(0), (n * 4).toULong()) }
        val geometry = SCNGeometry.geometryWithSources(listOf(
            SCNGeometrySource.geometrySourceWithData(pos, SCNGeometrySourceSemanticVertex, n.toLong(), true, 3, 4, 0, 12),
            SCNGeometrySource.geometrySourceWithData(colours, SCNGeometrySourceSemanticColor, n.toLong(), true, 4, 4, 0, 16),
        ), listOf(SCNGeometryElement.geometryElementWithData(indices, SCNGeometryPrimitiveTypeTriangles, g.faces.toLong(), 4)))
        geometry.firstMaterial?.let { m ->
            m.lightingModelName = SCNLightingModelConstant
            m.diffuse.contents = UIColor.whiteColor
            m.doubleSided = false           // only the faces towards the camera: the far side would only muddy it
            m.writesToDepthBuffer = false
        }
        guideNode.geometry = geometry
    }

    /** Rings in the world: holds, edges and marked spots, coloured by how done they are; the one being asked for in white. */
    private fun mark(markers: List<Marker>) {
        val seen = HashSet<String>()
        for (m in markers) {
            seen.add(m.id)
            val key = "${m.id}/${m.level}/${m.current}/${(m.radiusM * 100).roundToInt()}"
            var node = rings[m.id]
            if (node == null || node.name != key) {
                node?.removeFromParentNode()
                val r = m.radiusM * (if (m.current) 1.25 else 1.0)
                val torus = SCNTorus.torusWithRingRadius(r, pipeRadius = r * 0.12)
                val c = LEVEL_RGB[m.level.coerceIn(0, LEVEL_RGB.size - 1)]
                torus.firstMaterial?.let { mat ->
                    mat.lightingModelName = SCNLightingModelConstant
                    mat.diffuse.contents = if (m.current) UIColor.whiteColor else UIColor.colorWithRed(c[0].toDouble(), c[1].toDouble(), c[2].toDouble(), 1.0)
                    mat.readsFromDepthBuffer = false        // seen through the rock and the mesh, wherever they are
                }
                val ring = SCNNode.nodeWithGeometry(torus)
                ring.renderingOrder = 10
                ring.eulerAngles = SCNVector3Make((kotlin.math.PI / 2).toFloat(), 0f, 0f)        // a torus lies flat: stood up, it faces the camera
                node = SCNNode()
                node.name = key
                node.constraints = listOf(SCNBillboardConstraint.billboardConstraint())
                node.addChildNode(ring)
                scene.rootNode.addChildNode(node)
                rings[m.id] = node
            }
            node.position = SCNVector3Make(m.world.x.toFloat(), m.world.y.toFloat(), m.world.z.toFloat())
        }
        for (gone in rings.keys.filter { it !in seen }) rings.remove(gone)?.removeFromParentNode()
    }

    // ---- taps ---------------------------------------------------------------------------------------------------------
    @ObjCAction
    fun tapped(g: UITapGestureRecognizer) {
        val (x, y) = g.locationInView(sceneView).useContents { x to y }
        val near = sceneView.unprojectPoint(SCNVector3Make(x.toFloat(), y.toFloat(), 0f)).useContents { Vec3(this.x.toDouble(), this.y.toDouble(), this.z.toDouble()) }
        val far = sceneView.unprojectPoint(SCNVector3Make(x.toFloat(), y.toFloat(), 1f)).useContents { Vec3(this.x.toDouble(), this.y.toDouble(), this.z.toDouble()) }
        flow.tap(near, (far - near).unit()) {
            // where ARKit says that point of the screen meets the world: the lidar's surface, a plane it's found, or its best guess at one
            val query = sceneView.raycastQueryFromPoint(CGPointMake(x, y), allowingTarget = ARRaycastTarget.ARRaycastTargetEstimatedPlane, alignment = ARRaycastTargetAlignment.ARRaycastTargetAlignmentAny)
            val hit = query?.let { sceneView.session.raycast(it).firstOrNull() as? ARRaycastResult }
            hit?.worldTransform?.column(3)
        }
    }

    // ---- a photo ------------------------------------------------------------------------------------------------------
    private fun takeStill() {
        sceneView.session.captureHighResolutionFrameWithCompletion { frame, _ ->
            dispatch_async(dispatch_get_main_queue()) {
                // a phone that gives no full-size picture while it tracks: the frame it tracks with, as it is
                val f = frame ?: sceneView.session.currentFrame
                if (f == null) flow.noShot("The camera gave no picture.") else still(f)
            }
        }
    }

    private fun still(frame: ARFrame) {
        val camera = frame.camera
        val pose = poseOf(camera)
        stillWidth = pose.lens.w
        val tracking = camera.trackingState == ARTrackingState.ARTrackingStateNormal
        val sample = app.sensors.sample()
        val exposure = camera.exposureDuration
        val iso = ((frame.exifData["ISOSpeedRatings"] as? List<*>)?.firstOrNull() as? Number)?.toDouble()
        val now = app.sensors.now()
        val unix = NSDate().timeIntervalSince1970
        val buffer = frame.capturedImage
        dispatch_async(background) {
            val stats = lumaStats(buffer)
            dispatch_async(dispatch_get_main_queue()) {
                val out = flow.shot(ShotIn(pose, sample, stats, exposure, iso, null, tracking, arSession, now, unix, UPRIGHT_DEG, sample.gyro?.norm ?: 0.0))
                if (out.kept) dispatch_async(background) {
                    val ok = writeJpeg(frame, "$dir/photos/${out.record.name}", out.userComment, sample, pose.lens, exposure, iso, unix)
                    dispatch_async(dispatch_get_main_queue()) { if (ok) save() else flow.say("A photo couldn't be saved.") }
                }
            }
        }
    }

    /** A photo's stats from its brightness: the first plane of the camera's picture. */
    private fun lumaStats(buffer: CVPixelBufferRef?): ImageStats? {
        if (buffer == null || CVPixelBufferGetPlaneCount(buffer) < 1uL) return null
        CVPixelBufferLockBaseAddress(buffer, kCVPixelBufferLock_ReadOnly)
        try {
            val w = CVPixelBufferGetWidthOfPlane(buffer, 0u).toInt(); val h = CVPixelBufferGetHeightOfPlane(buffer, 0u).toInt()
            val stride = CVPixelBufferGetBytesPerRowOfPlane(buffer, 0u).toInt()
            val base = CVPixelBufferGetBaseAddressOfPlane(buffer, 0u) ?: return null
            return Quality.imageStats(base.readBytes(stride * (h - 1) + w), w, h, stride)
        } finally {
            CVPixelBufferUnlockBaseAddress(buffer, kCVPixelBufferLock_ReadOnly)
        }
    }

    /** The picture as a JPEG, its pixels as the sensor read them, with its readings and how to show it upright in its EXIF. */
    private fun writeJpeg(frame: ARFrame, path: String, comment: String, s: SensorSample, lens: Lens, exposure: Double, iso: Double?, unix: Double): Boolean {
        val image = CIImage.imageWithCVPixelBuffer(frame.capturedImage)
        val cg = ciContext.createCGImage(image, fromRect = image.extent) ?: return false
        val stamp = NSDateFormatter().apply { dateFormat = "yyyy:MM:dd HH:mm:ss" }.stringFromDate(NSDate.dateWithTimeIntervalSince1970(unix))
        // as Foundation's own numbers and dictionaries, which is what ImageIO is sure to read
        fun n(x: Double) = NSNumber.numberWithDouble(x)
        fun n(x: Int) = NSNumber.numberWithInt(x)
        fun dict(m: Map<Any?, *>) = NSDictionary.dictionaryWithDictionary(m)
        val exif = mutableMapOf<Any?, Any?>(kCGImagePropertyExifUserComment.text() to comment, kCGImagePropertyExifDateTimeOriginal.text() to stamp,
            kCGImagePropertyExifExposureTime.text() to n(exposure), kCGImagePropertyExifFocalLenIn35mmFilm.text() to n((lens.fx / lens.w * 36.0).roundToInt()))
        val props = mutableMapOf<Any?, Any?>(kCGImagePropertyOrientation.text() to n(6), kCGImageDestinationLossyCompressionQuality.text() to n(0.92), kCGImagePropertyExifDictionary.text() to dict(exif),
            kCGImagePropertyTIFFDictionary.text() to dict(mapOf<Any?, Any?>(kCGImagePropertyTIFFMake.text() to "Apple", kCGImagePropertyTIFFModel.text() to machine(), kCGImagePropertyTIFFSoftware.text() to "BoulderMesh ${IosStore.VERSION}")))
        val gps = mutableMapOf<Any?, Any?>()
        s.gps?.let { g ->
            gps[kCGImagePropertyGPSLatitude.text()] = n(abs(g.lat)); gps[kCGImagePropertyGPSLatitudeRef.text()] = if (g.lat >= 0) "N" else "S"
            gps[kCGImagePropertyGPSLongitude.text()] = n(abs(g.lng)); gps[kCGImagePropertyGPSLongitudeRef.text()] = if (g.lng >= 0) "E" else "W"
            g.alt?.let { gps[kCGImagePropertyGPSAltitude.text()] = n(abs(it)); gps[kCGImagePropertyGPSAltitudeRef.text()] = n(if (it >= 0) 0 else 1) }
            gps[kCGImagePropertyGPSHPositioningError.text()] = n(g.accM)
        }
        s.headingDeg?.let { gps[kCGImagePropertyGPSImgDirection.text()] = n(it); gps[kCGImagePropertyGPSImgDirectionRef.text()] = "T" }
        if (gps.isNotEmpty()) props[kCGImagePropertyGPSDictionary.text()] = dict(gps)
        val url: CFURLRef? = CFBridgingRetain(NSURL.fileURLWithPath(path))?.reinterpret()
        val type = CFStringCreateWithCString(null, "public.jpeg", kCFStringEncodingUTF8)
        val dictionary: CFDictionaryRef? = CFBridgingRetain(dict(props))?.reinterpret()
        val dest = if (url != null && type != null) CGImageDestinationCreateWithURL(url, type, 1u, null) else null
        var ok = false
        if (dest != null) {
            CGImageDestinationAddImage(dest, cg, dictionary)
            ok = CGImageDestinationFinalize(dest)
            CFRelease(dest)
        }
        for (held in listOf(dictionary, type, url)) if (held != null) CFRelease(held)      // releasing nothing at all is a crash
        CGImageRelease(cg)
        return ok
    }

    /** Writes the capture's record and what's to upload beside its photos: after each photo kept, so that one cut short isn't lost. */
    private fun save() {
        core.place = app.sensors.place.result()
        Files.write("$dir/capture.json", core.record(NSDate().timeIntervalSince1970).json())
        app.store.saveUpload(id, UploadState(name = "$name (capture)", mesh = plan?.mesh, photos = core.shotCount))
    }

    // ---- what's said and offered ---------------------------------------------------------------------------------------
    private fun show(now: CaptureUi) {
        ui = now
        hint.text = failed ?: now.text
        hint.textColor = if (failed != null) BAD else UIColor.whiteColor
        note.text = now.note
        note.hidden = now.note.isEmpty()
        note.textColor = if (now.note.startsWith("Not kept")) WARN else UIColor.whiteColor
        val capturing = now.phase == Phase.CAPTURE && now.total > 0
        bar.hidden = !capturing
        counts.hidden = !capturing
        if (capturing) {
            bar.progress = now.done.toFloat() / now.total
            counts.text = listOfNotNull("${now.done}/${now.total} ${if (now.guided) "parts" else "sides and spots"}", if (now.holds > 0 && now.guided) "${now.holdsDone}/${now.holds} holds" else null,
                "${now.kept} photos", if (now.rejected > 0) "${now.rejected} not kept" else null).joinToString(" · ")
        }
        readings.text = now.sensors
        readings.hidden = !(now.phase == Phase.CAPTURE && now.sensors.isNotEmpty())
        // which way to turn, when what's wanted is out of the picture
        val turn = now.phase == Phase.CAPTURE && hypot(now.turnX, now.turnY) > 0.3
        arrow.hidden = !turn
        if (turn) arrow.transform = CGAffineTransformMakeRotation(atan2(-now.turnY, now.turnX))     // the screen's y runs down
        val want = "${now.phase}/${now.auto}/${now.canSkip}/${now.wide}/${now.marking}/${now.guided}/${now.pairs > 0}/${now.kept > 0}"
        if (want != buttonsFor) { buttonsFor = want; offer(now) }
    }

    private fun offer(now: CaptureUi) {
        for (v in buttons.arrangedSubviews) (v as? UIView)?.removeFromSuperview()
        val leave = makeButton(if (now.kept > 0) "Finish" else "Leave", if (now.kept > 0) Look.FILLED else Look.PLAIN, color = if (now.kept > 0) GOOD else UIColor.whiteColor) { leave() }
        when (now.phase) {
            Phase.GHOST, Phase.ROCK -> {
                buttons.addArrangedSubview(makeRow(makeButton("Turn it") { flow.turnGhost(45.0) }, makeButton("Bring it here") { flow.bringGhost() }))
                buttons.addArrangedSubview(if (now.pairs > 0 || now.phase == Phase.ROCK) makeRow(makeButton("Start again", Look.PLAIN) { flow.restartAlign() }, leave) else makeRow(leave))
            }
            Phase.CONFIRM -> {
                buttons.addArrangedSubview(makeRow(makeButton("It fits: start", Look.FILLED) { flow.confirmFit() }, makeButton("Add a spot") { flow.addSpot() }))
                buttons.addArrangedSubview(makeRow(makeButton("Start again", Look.PLAIN) { flow.restartAlign() }, leave))
            }
            Phase.CAPTURE -> {
                buttons.addArrangedSubview(makeRow(makeButton("Take", Look.FILLED) { flow.shutter() }, makeButton(if (now.auto) "Auto: on" else "Auto: off") { flow.setAuto(!now.auto) }, leave))
                buttons.addArrangedSubview(if (now.guided) makeRow(makeButton("Skip this", enabled = now.canSkip) { flow.skip() }, makeButton(if (now.wide) "Doing: whole rock" else "Doing: its climbs") { flow.toggleWide() })
                    else makeRow(makeButton(if (now.marking) "Tap the hold…" else "Mark a hold") { flow.toggleMarking() }, makeButton("Can't reach that side") { flow.blockSide() }))
            }
            else -> buttons.addArrangedSubview(makeRow(leave))
        }
    }

    private fun leave() {
        val kept = ui.kept
        val go = {
            if (kept > 0) save() else app.store.deleteCapture(id)
            dismissViewControllerAnimated(true) { done(if (kept > 0) id else null) }
        }
        if (kept == 0) go()
        else confirm("Finish this capture?", "$kept photos kept" + (if (ui.total > 0 && ui.done < ui.total) ", with ${ui.total - ui.done} of ${ui.total} still to do. What's there is still worth sending." else "."),
            "Finish", "Carry on") { go() }
    }
}
