package sh.boulderme.ios

import sh.boulderme.core.LocateFrame
import sh.boulderme.core.Located
import sh.boulderme.core.Locator
import sh.boulderme.core.PhoneLocator
import sh.boulderme.core.Plan

/** Gets a mesh's locate pack to keep beside its plan, if it isn't kept already and the server has one made. */
fun fetchLocatePack(store: IosStore, api: IosApi, mesh: String) {
    if (store.locatePack(mesh) != null) return
    val plan = store.plan(mesh) ?: return
    api.locatePack(plan) { bytes -> if (bytes != null) store.saveLocatePack(mesh, bytes) }
}

/** The phone's own Locator for a mesh: its kept locate pack (nothing placed while none is kept). Call off the main thread. */
class IosPackLocator(private val store: IosStore, private val mesh: String) : Locator {
    private var phone: PhoneLocator? = null

    override suspend fun locate(plan: Plan, frames: List<LocateFrame>): List<Located> {
        val p = phone ?: store.locatePack(mesh)?.let { PhoneLocator(it) }?.also { phone = it } ?: return emptyList()
        return p.locate(plan, frames)
    }
}

/** What places a capture's frames against its mesh: the phone itself, and the server when there's signal. */
fun locators(app: App, plan: Plan?): List<Pair<String, Locator>> =
    if (plan == null) emptyList() else listOf("phone" to IosPackLocator(app.store, plan.mesh), "server" to IosServerLocator(app.api))
