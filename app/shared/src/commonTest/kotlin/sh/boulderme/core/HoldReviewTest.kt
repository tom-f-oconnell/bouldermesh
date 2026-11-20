package sh.boulderme.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class HoldReviewTest {
    /** A photo from `at`, looking along +z, in spell `s`, 1000 x 800 pixels. */
    private fun shot(i: Int, at: List<Double>, s: String = "a", kept: Boolean = true) = ShotRecord(
        "cap_k_00$i.jpg",
        ShotBlob(app = "test", device = "a phone", cap = "k", i = i, t = 0.0, ar = BlobAr(s, at, listOf(1.0, 0.0, 0.0, 0.0)),
            k = BlobLens(800.0, 800.0, 500.0, 400.0, 1000, 800)), kept)

    private fun record(marked: List<RecordMarked>, gym: RecordGym?, shots: List<ShotRecord>, checked: List<String> = emptyList()) =
        CaptureRecord(id = "k", app = "test", device = "a phone", started = 0.0, ended = 1.0, mode = "orbit", shots = shots,
            marked = marked, gym = gym, checked = checked)

    private val gym = RecordGym("blue", "#405b7a", 1.0, Gym.FINDER_COLOUR, listOf(listOf(40.0, 0.0, -20.0)),
        listOf(RecordGymHold("g1", listOf(0.0, 0.0, 2.0), 0.04, 12, picked = 1), RecordGymHold("g2", listOf(0.3, 0.0, 2.0), 0.04, 9),
            RecordGymHold("g3", listOf(0.0, 0.2, 2.0), 0.04, 20)), 300, 0)
    private val marked = listOf(RecordMarked("o1", "a", listOf(-0.3, 0.0, 2.0)), RecordMarked("g1", "a", listOf(0.0, 0.0, 2.0)),
        RecordMarked("g2", "a", listOf(0.3, 0.0, 2.0)), RecordMarked("g3", "a", listOf(0.0, 0.2, 2.0)))
    private val shots = listOf(shot(1, listOf(0.0, 0.0, 0.0)), shot(2, listOf(0.0, 0.0, 1.0)), shot(3, listOf(0.0, 0.0, 1.5), kept = false))

    @Test
    fun foundHoldsAreToCheckTappedOnesNot() {
        val r = record(marked, gym, shots)
        val holds = HoldReview.holds(r)
        assertEquals(listOf("g3", "g2", "g1", "o1"), holds.map { it.id })     // found first, most photos first
        assertEquals(setOf("g2", "g3"), HoldReview.unchecked(r).map { it.id }.toSet())
        assertTrue(holds.first { it.id == "g3" }.how.startsWith("Found by its colour (blue, as the hold you tapped), in 20 photos"), holds.first().how)
        assertEquals("Tapped by you", holds.first { it.id == "g1" }.how)
        assertTrue("Mark a hold" in holds.first { it.id == "o1" }.how)
        assertTrue(HoldReview.said(r)!!.startsWith("4 holds to add to the mesh as a set of beta: 2 found by the app"), HoldReview.said(r))
    }

    @Test
    fun keepingDropsTheRestAndRemembers() {
        val r = HoldReview.keep(record(marked, gym, shots), setOf("g3"))
        assertEquals(listOf("o1", "g1", "g3"), r.marked.map { it.id })
        assertEquals(listOf("g1", "g3"), r.gym!!.holds.map { it.id })
        assertEquals(listOf("g3"), r.checked)
        assertTrue(HoldReview.unchecked(r).isEmpty())
        val again = HoldReview.parse(r.json())!!
        assertEquals(listOf("g3"), again.checked)
        assertTrue(HoldReview.unchecked(again).isEmpty())
    }

    @Test
    fun aCaptureCarriedOnStillAsksAboutItsFoundHolds() {
        val r = record(marked, null, shots)        // carried on: its gym gone, its holds still in marked
        assertEquals(setOf("g1", "g2", "g3"), HoldReview.unchecked(r).map { it.id }.toSet())
    }

    @Test
    fun theViewIsThePhotoItsLargestIn() {
        val r = record(marked, gym, shots)
        val v = HoldReview.holds(r).first { it.id == "g1" }.view!!
        assertEquals("cap_k_002.jpg", v.photo)      // 1 m off, not 2; the one 0.5 m off wasn't kept
        assertEquals(500.0, v.u, 1e-6); assertEquals(400.0, v.v, 1e-6)
        assertEquals(32.0, v.rPx, 1e-6)
        assertNull(HoldReview.view(r, RecordMarked("x", "b", listOf(0.0, 0.0, 2.0)), 0.04))     // no photo of its spell
    }
}
