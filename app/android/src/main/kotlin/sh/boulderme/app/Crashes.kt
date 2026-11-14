package sh.boulderme.app

import android.os.Build
import java.io.File

/**
 * Writes what the app was doing when it crashed to a file of its own (Store.crashes), which goes to the server with
 * the next capture uploaded; then lets Android end the app as it would have.
 */
object Crashes {
    fun watch(dir: File) {
        val before = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, e ->
            runCatching {
                val files = dir.listFiles().orEmpty()
                if (files.size < 10) File(dir, "crash-${System.currentTimeMillis()}.txt").writeText(
                    "${e.javaClass.name}: ${e.message}\n${BuildInfo.APP} on ${Build.MANUFACTURER} ${Build.MODEL}, Android ${Build.VERSION.RELEASE} (${Build.VERSION.SDK_INT}), " +
                        "thread ${thread.name}, ${java.util.Date()}\n\n${e.stackTraceToString()}")
            }
            before?.uncaughtException(thread, e)
        }
    }
}
