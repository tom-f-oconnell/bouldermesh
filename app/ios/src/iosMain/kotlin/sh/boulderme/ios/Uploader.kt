package sh.boulderme.ios

/**
 * Sends captures to the server: a project of its own for each, its photos one at a time, then its record, then asks for
 * it to be meshed. What's been sent is written down as it goes, so that one cut off carries on from there when asked
 * again. All on the main thread, a step at a time as the server answers.
 */
class Uploader(private val store: IosStore, private val api: IosApi) {
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
        if (u.project != null) return next(id)
        say(id, "Starting…")
        api.createProject(u.name) { project, why ->
            if (project == null) stop(id, why ?: "no project") else { store.saveUpload(id, u.copy(project = project)); next(id) }
        }
    }

    /** The next thing still to send, then this again once it's gone. */
    private fun next(id: String) {
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
            else -> {
                say(id, "Asking for it to be meshed")
                api.reconstruct(project) { why ->
                    if (why != null && "already running" !in why) stop(id, why) else {
                        store.saveUpload(id, u.copy(done = true, note = ""))
                        running.remove(id)
                        say(id, "Sent: the server is meshing it")
                    }
                }
            }
        }
    }
}
