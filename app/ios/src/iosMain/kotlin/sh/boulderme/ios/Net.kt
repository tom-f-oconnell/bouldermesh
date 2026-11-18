package sh.boulderme.ios

import platform.Network.nw_interface_type_wifi
import platform.Network.nw_interface_type_wired
import platform.Network.nw_path_get_status
import platform.Network.nw_path_is_expensive
import platform.Network.nw_path_monitor_create
import platform.Network.nw_path_monitor_set_queue
import platform.Network.nw_path_monitor_set_update_handler
import platform.Network.nw_path_monitor_start
import platform.Network.nw_path_status_satisfied
import platform.Network.nw_path_uses_interface_type
import platform.darwin.dispatch_get_main_queue

/**
 * Whether the phone is on Wi-Fi (or a cable) that isn't expensive, as a phone's hotspot is: what "only over Wi-Fi" waits
 * for; and whether it has a connection at all, without which nothing is asked of the server unless pressed for.
 */
object Net {
    var onWifi = false
        private set
    var online = false
        private set
    private val monitor = nw_path_monitor_create()
    private val waiting = ArrayList<Pair<() -> Boolean, () -> Unit>>()     // await()'s, until they may go on
    private var known = false
    private val unknown = ArrayList<() -> Unit>()       // known()'s, until the first word on the network comes

    init {
        nw_path_monitor_set_update_handler(monitor) { path ->
            online = nw_path_get_status(path) == nw_path_status_satisfied
            onWifi = online && !nw_path_is_expensive(path) &&
                (nw_path_uses_interface_type(path, nw_interface_type_wifi) || nw_path_uses_interface_type(path, nw_interface_type_wired))
            if (!known) { known = true; unknown.toList().also { unknown.clear() }.forEach { it() } }
            changed()
        }
        nw_path_monitor_set_queue(monitor, dispatch_get_main_queue())
        nw_path_monitor_start(monitor)
    }

    /** Calls `then` once whether the phone is online is known (at once, but for just after the app starts), on the main thread. */
    fun known(then: () -> Unit) { if (known) then() else unknown.add(then) }

    /** Lets those waiting go on that now may: when the network changes, and when a Wi-Fi-only setting does (IosStore). */
    fun changed() {
        val go = waiting.filter { (onlyWifi, _) -> !onlyWifi() || onWifi }
        waiting.removeAll(go)
        go.forEach { it.second() }
    }

    /**
     * Calls `then`, on the main thread, once `onlyWifi()` is false or the phone is on Wi-Fi: at once if so already, else
     * after calling `waiting`, when the network or the setting changes (not looking every so often).
     */
    fun await(onlyWifi: () -> Boolean, waiting: () -> Unit, then: () -> Unit) {
        if (!onlyWifi() || onWifi) return then()
        waiting()
        this.waiting.add(onlyWifi to then)
    }
}
