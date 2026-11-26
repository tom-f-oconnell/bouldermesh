package sh.boulderme.app

import java.io.File
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Sends the owner's capture traces (Trace, kept in Store.debugDir("capture", <id>)) to the server's
 * debug/capture/<id>/, by itself, once a capture is over: never while one runs, so that sending doesn't change the
 * timings being measured. A file is sent again only if it changed since (a capture carried on). Only over Wi-Fi when
 * captures are (Store.uploadWifiOnly); Settings can turn it off (Store.traceSend), and send them by hand.
 */
object DebugSender {
    /** Whether a capture is running: set by the capture screen. Sending stops between files while it is. */
    @Volatile var capturing = false
    private val running = AtomicBoolean(false)

    /** Sends what's waiting, after `delayMs` (for the capture's last writes to land), on a thread of its own. */
    fun soon(store: Store, api: Api, delayMs: Long = 10_000) {
        if (!store.owner || !store.traceSend || store.token.isEmpty()) return
        if (!running.compareAndSet(false, true)) return
        Thread {
            try {
                Thread.sleep(delayMs)
                for (d in store.debugSessions("capture")) if (!send(store, api, d, "capture")) break
            } catch (_: Exception) {
            } finally { running.set(false) }
        }.start()
    }

    /**
     * Sends a session's files not sent before, or changed since (each noted in its .sent, with its size and time).
     * Returns false if it stopped short: a capture started, no signal (or Wi-Fi, if that's asked for), or the server refused one.
     */
    fun send(store: Store, api: Api, d: File, kind: String, always: Boolean = false): Boolean {
        val sentFile = File(d, ".sent")
        val sent = sentFile.takeIf { it.exists() }?.readLines()?.toMutableSet() ?: mutableSetOf()
        val todo = (d.listFiles() ?: emptyArray()).filter { it.isFile && !it.name.startsWith(".") && !it.name.endsWith(".partial") }
            .map { it to "${it.name} ${it.length()} ${it.lastModified()}" }.filter { it.second !in sent }.sortedBy { it.first.name }
        for ((f, key) in todo) {
            if (!always && (capturing || !store.online() || (store.uploadWifiOnly && !store.onWifi()))) return false
            runCatching { api.debugFile(kind, d.name, f.name, f.readBytes()) }.getOrElse { return false }
            sent += key; sentFile.appendText(key + "\n")
        }
        return true
    }

    /** Whether all of a session's files have been sent as they are now. */
    fun allSent(d: File): Boolean {
        val sent = File(d, ".sent").takeIf { it.exists() }?.readLines()?.toSet() ?: emptySet()
        return (d.listFiles() ?: emptyArray()).filter { it.isFile && !it.name.startsWith(".") && !it.name.endsWith(".partial") }
            .all { "${it.name} ${it.length()} ${it.lastModified()}" in sent }
    }
}
