package sh.boulderme.app

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import sh.boulderme.core.BetaDrafts
import java.net.HttpURLConnection
import java.net.URL

/**
 * Sends the sets of beta changed in the viewer with no signal (BetaDrafts), once Home comes up with signal: each as its
 * requests say, a new set's id kept as soon as the server gives it, so that it's never made twice. Then the mesh's plan
 * is got again, with them in, and the drafts sent go. What fails stays, to try again next time.
 */
object BetaSync {
    @Volatile private var busy = false

    suspend fun send(a: MainActivity) {
        if (busy || !a.store.online() || a.store.token.isEmpty()) return
        busy = true
        try { withContext(Dispatchers.IO) { for (mesh in a.store.draftMeshes()) runCatching { sendMesh(a, mesh) } } }
        finally { busy = false }
    }

    private fun sendMesh(a: MainActivity, mesh: String) {
        var left = a.store.drafts(mesh)
        for (d in left.toList()) {
            var id = d.id
            for ((method, path, body) in BetaDrafts.requests(mesh, d)) {
                val (code, text) = call(a, method, path.replace(BetaDrafts.ID, id), body)
                if (code == 404 && method == "DELETE") continue     // gone already
                if (code !in 200..299) throw java.io.IOException("the server answered $code")
                if (method == "POST") {
                    id = Store.json.parseToJsonElement(text).jsonObject["id"]!!.jsonPrimitive.content
                    left = left.map { if (it === d) it.copy(id = id, local = false) else it }
                    a.store.saveDrafts(mesh, left)
                }
            }
            left = left.filter { it.id != id && it.id != d.id }
            a.store.saveDrafts(mesh, left)
        }
        if (a.store.plan(mesh) != null) runCatching { val (plan, guide) = a.api.plan(mesh); a.store.savePlan(mesh, plan, guide) }
    }

    private fun call(a: MainActivity, method: String, path: String, body: String?): Pair<Int, String> {
        val c = URL(a.store.server.trimEnd('/') + path).openConnection() as HttpURLConnection
        c.requestMethod = method
        c.connectTimeout = 15_000; c.readTimeout = 60_000
        c.setRequestProperty("User-Agent", "BoulderMeshApp/${BuildInfo.VERSION} (Android)")
        c.setRequestProperty("Authorization", "Bearer ${a.store.token}")
        if (body != null) { c.doOutput = true; c.setRequestProperty("Content-Type", "application/json"); c.outputStream.use { it.write(body.toByteArray()) } }
        try {
            val code = c.responseCode
            return code to ((if (code in 200..299) c.inputStream else c.errorStream)?.use { it.readBytes().decodeToString() } ?: "")
        } finally { c.disconnect() }
    }
}
