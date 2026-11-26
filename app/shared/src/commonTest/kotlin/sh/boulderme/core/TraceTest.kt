package sh.boulderme.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class TraceTest {
    @Test
    fun linesAreTurnedIntoTextOnlyWhenAsked() {
        var t = 0.0
        val tr = Trace { t }
        tr.add("phase", "to" to "GHOST", "from" to null)
        t = 1.23456
        tr.add("locate", "by" to "server", "ms" to 812.34567, "inliers" to listOf(40, 31), "took" to mapOf("http_ms" to 700.0, "error" to null))
        val lines = tr.text().trim().lines().map { Json.parseToJsonElement(it).jsonObject }
        assertEquals(2, lines.size)
        assertEquals("phase", lines[0]["e"]!!.jsonPrimitive.content)
        assertNull(lines[0]["from"], "nulls are left out")
        assertEquals(1.235, lines[1]["t"]!!.jsonPrimitive.content.toDouble())
        assertEquals(812.346, lines[1]["ms"]!!.jsonPrimitive.content.toDouble())
        assertEquals(listOf(40, 31), lines[1]["inliers"]!!.jsonArray.map { it.jsonPrimitive.content.toInt() })
        assertTrue((lines[1]["took"] as JsonObject)["http_ms"] != null)
    }

    @Test
    fun aFullTraceSaysHowManyItLeftOut() {
        val tr = Trace { 0.0 }
        repeat(Trace.MOST + 3) { tr.add("x") }
        val last = Json.parseToJsonElement(tr.text().trim().lines().last()).jsonObject
        assertEquals("dropped", last["e"]!!.jsonPrimitive.content)
        assertEquals(3, last["n"]!!.jsonPrimitive.content.toInt())
    }
}
