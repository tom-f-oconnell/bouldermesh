package sh.boulderme.app

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import sh.boulderme.core.MeshFresh

/**
 * Gets a mesh to keep on the phone (blocking: call off the main thread): its plan and guide, for capturing, unless it has
 * one already and `again` is false; and if `viewer` says how (with textures or not), its files for the viewer with no
 * signal, those kept before replaced.
 *
 * Args:
 *     wait: blocks until it may be fetched (Store.awaitDownloads()).
 *     progress: how much of the viewer's copy has come, 0 to 1.
 */
fun keepMesh(a: MainActivity, mesh: String, viewer: Boolean?, again: Boolean = false, wait: () -> Unit = {}, progress: (Double) -> Unit = {}) {
    if (again || a.store.plan(mesh) == null) {
        wait()
        val (plan, guide) = a.api.plan(mesh)
        a.store.savePlan(mesh, plan, guide)
        runCatching { a.store.plan(mesh)?.let { p -> a.api.locatePack(p)?.let { a.store.saveLocatePack(mesh, it) } } }     // for lining up with no taps and no signal
    }
    if (viewer != null) a.store.offline.fetch(a.api, a.store, mesh, viewer, progress = progress, wait = wait)
}

/**
 * Whether the meshes kept on the phone are as the server has them now (MeshFresh, by mesh), asked each time Home or Get
 * meshes comes up while there's a connection, never on its own; and bringing one up to date: its plan and the viewer's
 * copy got again, or for one rebuilt, the newer mesh got in its place.
 */
object MeshUpdates {
    val state = MutableStateFlow<Map<String, MeshFresh>>(emptyMap())
    val busy = MutableStateFlow<Map<String, String>>(emptyMap())     // those being brought up to date, with how far they've got
    val error = MutableStateFlow<String?>(null)

    suspend fun refresh(a: MainActivity) {
        val have = a.store.downloaded().map { it.mesh }
        if (have.isEmpty() || !a.store.online()) return
        runCatching { withContext(Dispatchers.IO) { a.api.fresh(have) } }.onSuccess { state.value = it }    // with none, what it said last stands
    }

    /** The newer copy of a kept mesh, or the newer mesh in its place (the old one then goes unless a capture to send goes by it). */
    suspend fun update(a: MainActivity, mesh: String) {
        val fresh = state.value[mesh] ?: return
        if (mesh in busy.value) return
        busy.value = busy.value + (mesh to "Starting…")
        error.value = null
        try {
            withContext(Dispatchers.IO) {
                val viewer = a.store.offline.index(mesh)?.textures
                val wait = { a.store.awaitDownloads({ true }) { busy.value = busy.value + (mesh to "Waiting for Wi-Fi") } }
                val target = fresh.newer ?: mesh
                keepMesh(a, target, viewer, again = true, wait = wait) { busy.value = busy.value + (mesh to "Getting… ${(it * 100).toInt()}%") }
                if (fresh.newer != null) {
                    val unsent = a.store.captureIds().mapNotNull { a.store.upload(it) }.filter { !it.done }.map { it.mesh }
                    if (MeshFresh.mayDrop(mesh, unsent)) a.store.removePlan(mesh)
                }
            }
            refresh(a)
        } catch (e: kotlinx.coroutines.CancellationException) { throw e
        } catch (e: Exception) {
            error.value = "Couldn't update it: ${e.message}"
        } finally {
            busy.value = busy.value - mesh
        }
    }
}
