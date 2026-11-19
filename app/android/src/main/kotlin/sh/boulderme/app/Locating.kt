package sh.boulderme.app

import sh.boulderme.core.LocateFrame
import sh.boulderme.core.LocatePack
import sh.boulderme.core.Located
import sh.boulderme.core.Locator
import sh.boulderme.core.PhoneLocator
import sh.boulderme.core.Plan

/**
 * The phone's own Locator for a mesh: its locate pack, kept beside its plan, fetched the first time it's needed if it
 * isn't kept yet (and there's signal; the server may still be making it, when it's asked for again next frame).
 */
class PackLocator(private val api: Api, private val store: Store, private val mesh: String) : Locator {
    @Volatile private var pack: LocatePack? = null

    override suspend fun locate(plan: Plan, frames: List<LocateFrame>): List<Located> {
        val p = pack ?: load() ?: return emptyList()
        pack = p
        return PhoneLocator(p).locate(plan, frames)
    }

    private fun load(): LocatePack? = store.locatePack(mesh) ?: runCatching {
        val plan = store.plan(mesh) ?: return null
        api.locatePack(plan)?.let { bytes -> LocatePack.parse(bytes).also { store.saveLocatePack(mesh, bytes) } }
    }.getOrNull()
}

/** What places a capture's frames against its mesh: the phone itself, and the server when there's signal. */
fun locators(a: MainActivity, plan: Plan?): List<Pair<String, Locator>> =
    if (plan == null) emptyList() else listOf("phone" to PackLocator(a.api, a.store, plan.mesh), "server" to ServerLocator(a.api))
