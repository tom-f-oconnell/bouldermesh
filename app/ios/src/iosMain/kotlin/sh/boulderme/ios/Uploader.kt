package sh.boulderme.ios

import sh.boulderme.core.HoldReview

/**
 * Sends captures to the server: a project of its own for each, its photos one at a time, then its record, then asks for
 * it to be meshed. What's been sent is written down as it goes, so that one cut off carries on from there when asked
 * again. All on the main thread, a step at a time as the server answers.
 */
class Uploader(private val store: IosStore, private val api: IosApi) {
    companion object {
        /** What one that's waiting says: captures are sent only over Wi-Fi (IosStore.uploadWifiOnly), and it isn't on one. */
        const val WAITING = "Waiting for Wi-Fi: captures are sent only over Wi-Fi (Settings)"
    }

    /** What each capture being sent is doing, in words, by its id. */
    val state = HashMap<String, String>()
    private val running = HashSet<String>()
    var onChange: (() -> Unit)? = null

    fun busy(id: String) = id in running

    private fun say(id: String, text: String) { state[id] = text; onChange?.invoke() }

    private fun stop(id: String, why: String) {
        store.upload(id)?.let { store.saveUpload(id, it.copy(note = why)) }
        running.remove(id)
        say(id, "Stopped: $why")
    }

    fun start(id: String) {
        if (!running.add(id)) return
        val u = store.upload(id)
        if (u == null) return stop(id, "this capture's record is missing")
        if (store.photos(id).size < 3) return stop(id, "a mesh needs at least 3 photos")
        if (store.token.isEmpty()) return stop(id, "the server's editing token isn't set (Settings)")
        if (!u.recordSent && HoldReview.parse(Files.readText("${store.captureDir(id)}/capture.json"))?.let { HoldReview.unchecked(it).isNotEmpty() } == true)
            return stop(id, "check the holds the app found first (Check its holds)")
        if (u.project != null) return next(id)
        say(id, "Starting…")
        wifi(id) { api.acceptTerms { api.createProject(u.name) { project, why ->
            if (project == null) stop(id, why ?: "no project") else { store.saveUpload(id, u.copy(project = project)); next(id) }
        } } }
    }

    /** Before each thing sent: if it's to go only over Wi-Fi, waits for one, then on with what it was doing. */
    private fun wifi(id: String, then: () -> Unit) {
        val was = state[id]
        var waited = false
        Net.await({ store.uploadWifiOnly }, { waited = true; say(id, WAITING) }) { if (waited) was?.let { say(id, it) }; then() }
    }

    /** The next thing still to send, then this again once it's gone. */
    private fun next(id: String) = wifi(id) { step(id) }

    private fun step(id: String) {
        val u = store.upload(id)
        val project = u?.project
        if (u == null || project == null) return stop(id, "this capture's record is missing")
        val photos = store.photos(id)
        val name = photos.firstOrNull { it !in u.sent }
        val dir = store.captureDir(id)
        when {
            name != null -> {
                say(id, "Sending photo ${u.sent.size + 1} of ${photos.size}")
                api.uploadPhoto(project, "$dir/photos/$name", name) { why -> if (why != null) stop(id, why) else { store.saveUpload(id, u.copy(sent = u.sent + name)); next(id) } }
            }
            !u.recordSent -> {
                say(id, "Sending its record")
                val record = Files.readText("$dir/capture.json")
                if (record == null) stop(id, "this capture's record is missing")
                else api.sendRecord(project, record) { why -> if (why != null) stop(id, why) else { store.saveUpload(id, u.copy(recordSent = true)); next(id) } }
            }
            !u.pointsSent && Files.exists("$dir/points.bin") -> {
                say(id, "Sending what its tracking found")
                api.sendPoints(project, "$dir/points.bin") { why -> if (why != null) stop(id, why) else { store.saveUpload(id, u.copy(pointsSent = true)); next(id) } }
            }
            // how it went on the phone, and any crash since: for working out what happened; neither holds the rest up
            !u.logSent && Files.exists("$dir/capture-log.jsonl") -> {
                say(id, "Sending how it went")
                api.sendLog(project, "log", "$dir/capture-log.jsonl") { _ -> store.saveUpload(id, u.copy(logSent = true)); next(id) }
            }
            Files.list(Crashes.dir).isNotEmpty() -> {
                val crash = Files.list(Crashes.dir).first()
                api.sendLog(project, "crash", "${Crashes.dir}/$crash") { _ -> Files.delete("${Crashes.dir}/$crash"); next(id) }
            }
            !u.meshAsked -> {
                say(id, "Asking for it to be meshed")
                api.reconstruct(project) { why ->
                    if (why != null && "already running" !in why) stop(id, why) else { store.saveUpload(id, u.copy(meshAsked = true)); next(id) }
                }
            }
            // then the scenery round the rock, last, as it's the most to send and only a backdrop
            store.scenery(id).any { it !in u.scenerySent } -> {
                val all = store.scenery(id)
                val frame = all.first { it !in u.scenerySent }
                say(id, "Sending the scenery: ${u.scenerySent.size + 1} of ${all.size}")
                api.uploadScenery(project, "$dir/scenery/$frame", frame) { why -> if (why != null) stop(id, why) else { store.saveUpload(id, u.copy(scenerySent = u.scenerySent + frame)); next(id) } }
            }
            store.scenery(id).isNotEmpty() && !u.sceneryDone -> api.sceneryDone(project) { why ->
                if (why != null) stop(id, why) else { store.saveUpload(id, u.copy(sceneryDone = true)); next(id) }
            }
            else -> {
                store.saveUpload(id, u.copy(done = true, note = ""))
                running.remove(id)
                say(id, "Sent: the server is meshing it")
            }
        }
    }
}
