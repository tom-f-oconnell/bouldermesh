package sh.boulderme.core

import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

/** A point or direction. Lengths are metres in the world (the phone's tracking frame, +Y up), or a mesh's own units. */
data class Vec3(val x: Double, val y: Double, val z: Double) {
    operator fun plus(o: Vec3) = Vec3(x + o.x, y + o.y, z + o.z)
    operator fun minus(o: Vec3) = Vec3(x - o.x, y - o.y, z - o.z)
    operator fun times(s: Double) = Vec3(x * s, y * s, z * s)
    operator fun div(s: Double) = Vec3(x / s, y / s, z / s)
    operator fun unaryMinus() = Vec3(-x, -y, -z)
    infix fun dot(o: Vec3) = x * o.x + y * o.y + z * o.z
    infix fun cross(o: Vec3) = Vec3(y * o.z - z * o.y, z * o.x - x * o.z, x * o.y - y * o.x)
    val norm: Double get() = sqrt(x * x + y * y + z * z)
    fun unit(): Vec3 = norm.let { if (it > 1e-12) this / it else this }
    fun distance(o: Vec3) = (this - o).norm
    fun toList() = listOf(x, y, z)

    companion object {
        val ZERO = Vec3(0.0, 0.0, 0.0)
        val UP = Vec3(0.0, 1.0, 0.0)
        fun of(v: List<Double>) = Vec3(v[0], v[1], v[2])
    }
}

/** The angle between two directions, in degrees. */
fun angleDeg(a: Vec3, b: Vec3): Double {
    val d = (a.unit() dot b.unit()).coerceIn(-1.0, 1.0)
    return acos(d) * 180.0 / kotlin.math.PI
}

/** A rotation, as a unit quaternion. */
data class Quat(val w: Double, val x: Double, val y: Double, val z: Double) {
    operator fun times(o: Quat) = Quat(
        w * o.w - x * o.x - y * o.y - z * o.z,
        w * o.x + x * o.w + y * o.z - z * o.y,
        w * o.y - x * o.z + y * o.w + z * o.x,
        w * o.z + x * o.y - y * o.x + z * o.w,
    )

    fun conj() = Quat(w, -x, -y, -z)

    fun unit(): Quat {
        val n = sqrt(w * w + x * x + y * y + z * z)
        return if (n > 1e-12) Quat(w / n, x / n, y / n, z / n) else IDENTITY
    }

    fun rotate(v: Vec3): Vec3 {
        // v + 2 q x (q x v + w v), q the vector part
        val q = Vec3(x, y, z)
        val t = (q cross v) * 2.0
        return v + t * w + (q cross t)
    }

    fun toList() = listOf(w, x, y, z)

    companion object {
        val IDENTITY = Quat(1.0, 0.0, 0.0, 0.0)

        fun axisAngle(axis: Vec3, radians: Double): Quat {
            val a = axis.unit()
            val s = sin(radians / 2)
            return Quat(cos(radians / 2), a.x * s, a.y * s, a.z * s)
        }

        /** A turn about +Y (up): positive turns +Z towards +X. */
        fun yaw(radians: Double) = axisAngle(Vec3.UP, radians)

        /** From the columns of a rotation matrix: where it takes the x, y and z axes. */
        fun fromAxes(cx: Vec3, cy: Vec3, cz: Vec3): Quat {
            val m00 = cx.x; val m10 = cx.y; val m20 = cx.z
            val m01 = cy.x; val m11 = cy.y; val m21 = cy.z
            val m02 = cz.x; val m12 = cz.y; val m22 = cz.z
            val tr = m00 + m11 + m22
            val q = when {
                tr > 0 -> { val s = sqrt(tr + 1.0) * 2; Quat(s / 4, (m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s) }
                m00 > m11 && m00 > m22 -> { val s = sqrt(1.0 + m00 - m11 - m22) * 2; Quat((m21 - m12) / s, s / 4, (m01 + m10) / s, (m02 + m20) / s) }
                m11 > m22 -> { val s = sqrt(1.0 + m11 - m00 - m22) * 2; Quat((m02 - m20) / s, (m01 + m10) / s, s / 4, (m12 + m21) / s) }
                else -> { val s = sqrt(1.0 + m22 - m00 - m11) * 2; Quat((m10 - m01) / s, (m02 + m20) / s, (m12 + m21) / s, s / 4) }
            }
            return q.unit()
        }

        fun of(v: List<Double>) = Quat(v[0], v[1], v[2], v[3]).unit()
    }
}

/**
 * What takes a mesh's own coordinates to the world's: scaled (metres a unit), turned, then moved. Both are +Y up, so
 * the turn is mostly about Y; the rest of it is how far off level the mesh was.
 */
data class Similarity(val scale: Double, val rot: Quat, val shift: Vec3) {
    fun apply(p: Vec3): Vec3 = rot.rotate(p * scale) + shift
    fun dir(d: Vec3): Vec3 = rot.rotate(d)
    fun inverse(): Similarity {
        val r = rot.conj()
        return Similarity(1.0 / scale, r, r.rotate(-shift) / scale)
    }

    /** As a column-major 4x4 matrix, as OpenGL and SceneKit take a model's. */
    fun matrix(): FloatArray {
        val cx = rot.rotate(Vec3(scale, 0.0, 0.0)); val cy = rot.rotate(Vec3(0.0, scale, 0.0)); val cz = rot.rotate(Vec3(0.0, 0.0, scale))
        return floatArrayOf(
            cx.x.toFloat(), cx.y.toFloat(), cx.z.toFloat(), 0f,
            cy.x.toFloat(), cy.y.toFloat(), cy.z.toFloat(), 0f,
            cz.x.toFloat(), cz.y.toFloat(), cz.z.toFloat(), 0f,
            shift.x.toFloat(), shift.y.toFloat(), shift.z.toFloat(), 1f,
        )
    }

    /** How far its up is from the world's, in degrees: how far off level the mesh was. */
    val tiltDeg: Double get() = angleDeg(rot.rotate(Vec3.UP), Vec3.UP)

    companion object {
        val IDENTITY = Similarity(1.0, Quat.IDENTITY, Vec3.ZERO)
    }
}

/**
 * A camera as a photo was taken: where it was, and how it was turned, in image axes (x along the stored pixels' rows,
 * y down their columns, z the way it looks), with its lens for that picture's size.
 */
data class CameraPose(val centre: Vec3, val rot: Quat, val lens: Lens) {
    val forward: Vec3 get() = rot.rotate(Vec3(0.0, 0.0, 1.0))

    /** Where a point lands in the picture, in pixels, and how far along the view it is; null behind the camera. */
    fun project(p: Vec3): Projected? {
        val c = rot.conj().rotate(p - centre)
        if (c.z <= 1e-6) return null
        return Projected(lens.fx * c.x / c.z + lens.cx, lens.fy * c.y / c.z + lens.cy, c.z)
    }

    /** The same camera, in the coordinates s comes from (a mesh's, for s its mesh-to-world). */
    fun into(s: Similarity): CameraPose {
        val inv = s.inverse()
        return CameraPose(inv.apply(centre), (inv.rot * rot).unit(), lens)
    }

    /**
     * The same camera for its picture turned clockwise by `deg` (a quarter, a half or three quarters): what a phone
     * held upright stores, where its sensor reads the picture out on its side. The picture's axes turn with it.
     */
    fun turned(deg: Int): CameraPose = when (((deg % 360) + 360) % 360) {
        0 -> this
        else -> CameraPose(centre, (rot * Quat.axisAngle(Vec3(0.0, 0.0, 1.0), -deg * kotlin.math.PI / 180)).unit(), lens.turned(deg))
    }

    companion object {
        /**
         * From a tracking pose given the OpenGL way (x right, y up, z towards the viewer, relative to the picture as
         * read out), as ARKit's and ARCore's camera transforms are.
         */
        fun fromGl(centre: Vec3, glToWorld: Quat, lens: Lens): CameraPose =
            CameraPose(centre, (glToWorld * Quat(0.0, 1.0, 0.0, 0.0)).unit(), lens)     // half a turn about x: y and z flipped
    }
}

data class Projected(val u: Double, val v: Double, val depth: Double)

/** A pinhole lens for a picture w by h pixels. */
data class Lens(val fx: Double, val fy: Double, val cx: Double, val cy: Double, val w: Int, val h: Int) {
    /** The same lens for the picture at another size (a still, where tracking gives it for its video frames). */
    fun scaledTo(width: Int, height: Int): Lens {
        val sx = width.toDouble() / w; val sy = height.toDouble() / h
        return Lens(fx * sx, fy * sy, cx * sx, cy * sy, width, height)
    }

    /** The same lens for the picture turned clockwise by a quarter (90), a half (180) or three quarters (270). */
    fun turned(deg: Int): Lens = when (((deg % 360) + 360) % 360) {
        90 -> Lens(fy, fx, h - cy, cx, h, w)
        180 -> Lens(fx, fy, w - cx, h - cy, w, h)
        270 -> Lens(fy, fx, cy, w - cx, h, w)
        else -> this
    }
}

internal fun clamp01(x: Double) = max(0.0, min(1.0, x))

/** The compass bearing (degrees from north, clockwise) of a direction given as east and north. */
fun bearingDeg(east: Double, north: Double): Double = ((atan2(east, north) * 180.0 / kotlin.math.PI) % 360.0 + 360.0) % 360.0

internal fun nearlyZero(x: Double) = abs(x) < 1e-12
