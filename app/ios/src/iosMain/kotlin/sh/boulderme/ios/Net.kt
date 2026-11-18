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
import platform.darwin.DISPATCH_TIME_NOW
import platform.darwin.dispatch_after
import platform.darwin.dispatch_get_main_queue
import platform.darwin.dispatch_time

/** Whether the phone is on Wi-Fi (or a cable) that isn't expensive, as a phone's hotspot is: what "only over Wi-Fi" waits for. */
object Net {
    var onWifi = false
        private set
    private val monitor = nw_path_monitor_create()

    init {
        nw_path_monitor_set_update_handler(monitor) { path ->
            onWifi = nw_path_get_status(path) == nw_path_status_satisfied && !nw_path_is_expensive(path) &&
                (nw_path_uses_interface_type(path, nw_interface_type_wifi) || nw_path_uses_interface_type(path, nw_interface_type_wired))
        }
        nw_path_monitor_set_queue(monitor, dispatch_get_main_queue())
        nw_path_monitor_start(monitor)
    }

    /**
     * Calls `then`, on the main thread, once `onlyWifi()` is false or the phone is on Wi-Fi: at once if so already, else
     * looking every 2 s, after calling `waiting`.
     */
    fun await(onlyWifi: () -> Boolean, waiting: () -> Unit, then: () -> Unit) {
        if (!onlyWifi() || onWifi) return then()
        waiting()
        fun look() {
            if (!onlyWifi() || onWifi) then()
            else dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 2_000_000_000L), dispatch_get_main_queue()) { look() }
        }
        look()
    }
}
