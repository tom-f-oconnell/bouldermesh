package sh.boulderme.app

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.io.File
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap

/**
 * Sends captures to the server: a project of its own for each, its photos one at a time, then its record, then asks for
 * it to be meshed. What's been sent is written down as it goes (Store.Upload), so that one cut off (no signal, the app
 * closed) carries on from there when asked again.
 */
object Uploader {
    /** What each capture being sent is doing, in words, by its id. */
    val state = MutableStateFlow<Map<String, String>>(emptyMap())
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val running = ConcurrentHashMap.newKeySet<String>()

    fun busy(id: String) = id in running

    private fun say(id: String, text: String) = state.update { it + (id to text) }

    fun start(store: Store, api: Api, id: String) {
        if (!running.add(id)) return
        scope.launch {
            var u = store.upload(id)
            try {
                if (u == null) throw IOException("this capture's record is missing")
                val dir = store.captureDir(id)
                val photos = (File(dir, "photos").listFiles() ?: emptyArray()).filter { it.name.endsWith(".jpg") }.sortedBy { it.name }
                if (photos.size < 3) throw IOException("a mesh needs at least 3 photos")
                if (store.token.isEmpty()) throw IOException("the server's editing token isn't set (Settings)")
                var project = u.project
                if (project == null) {
                    say(id, "Starting…")
                    project = api.createProject(u.name)
                    u = u.copy(project = project); store.saveUpload(id, u)
                }
                for ((i, f) in photos.withIndex()) {
                    if (f.name in u!!.sent) continue
                    say(id, "Sending photo ${i + 1} of ${photos.size}")
                    api.uploadPhoto(project, f)
                    u = u.copy(sent = u.sent + f.name); store.saveUpload(id, u)
                }
                if (!u!!.recordSent) {
                    say(id, "Sending its record")
                    api.sendRecord(project, File(dir, "capture.json").readText())
                    u = u.copy(recordSent = true); store.saveUpload(id, u)
                }
                val points = File(dir, "points.bin")
                if (!u.pointsSent && points.exists()) {
                    say(id, "Sending what its tracking found")
                    api.sendPoints(project, points)
                    u = u.copy(pointsSent = true); store.saveUpload(id, u)
                }
                say(id, "Asking for it to be meshed")
                try { api.reconstruct(project) } catch (e: IOException) { if ("already running" !in (e.message ?: "")) throw e }      // asked before, and cut off
                u = u.copy(done = true, note = ""); store.saveUpload(id, u)
                say(id, "Sent: the server is meshing it")
            } catch (e: Exception) {
                val why = e.message ?: e.javaClass.simpleName
                u?.let { store.saveUpload(id, it.copy(note = why)) }
                say(id, "Stopped: $why")
            } finally {
                running.remove(id)
            }
        }
    }
}
