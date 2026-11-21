package sh.boulderme.ios

import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import platform.UIKit.UIColor
import platform.UIKit.secondaryLabelColor
import platform.WebKit.WKScriptMessage
import platform.WebKit.WKScriptMessageHandlerProtocol
import platform.WebKit.WKUserContentController
import platform.darwin.NSObject
import sh.boulderme.core.BetaDrafts
import sh.boulderme.core.DraftSet
import sh.boulderme.core.Focus
import sh.boulderme.core.Plan

/**
 * Before a capture of holds in detail: which of the mesh's sets of beta it's for (any of them), and then which of their
 * holds, each ticked to begin with, to leave out. `start` gets what was chosen (BetaDrafts.focused() makes it one set).
 */
class FocusForm(private val plan: Plan, private val start: (Focus) -> Unit) : Form("Hold closeups") {
    private val sets = plan.setsWithHolds
    private var chosen = if (sets.size == 1) setOf(sets[0].id) else emptySet()
    private var left = emptySet<String>()

    override fun build() {
        val byId = plan.targets.associateBy { it.id }
        section("Sets of beta to photograph the holds of")
        for (s in sets) {
            val unsent = s.id.startsWith(BetaDrafts.LOCAL) || plan.targets.any { it.id.startsWith("d-${s.id}-") }
            button((if (s.id in chosen) "☑ " else "☐ ") + "${s.name ?: "A set of beta"} (${s.targets.size} hold${if (s.targets.size == 1) "" else "s"})" +
                if (unsent) " · not sent yet" else "", Look.PLAIN) { chosen = if (s.id in chosen) chosen - s.id else chosen + s.id; rebuild() }
        }
        val holds = sets.filter { it.id in chosen }.flatMap { it.targets }.distinct().mapNotNull { byId[it] }
        if (holds.isNotEmpty()) {
            section("Its holds")
            text("Untick any to leave out.", UIColor.secondaryLabelColor, small = true)
        }
        for ((i, t) in holds.withIndex()) button((if (t.id in left) "☐ " else "☑ ") + "${i + 1}. " + BetaDrafts.label(plan, t, chosen), Look.PLAIN) {
            left = if (t.id in left) left - t.id else left + t.id; rebuild()
        }
        val n = holds.count { it.id !in left }
        button(if (n > 0) "Capture $n hold${if (n == 1) "" else "s"}" else "Choose some holds", Look.FILLED, enabled = n > 0) {
            start(Focus(sets.map { it.id }.filter { it in chosen }, left.filter { l -> holds.any { it.id == l } }))
        }
    }
}

/**
 * The viewer's page keeping the sets of beta it changed with no signal (its bmBeta message: {mesh, drafts}), for the app
 * to capture and send (BetaDrafts); they're given back to the page as BM_BETA_DRAFTS (script()).
 */
class BetaBridge(private val store: IosStore) : NSObject(), WKScriptMessageHandlerProtocol {
    private val meshId = Regex("[0-9a-f]{6,32}")

    override fun userContentController(userContentController: WKUserContentController, didReceiveScriptMessage: WKScriptMessage) {
        val text = didReceiveScriptMessage.body as? String ?: return
        if (text.length > 2_000_000) return
        val obj = runCatching { IosStore.json.parseToJsonElement(text).jsonObject }.getOrNull() ?: return
        val mesh = obj["mesh"]?.jsonPrimitive?.content ?: return
        if (!meshId.matches(mesh)) return
        store.saveDrafts(mesh, BetaDrafts.parse(obj["drafts"]?.toString()))
    }

    /** What the page starts with: this mesh's drafts kept on the phone. */
    fun script(mesh: String): String = "window.BM_BETA_DRAFTS = {${kotlinx.serialization.json.JsonPrimitive(mesh)}: ${BetaDrafts.write(store.drafts(mesh))}};"
}

/**
 * Sends the sets of beta changed in the viewer with no signal (BetaDrafts), when Home comes up: each as its requests
 * say, a new set's id kept as soon as the server gives it, so that it's never made twice; then the mesh's plan is got
 * again, with them in. What fails stays, to try again next time. `done` when it's finished, if anything was sent.
 */
object BetaSync {
    private var busy = false

    fun send(app: App, done: () -> Unit) {
        if (busy || app.store.token.isEmpty()) return
        val meshes = app.store.draftMeshes()
        if (meshes.isEmpty()) return
        busy = true
        fun next(i: Int) {
            if (i >= meshes.size) { busy = false; done(); return }
            mesh(app, meshes[i]) { next(i + 1) }
        }
        next(0)
    }

    private fun mesh(app: App, mesh: String, then: () -> Unit) {
        val drafts = app.store.drafts(mesh)
        fun draft(i: Int) {
            if (i >= drafts.size) {
                if (app.store.plan(mesh) != null) app.api.plan(mesh) { plan, guide, _ -> if (plan != null && guide != null) app.store.savePlan(mesh, plan, guide); then() }
                else then()
                return
            }
            val d = drafts[i]
            val steps = BetaDrafts.requests(mesh, d)
            fun step(k: Int, id: String) {
                if (k >= steps.size) {
                    app.store.saveDrafts(mesh, app.store.drafts(mesh).filter { it.id != id && it.id != d.id })
                    return draft(i + 1)
                }
                val (method, path, body) = steps[k]
                app.api.beta(method, path.replace(BetaDrafts.ID, id), body) { code, text, _ ->
                    when {
                        code == 404 && method == "DELETE" -> step(k + 1, id)
                        code !in 200..299 -> then()      // no signal, or refused: kept, to try again next time
                        method == "POST" -> {
                            val made = text?.let { runCatching { IosStore.json.parseToJsonElement(it).jsonObject["id"]?.jsonPrimitive?.content }.getOrNull() }
                            if (made == null) then() else {
                                app.store.saveDrafts(mesh, app.store.drafts(mesh).map { if (it.id == d.id) it.copy(id = made, local = false) else it })
                                step(k + 1, made)
                            }
                        }
                        else -> step(k + 1, id)
                    }
                }
            }
            step(0, d.id)
        }
        draft(0)
    }
}
