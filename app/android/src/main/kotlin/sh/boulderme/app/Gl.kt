package sh.boulderme.app

import android.opengl.GLES11Ext
import android.opengl.GLES20
import com.google.ar.core.Coordinates2d
import com.google.ar.core.Frame
import sh.boulderme.core.Coverage
import sh.boulderme.core.GuideMesh
import sh.boulderme.core.Marker
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer

private fun floats(n: Int): FloatBuffer = ByteBuffer.allocateDirect(n * 4).order(ByteOrder.nativeOrder()).asFloatBuffer()

private fun program(vertex: String, fragment: String): Int {
    fun shader(type: Int, src: String): Int {
        val s = GLES20.glCreateShader(type)
        GLES20.glShaderSource(s, src); GLES20.glCompileShader(s)
        val ok = IntArray(1)
        GLES20.glGetShaderiv(s, GLES20.GL_COMPILE_STATUS, ok, 0)
        check(ok[0] != 0) { "shader: " + GLES20.glGetShaderInfoLog(s) }
        return s
    }
    val p = GLES20.glCreateProgram()
    GLES20.glAttachShader(p, shader(GLES20.GL_VERTEX_SHADER, vertex)); GLES20.glAttachShader(p, shader(GLES20.GL_FRAGMENT_SHADER, fragment))
    GLES20.glLinkProgram(p)
    return p
}

/** RGBA for each Coverage.LEVEL_*: grey for what doesn't count, red to do, amber part done, green done, blue-grey skipped. */
val LEVEL_COLORS = arrayOf(
    floatArrayOf(0.62f, 0.62f, 0.62f, 0.22f), floatArrayOf(0.90f, 0.22f, 0.18f, 0.45f), floatArrayOf(0.96f, 0.68f, 0.16f, 0.45f),
    floatArrayOf(0.20f, 0.78f, 0.38f, 0.35f), floatArrayOf(0.40f, 0.50f, 0.72f, 0.30f),
)

/** The camera's picture, behind everything. */
class Background {
    val texture: Int
    private val prog = program(
        "attribute vec4 a_Pos; attribute vec2 a_Tex; varying vec2 v_Tex; void main() { gl_Position = a_Pos; v_Tex = a_Tex; }",
        "#extension GL_OES_EGL_image_external : require\nprecision mediump float; varying vec2 v_Tex; uniform samplerExternalOES u_Tex; void main() { gl_FragColor = texture2D(u_Tex, v_Tex); }",
    )
    private val quad = floats(8).apply { put(floatArrayOf(-1f, -1f, 1f, -1f, -1f, 1f, 1f, 1f)); position(0) }
    private val tex = floats(8)
    private var ready = false

    init {
        val t = IntArray(1)
        GLES20.glGenTextures(1, t, 0)
        texture = t[0]
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, texture)
        for (p in intArrayOf(GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_TEXTURE_WRAP_T)) GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, p, GLES20.GL_CLAMP_TO_EDGE)
        for (p in intArrayOf(GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_TEXTURE_MAG_FILTER)) GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, p, GLES20.GL_LINEAR)
    }

    fun draw(frame: Frame) {
        if (frame.hasDisplayGeometryChanged() || !ready) {
            quad.position(0); tex.position(0)
            frame.transformCoordinates2d(Coordinates2d.OPENGL_NORMALIZED_DEVICE_COORDINATES, quad, Coordinates2d.TEXTURE_NORMALIZED, tex)
            ready = true
        }
        if (frame.timestamp == 0L) return       // nothing from the camera yet
        quad.position(0); tex.position(0)
        GLES20.glDisable(GLES20.GL_DEPTH_TEST); GLES20.glDepthMask(false)
        GLES20.glUseProgram(prog)
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, texture)
        val pos = GLES20.glGetAttribLocation(prog, "a_Pos"); val uv = GLES20.glGetAttribLocation(prog, "a_Tex")
        GLES20.glVertexAttribPointer(pos, 2, GLES20.GL_FLOAT, false, 0, quad); GLES20.glEnableVertexAttribArray(pos)
        GLES20.glVertexAttribPointer(uv, 2, GLES20.GL_FLOAT, false, 0, tex); GLES20.glEnableVertexAttribArray(uv)
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)
        GLES20.glDisableVertexAttribArray(pos); GLES20.glDisableVertexAttribArray(uv)
        GLES20.glDepthMask(true); GLES20.glEnable(GLES20.GL_DEPTH_TEST)
    }
}

/** The guide mesh, see-through, each face coloured by how done its patch is and shaded by which way it faces. */
class GuideRenderer(private val guide: GuideMesh) {
    private val prog = program(
        "uniform mat4 u_Mvp; attribute vec3 a_Pos; attribute vec4 a_Col; varying vec4 v_Col; void main() { gl_Position = u_Mvp * vec4(a_Pos, 1.0); v_Col = a_Col; }",
        "precision mediump float; varying vec4 v_Col; uniform float u_Alpha; void main() { gl_FragColor = vec4(v_Col.rgb, min(1.0, v_Col.a * u_Alpha)); }",
    )
    private val positions = floats(guide.faces * 9)
    private val colors = floats(guide.faces * 12)
    private val shade = FloatArray(guide.faces)

    init {
        for (f in 0 until guide.faces) {
            for (k in 0 until 3) { val v = guide.vertex(guide.indices[3 * f + k]); positions.put(v.x.toFloat()); positions.put(v.y.toFloat()); positions.put(v.z.toFloat()) }
            val n = guide.normal(f)
            shade[f] = (0.72 + 0.28 * (0.5 * n.x + 0.7 * n.y + 0.5 * n.z)).toFloat().coerceIn(0.45f, 1f)
        }
        positions.position(0)
        setLevels(IntArray(guide.patches) { Coverage.LEVEL_OUT })
    }

    /** Colours each face by its patch's level (Coverage.patchLevels()). */
    fun setLevels(levels: IntArray) {
        colors.position(0)
        for (f in 0 until guide.faces) {
            val c = LEVEL_COLORS[levels.getOrElse(guide.patch[f]) { Coverage.LEVEL_OUT }]
            repeat(3) { colors.put(c[0] * shade[f]); colors.put(c[1] * shade[f]); colors.put(c[2] * shade[f]); colors.put(c[3]) }
        }
        colors.position(0)
    }

    /**
     * @param alpha how much of each face's own see-throughness to keep; more than 1 makes it more solid.
     * @param solid drawn as a thing of its own (the small model), nearer faces hiding further ones, rather than as a
     *   wash over the rock in the camera's picture.
     */
    fun draw(mvp: FloatArray, alpha: Float, solid: Boolean = false) {
        GLES20.glUseProgram(prog)
        GLES20.glEnable(GLES20.GL_BLEND); GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA)
        GLES20.glEnable(GLES20.GL_CULL_FACE)        // only the faces towards the camera: the far side would only muddy it
        GLES20.glDepthMask(solid)
        GLES20.glUniformMatrix4fv(GLES20.glGetUniformLocation(prog, "u_Mvp"), 1, false, mvp, 0)
        GLES20.glUniform1f(GLES20.glGetUniformLocation(prog, "u_Alpha"), alpha)
        val pos = GLES20.glGetAttribLocation(prog, "a_Pos"); val col = GLES20.glGetAttribLocation(prog, "a_Col")
        positions.position(0); colors.position(0)
        GLES20.glVertexAttribPointer(pos, 3, GLES20.GL_FLOAT, false, 0, positions); GLES20.glEnableVertexAttribArray(pos)
        GLES20.glVertexAttribPointer(col, 4, GLES20.GL_FLOAT, false, 0, colors); GLES20.glEnableVertexAttribArray(col)
        GLES20.glDrawArrays(GLES20.GL_TRIANGLES, 0, guide.faces * 3)
        GLES20.glDisableVertexAttribArray(pos); GLES20.glDisableVertexAttribArray(col)
        GLES20.glDepthMask(true); GLES20.glDisable(GLES20.GL_BLEND); GLES20.glDisable(GLES20.GL_CULL_FACE)
    }
}

/** Rings in the world: holds, edges and marked spots, coloured by how done they are; the one being asked for in white. */
class MarkerRenderer {
    private val prog = program(
        "uniform mat4 u_ViewProj; uniform float u_Scale; uniform float u_Radius; attribute vec3 a_Pos; " +
            "void main() { gl_Position = u_ViewProj * vec4(a_Pos, 1.0); gl_PointSize = clamp(u_Radius * u_Scale / gl_Position.w, 14.0, 260.0); }",
        "precision mediump float; uniform vec4 u_Col; void main() { float d = length(gl_PointCoord - vec2(0.5)) * 2.0; if (d > 1.0 || d < 0.72) discard; gl_FragColor = u_Col; }",
    )
    private val one = floats(3)

    /** @param pixelsPerMetre how many pixels a metre is at a metre off: the view's height times the projection's [5], over 2, doubled for a diameter. */
    fun draw(markers: List<Marker>, viewProj: FloatArray, pixelsPerMetre: Float) {
        if (markers.isEmpty()) return
        GLES20.glUseProgram(prog)
        GLES20.glDisable(GLES20.GL_DEPTH_TEST)
        GLES20.glEnable(GLES20.GL_BLEND); GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA)
        GLES20.glUniformMatrix4fv(GLES20.glGetUniformLocation(prog, "u_ViewProj"), 1, false, viewProj, 0)
        GLES20.glUniform1f(GLES20.glGetUniformLocation(prog, "u_Scale"), pixelsPerMetre)
        val pos = GLES20.glGetAttribLocation(prog, "a_Pos")
        GLES20.glEnableVertexAttribArray(pos)
        for (m in markers) {
            one.position(0); one.put(m.world.x.toFloat()); one.put(m.world.y.toFloat()); one.put(m.world.z.toFloat()); one.position(0)
            val c = if (m.current) floatArrayOf(1f, 1f, 1f, 0.95f) else LEVEL_COLORS[m.level].let { floatArrayOf(it[0], it[1], it[2], 0.9f) }
            GLES20.glUniform4fv(GLES20.glGetUniformLocation(prog, "u_Col"), 1, c, 0)
            GLES20.glUniform1f(GLES20.glGetUniformLocation(prog, "u_Radius"), (if (m.current) 1.25f else 1f) * m.radiusM.toFloat())
            GLES20.glVertexAttribPointer(pos, 3, GLES20.GL_FLOAT, false, 0, one)
            GLES20.glDrawArrays(GLES20.GL_POINTS, 0, 1)
        }
        GLES20.glDisableVertexAttribArray(pos)
        GLES20.glDisable(GLES20.GL_BLEND); GLES20.glEnable(GLES20.GL_DEPTH_TEST)
    }
}
