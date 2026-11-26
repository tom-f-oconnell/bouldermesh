package sh.boulderme.app

import sh.boulderme.core.LocateFrame
import sh.boulderme.core.LocatePack
import sh.boulderme.core.Located
import sh.boulderme.core.Locator
import sh.boulderme.core.PhoneLocator
import sh.boulderme.core.Plan
import sh.boulderme.core.TimedLocator

/**
 * The phone's own Locator for a mesh: its locate pack, kept beside its plan, fetched the first time it's needed if it
 * isn't kept yet (and there's signal; the server may still be making it, when it's asked for again next frame).
 */
class PackLocator(private val api: Api, private val store: Store, private val mesh: String) : Locator, TimedLocator {
    @Volatile private var pack: LocatePack? = null
    @Volatile override var took: Map<String, Any?> = emptyMap()
        private set

    override suspend fun locate(plan: Plan, frames: List<LocateFrame>): List<Located> {
        val loading = pack == null
        val t0 = System.nanoTime()
        val p = pack ?: load()
        val packed = if (loading) mapOf("pack_ms" to (System.nanoTime() - t0) / 1e6, "pack" to (if (p == null) "none" else packFrom)) else emptyMap()
        if (p == null) { took = packed; return emptyList() }
        pack = p
        val phone = PhoneLocator(p)
        return phone.locate(plan, frames).also { took = packed + phone.took }
    }

    private var packFrom = ""       // where the pack came from, when it was first wanted: "kept" on the phone, or "fetched"
    private fun load(): LocatePack? = store.locatePack(mesh)?.also { packFrom = "kept" } ?: runCatching {
        val plan = store.plan(mesh) ?: return null
        api.locatePack(plan)?.let { bytes -> LocatePack.parse(bytes).also { store.saveLocatePack(mesh, bytes); packFrom = "fetched" } }
    }.getOrNull()
}

/** What places a capture's frames against its mesh: the phone itself, and the server when there's signal. */
fun locators(a: MainActivity, plan: Plan?): List<Pair<String, Locator>> =
    if (plan == null) emptyList() else listOf("phone" to PackLocator(a.api, a.store, plan.mesh), "server" to ServerLocator(a.api))
