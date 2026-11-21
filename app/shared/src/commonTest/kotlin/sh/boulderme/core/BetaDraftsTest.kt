package sh.boulderme.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class BetaDraftsTest {
    private val plan = Plan(
        mesh = "m1", frame = Frame(metresPerUnit = 0.5),
        sets = listOf(PlanSet("s1", "Beta", targets = listOf("h0", "h1")), PlanSet("s2", "Other", targets = listOf("h1", "h2"))),
        targets = listOf(
            Target("p0", "patch", listOf(0.0, 0.0, 0.0), listOf(1.0, 0.0, 0.0), 0.5),
            Target("p1", "patch", listOf(5.0, 0.0, 0.0), listOf(0.0, 0.0, 1.0), 0.5),
            Target("h0", "hold", listOf(0.0, 1.0, 0.0), listOf(1.0, 0.0, 0.0), 0.3, prio = 3, role = "start", sets = listOf("s1"), holds = listOf("a")),
            Target("h1", "hold", listOf(1.0, 1.0, 0.0), listOf(1.0, 0.0, 0.0), 0.3, prio = 3, role = "hand", sets = listOf("s1", "s2"), holds = listOf("b")),
            Target("h2", "hold", listOf(2.0, 1.0, 0.0), listOf(1.0, 0.0, 0.0), 0.3, prio = 3, role = "foot", sets = listOf("s2"), holds = listOf("c"))))

    @Test fun draftsReplaceAndAddSets() {
        val drafts = BetaDrafts.parse("""[
            {"id":"s1","name":"Beta","holds":[{"id":"a","p":[0,1,0],"role":"start"},{"p":[4.9,0.1,0],"role":"hand"}]},
            {"id":"s2","deleted":true},
            {"id":"local-x","name":"Mine","local":true,"holds":[{"id":"q","p":[0.2,0.2,0],"role":"foot","n":[0,1,0]}]}]""")
        val p = BetaDrafts.applied(plan, drafts)
        assertEquals(listOf("s1", "local-x"), p.sets.map { it.id })
        val s1 = p.sets.first { it.id == "s1" }
        assertEquals("h0", s1.targets[0])                                   // the same hold, at the same spot: the plan's
        val added = p.targets.first { it.id == s1.targets[1] }
        assertEquals(listOf(0.0, 0.0, 1.0), added.n)                       // facing as the nearest patch
        assertEquals(0.3, added.r, 1e-9)
        assertTrue(p.targets.first { it.id == "h1" }.sets.isEmpty())        // in neither set now
        assertEquals(listOf(0.0, 1.0, 0.0), p.targets.first { it.id == p.sets[1].targets[0] }.n)
    }

    @Test fun focusJoinsSetsLessThoseLeftOut() {
        val p = BetaDrafts.focused(plan, Focus(listOf("s1", "s2"), left = listOf("h0")))
        val f = p.sets.first { it.id == BetaDrafts.FOCUS }
        assertEquals(setOf("h1", "h2"), f.targets.toSet())
        assertEquals("Beta + Other", f.name)
        assertTrue(BetaDrafts.FOCUS in p.targets.first { it.id == "h2" }.sets)
        assertTrue(BetaDrafts.FOCUS !in p.targets.first { it.id == "h0" }.sets)
        assertEquals(plan, BetaDrafts.focused(plan, Focus(listOf("s1"), left = listOf("h0", "h1"))))     // nothing left: as it was
        val byHold = BetaDrafts.focused(plan, Focus(listOf("s1", "s2"), left = listOf("c")))             // a hold's own id, as the viewer has it
        assertEquals(setOf("h0", "h1"), byHold.sets.first { it.id == BetaDrafts.FOCUS }.targets.toSet())
        assertEquals(Focus(listOf("video:t1"), listOf("t1/3")), BetaDrafts.focus("""{"sets":["video:t1"],"left":["t1/3"]}"""))
    }

    @Test fun requestsForEachDraft() {
        val new = BetaDrafts.requests("m1", DraftSet("local-x", "Mine", listOf(DraftHold("q", listOf(1.0, 2.0, 3.0), "hand")), local = true))
        assertEquals(listOf("POST", "PATCH"), new.map { it.first })
        assertEquals("/api/meshes/m1/beta/sets/${BetaDrafts.ID}", new[1].second)
        assertTrue(new[1].third!!.contains("\"holds\":[{"))
        assertEquals(listOf(Triple("DELETE", "/api/meshes/m1/beta/sets/s2", null)), BetaDrafts.requests("m1", DraftSet("s2", deleted = true)))
        assertTrue(BetaDrafts.requests("m1", DraftSet("local-y", local = true, deleted = true)).isEmpty())
    }
}
