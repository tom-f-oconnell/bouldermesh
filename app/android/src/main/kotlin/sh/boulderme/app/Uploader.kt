package sh.boulderme.app

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
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
    /** What one that's waiting says: captures are sent only over Wi-Fi (Store.uploadWifiOnly), and it isn't on one. */
    const val WAITING = "Waiting for Wi-Fi: captures are sent only over Wi-Fi (Settings)"

    /** What each capture being sent is doing, in words, by its id. */
    val state = MutableStateFlow<Map<String, String>>(emptyMap())
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val running = ConcurrentHashMap.newKeySet<String>()

    fun busy(id: String) = id in running

    private fun say(id: String, text: String) = state.update { it + (id to text) }

    fun start(store: Store, api: Api, id: String) {
        if (!running.add(id)) return
        scope.launch {
            // before each thing sent: if it's to go only over Wi-Fi, until there's one, then on with what it was doing
            suspend fun wifi() {
                val was = state.value[id]
                if (!store.uploadWifiOnly || store.onWifi()) return
                say(id, WAITING)
                while (store.uploadWifiOnly && !store.onWifi()) delay(2000)
                was?.let { say(id, it) }
            }
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
                    wifi()
                    api.acceptTerms()
                    project = api.createProject(u.name)
                    u = u.copy(project = project); store.saveUpload(id, u)
                }
                for ((i, f) in photos.withIndex()) {
                    if (f.name in u!!.sent) continue
                    say(id, "Sending photo ${i + 1} of ${photos.size}")
                    wifi()
                    api.uploadPhoto(project, f)
                    u = u.copy(sent = u.sent + f.name); store.saveUpload(id, u)
                }
                if (!u!!.recordSent) {
                    say(id, "Sending its record")
                    wifi()
                    api.sendRecord(project, File(dir, "capture.json").readText())
                    u = u.copy(recordSent = true); store.saveUpload(id, u)
                }
                val points = File(dir, "points.bin")
                if (!u.pointsSent && points.exists()) {
                    say(id, "Sending what its tracking found")
                    wifi()
                    api.sendPoints(project, points)
                    u = u.copy(pointsSent = true); store.saveUpload(id, u)
                }
                // how it went on the phone, and any crash since: for working out what happened; neither holds the rest up
                val log = File(dir, "capture-log.jsonl")
                if (!u.logSent && log.exists()) {
                    say(id, "Sending how it went")
                    wifi()
                    runCatching { api.sendLog(project, "log", log) }.onSuccess {
                        u = u!!.copy(logSent = true); store.saveUpload(id, u!!)
                    }
                }
                for (crash in store.crashes.listFiles().orEmpty().sortedBy { it.name }) runCatching { api.sendLog(project, "crash", crash); crash.delete() }
                say(id, "Asking for it to be meshed")
                wifi()
                try { api.reconstruct(project) } catch (e: IOException) { if ("already running" !in (e.message ?: "")) throw e }      // asked before, and cut off
                u = u!!.copy(meshAsked = true); store.saveUpload(id, u!!)
                // then the scenery round the rock, last, as it's the most to send and only a backdrop
                val frames = (File(dir, "scenery").listFiles() ?: emptyArray()).filter { it.name.endsWith(".jpg") }.sortedBy { it.name }
                for ((i, f) in frames.withIndex()) {
                    if (f.name in u!!.scenerySent) continue
                    say(id, "Sending the scenery: ${i + 1} of ${frames.size}")
                    wifi()
                    api.uploadScenery(project, f)
                    u = u.copy(scenerySent = u.scenerySent + f.name); store.saveUpload(id, u)
                }
                if (frames.isNotEmpty() && !u!!.sceneryDone) { wifi(); api.sceneryDone(project); u = u.copy(sceneryDone = true); store.saveUpload(id, u) }
                u = u!!.copy(done = true, note = ""); store.saveUpload(id, u)
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
