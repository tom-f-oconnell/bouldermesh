package sh.boulderme.ios

import kotlinx.cinterop.useContents
import platform.CoreLocation.CLLocation
import platform.CoreLocation.CLLocationManager
import platform.CoreLocation.CLLocationManagerDelegateProtocol
import platform.CoreLocation.kCLLocationAccuracyBest
import platform.CoreMotion.CMAttitudeReferenceFrameXArbitraryCorrectedZVertical
import platform.CoreMotion.CMAttitudeReferenceFrameXTrueNorthZVertical
import platform.CoreMotion.CMMotionManager
import platform.Foundation.NSProcessInfo
import platform.Foundation.timeIntervalSinceNow
import platform.darwin.NSObject
import sh.boulderme.core.Axes
import sh.boulderme.core.GpsAverager
import sh.boulderme.core.GpsFix
import sh.boulderme.core.SensorSample
import sh.boulderme.core.Sensors
import sh.boulderme.core.Vec3

/**
 * The phone's own readings, read when asked for: how it's turned and turning, the field its compass feels, where its
 * camera points by them, and its GPS fixes as they come. Axes are the phone's (x to its right, y to its top, z out of
 * its screen), and an acceleration is what an accelerometer reads at rest: 9.81 m/s2 upwards, which is the other way
 * round from how iOS gives it. Times are seconds since the phone started.
 */
class IosSensors : NSObject(), CLLocationManagerDelegateProtocol {
    private val motion = CMMotionManager()
    private val locations = CLLocationManager()
    var fix: GpsFix? = null
        private set
    val place = GpsAverager()
    private var northKnown = false

    fun now(): Double = NSProcessInfo.processInfo.systemUptime

    fun start() {
        locations.delegate = this
        locations.desiredAccuracy = kCLLocationAccuracyBest
        locations.requestWhenInUseAuthorization()
        locations.startUpdatingLocation()
        startMotion()
    }

    /**
     * Its attitude from true north needs to know where it is: used once location is allowed, and without north until
     * then. Even allowed, iOS gives no attitude at all until it has a fix and a settled compass, which at a rock with
     * no signal can take minutes: so the gyroscope and accelerometer are read on their own too, for until it does.
     */
    private fun startMotion() {
        if (motion.isGyroAvailable()) { motion.gyroUpdateInterval = 1.0 / 60; motion.startGyroUpdates() }
        if (motion.isAccelerometerAvailable()) { motion.accelerometerUpdateInterval = 1.0 / 60; motion.startAccelerometerUpdates() }
        if (!motion.isDeviceMotionAvailable()) return
        val status = locations.authorizationStatus
        northKnown = status == 3 || status == 4     // allowed always, or while the app is in use
        motion.stopDeviceMotionUpdates()
        motion.deviceMotionUpdateInterval = 1.0 / 60
        motion.startDeviceMotionUpdatesUsingReferenceFrame(if (northKnown) CMAttitudeReferenceFrameXTrueNorthZVertical else CMAttitudeReferenceFrameXArbitraryCorrectedZVertical)
    }

    fun stop() {
        locations.stopUpdatingLocation()
        motion.stopDeviceMotionUpdates()
        motion.stopGyroUpdates()
        motion.stopAccelerometerUpdates()
    }

    override fun locationManager(manager: CLLocationManager, didUpdateLocations: List<*>) {
        for (l in didUpdateLocations) {
            val loc = l as? CLLocation ?: continue
            if (loc.horizontalAccuracy < 0) continue
            val (lat, lng) = loc.coordinate.useContents { latitude to longitude }
            val f = GpsFix(lat, lng, if (loc.verticalAccuracy >= 0) loc.altitude else null, loc.horizontalAccuracy, now() + loc.timestamp.timeIntervalSinceNow)
            fix = f
            place.add(f)
        }
    }

    override fun locationManagerDidChangeAuthorization(manager: CLLocationManager) {
        val status = manager.authorizationStatus
        if ((status == 3 || status == 4) != northKnown) startMotion()
        if (status == 3 || status == 4) manager.startUpdatingLocation()
    }

    val locationAllowed: Boolean get() = locations.authorizationStatus.let { it == 3 || it == 4 }
    val locationRefused: Boolean get() = locations.authorizationStatus.let { it == 1 || it == 2 }

    /** What the accelerometer reads by itself, as Android gives it: m/s2, 9.81 upwards at rest (the other way round from iOS, and not in g). */
    fun accel(): Vec3? = motion.accelerometerData?.acceleration?.useContents { Vec3(x, y, z) * -Sensors.G }

    private fun turning(): Vec3? = motion.deviceMotion?.rotationRate?.useContents { Vec3(x, y, z) } ?: motion.gyroData?.rotationRate?.useContents { Vec3(x, y, z) }

    /** How fast the phone is turning, radians a second; as if turning fast when nothing says, so that no photo takes itself unjudged. */
    fun gyro(): Double = turning()?.norm ?: UNKNOWN_TURNING

    /** The field its compass feels, in microtesla, with the phone's own part taken out by iOS; null until it has one. */
    fun field(): Vec3? = motion.deviceMotion?.magneticField?.useContents { if (accuracy < 0) null else Vec3(field.x, field.y, field.z) }

    /** How iOS rates its compass's calibration, as Android does: 0 unreliable, 1 low, 2 medium, 3 high; null if not known. */
    fun fieldAccuracy(): Int? = motion.deviceMotion?.magneticField?.useContents { accuracy + 1 }

    /** Everything as it is now, for a photo being taken. */
    fun sample(): SensorSample {
        val m = motion.deviceMotion
        if (m == null) {    // no fused readings yet: what the sensors say by themselves, and no compass
            return SensorSample(accel(), turning(), gps = fix, t = now())
        }
        val gravity = m.gravity.useContents { Vec3(x, y, z) }
        val accel = accel() ?: ((gravity + m.userAcceleration.useContents { Vec3(x, y, z) }) * -Sensors.G)      // the sensor's own reading, as the turn on the spot's were
        val gyro = m.rotationRate.useContents { Vec3(x, y, z) }
        // which way the back camera looks, in the frame its attitude is from (x north, y west, z up). The matrix takes
        // one frame's vectors to the other's; which way round is settled by which gives gravity as the phone feels it
        val heading = if (!northKnown) null else m.attitude.rotationMatrix.useContents {
            val column = Vec3(-m13, -m23, -m33); val row = Vec3(-m31, -m32, -m33)
            Axes.headingOfNwu(if (column.distance(gravity) <= row.distance(gravity)) row else column)
        }
        return SensorSample(accel, gyro, field(), fieldAccuracy(), heading, null, fix, now())
    }
}

/** Radians a second: what an unread gyroscope is taken to say. */
private const val UNKNOWN_TURNING = 1.0
