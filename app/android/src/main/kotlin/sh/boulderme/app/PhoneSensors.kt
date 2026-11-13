package sh.boulderme.app

import android.annotation.SuppressLint
import android.content.Context
import android.hardware.GeomagneticField
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.os.SystemClock
import sh.boulderme.core.Axes
import sh.boulderme.core.GpsAverager
import sh.boulderme.core.GpsFix
import sh.boulderme.core.Quat
import sh.boulderme.core.SensorSample
import sh.boulderme.core.Vec3
import kotlin.math.sqrt

/**
 * The phone's own readings, kept current: its accelerometer, gyroscope and compass, where its camera points by them,
 * and its GPS fixes. Times are seconds since the phone started (SystemClock.elapsedRealtime), as its fixes' are.
 */
class PhoneSensors(private val context: Context) : SensorEventListener, LocationListener {
    private val sensors = context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private val locations = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager

    @Volatile var accel: Vec3? = null; private set
    @Volatile var gyro: Vec3? = null; private set
    @Volatile var mag: Vec3? = null; private set
    @Volatile var magAccuracy: Int? = null; private set
    @Volatile var heading: Double? = null; private set          // of the back camera's view, from true north once a fix has come
    @Volatile var fix: GpsFix? = null; private set
    @Volatile var declination = 0.0; private set
    val place = GpsAverager()
    var onMag: ((Vec3) -> Unit)? = null         // for the Calibrate screen

    fun now(): Double = SystemClock.elapsedRealtimeNanos() / 1e9

    @SuppressLint("MissingPermission")
    fun start(withLocation: Boolean) {
        for (type in intArrayOf(Sensor.TYPE_ACCELEROMETER, Sensor.TYPE_GYROSCOPE, Sensor.TYPE_MAGNETIC_FIELD, Sensor.TYPE_ROTATION_VECTOR))
            sensors.getDefaultSensor(type)?.let { sensors.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME) }
        if (withLocation && locations.isProviderEnabled(LocationManager.GPS_PROVIDER))
            runCatching { locations.requestLocationUpdates(LocationManager.GPS_PROVIDER, 1000L, 0f, this, context.mainLooper) }
    }

    fun stop() {
        sensors.unregisterListener(this)
        runCatching { locations.removeUpdates(this) }
    }

    override fun onSensorChanged(e: SensorEvent) {
        when (e.sensor.type) {
            Sensor.TYPE_ACCELEROMETER -> accel = Vec3(e.values[0].toDouble(), e.values[1].toDouble(), e.values[2].toDouble())
            Sensor.TYPE_GYROSCOPE -> gyro = Vec3(e.values[0].toDouble(), e.values[1].toDouble(), e.values[2].toDouble())
            Sensor.TYPE_MAGNETIC_FIELD -> Vec3(e.values[0].toDouble(), e.values[1].toDouble(), e.values[2].toDouble()).let { mag = it; magAccuracy = e.accuracy; onMag?.invoke(it) }
            Sensor.TYPE_ROTATION_VECTOR -> {
                // the phone's turn into a frame of east, (magnetic) north and up
                val x = e.values[0].toDouble(); val y = e.values[1].toDouble(); val z = e.values[2].toDouble()
                val w = if (e.values.size > 3) e.values[3].toDouble() else sqrt((1 - x * x - y * y - z * z).coerceAtLeast(0.0))
                heading = Axes.headingOfView(Quat(w, x, y, z).unit(), declination)
            }
        }
    }

    override fun onAccuracyChanged(sensor: Sensor, accuracy: Int) { if (sensor.type == Sensor.TYPE_MAGNETIC_FIELD) magAccuracy = accuracy }

    override fun onLocationChanged(l: Location) {
        val f = GpsFix(l.latitude, l.longitude, if (l.hasAltitude()) l.altitude else null, if (l.hasAccuracy()) l.accuracy.toDouble() else 20.0, l.elapsedRealtimeNanos / 1e9)
        fix = f
        synchronized(place) { place.add(f) }
        declination = GeomagneticField(l.latitude.toFloat(), l.longitude.toFloat(), (if (l.hasAltitude()) l.altitude else 0.0).toFloat(), System.currentTimeMillis()).declination.toDouble()
    }

    @Deprecated("Deprecated in Java") override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
    override fun onProviderEnabled(provider: String) {}
    override fun onProviderDisabled(provider: String) {}

    /** Its fixes so far as one place: asked for from the thread that saves photos, while fixes arrive on the main one. */
    fun placed(): GpsAverager.Result? = synchronized(place) { place.result() }

    /** Everything as it is now, for a photo being taken. */
    fun sample(): SensorSample = SensorSample(accel, gyro, mag, magAccuracy, heading, null, fix, now())
}
