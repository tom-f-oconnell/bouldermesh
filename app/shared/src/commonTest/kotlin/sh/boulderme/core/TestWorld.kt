package sh.boulderme.core

import kotlin.math.PI
import kotlin.math.abs

/** A made-up wall of rock and its plan, for the tests: 4 units wide and 3 high, facing +Z, with two holds and an edge. */
object TestWorld {
    const val NX = 20; const val NY = 15
    val lens = Lens(3000.0, 3000.0, 2000.0, 1500.0, 4000, 3000)

    fun guide(): GuideMesh {
        val pos = ArrayList<Float>(); val idx = ArrayList<Int>(); val patch = ArrayList<Int>()
        for (i in 0..NX) for (j in 0..NY) { pos.add(-2f + 4f * i / NX); pos.add(3f * j / NY); pos.add(0f) }
        fun v(i: Int, j: Int) = i * (NY + 1) + j
        for (i in 0 until NX) for (j in 0 until NY) {
            val p = (i * 4 / NX) * 3 + (j * 3 / NY)
            idx.addAll(listOf(v(i, j), v(i + 1, j), v(i + 1, j + 1))); patch.add(p)
            idx.addAll(listOf(v(i, j), v(i + 1, j + 1), v(i, j + 1))); patch.add(p)
        }
        val nf = idx.size / 3
        return GuideMesh.parse(GuideMesh.bytes(pos.toFloatArray(), idx.toIntArray(), patch.toIntArray(), FloatArray(nf) { 0.008f }, IntArray(nf) { 2 }, 12))
    }

    fun plan(scale: Double = 1.25): Plan {
        val targets = ArrayList<Target>()
        for (a in 0 until 4) for (b in 0 until 3)
            targets.add(Target("p${a * 3 + b}", "patch", listOf(-1.5 + a, 0.5 + b, 0.0), listOf(0.0, 0.0, 1.0), 0.5, prio = if (a in 1..2) 2 else 1,
                haveGsd = 0.008, haveViews = 2, climbs = if (a in 1..2) listOf("c1") else emptyList()))
        targets.add(Target("h0", "hold", listOf(-0.5, 1.0, 0.0), listOf(0.0, 0.0, 1.0), 0.12, prio = 3, role = "start", haveGsd = 0.002, haveViews = 5, climbs = listOf("c1")))
        targets.add(Target("h1", "hold", listOf(0.6, 1.8, 0.0), listOf(0.0, 0.0, 1.0), 0.12, prio = 3, role = "finish", haveGsd = 0.002, haveViews = 5, climbs = listOf("c1")))
        targets.add(Target("e0", "edge", listOf(2.0, 1.5, 0.0), listOf(0.0, 0.0, 1.0), 0.25, prio = 2, out = listOf(1.0, 0.0, 0.0), climbs = listOf("c1")))
        return Plan(mesh = "abc", name = "A Wall", frame = Frame(groundY = 0.0, size = 4.0, height = 3.0, metresPerUnit = scale, scaleGuessed = false),
            guide = GuideInfo(patches = 12), targets = targets, climbs = listOf(PlanClimb("c1", "Alpha", "V2")))
    }

    /** A camera at `from`, looking at `at`, held level. */
    fun look(from: Vec3, at: Vec3, l: Lens = lens): CameraPose {
        val f = (at - from).unit()
        val right = (f cross Vec3.UP).unit()
        return CameraPose(from, Quat.fromAxes(right, f cross right, f), l)
    }

    fun near(a: Double, b: Double, eps: Double) = abs(a - b) <= eps
    fun near(a: Vec3, b: Vec3, eps: Double) = a.distance(b) <= eps
    val rad = PI / 180
}
