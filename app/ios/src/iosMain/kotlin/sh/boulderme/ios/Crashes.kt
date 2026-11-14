package sh.boulderme.ios

import kotlin.experimental.ExperimentalNativeApi
import platform.Foundation.NSDate
import platform.Foundation.timeIntervalSince1970
import platform.UIKit.UIDevice

/**
 * Writes what the app was doing when Kotlin code crashed to a file of its own, which goes to the server with the next
 * capture uploaded (Uploader); then the app ends as it would have. A crash in Apple's own code isn't caught here.
 */
object Crashes {
    val dir: String = Files.docs + "/crashes"

    @OptIn(ExperimentalNativeApi::class)
    fun watch() {
        Files.mkdirs(dir)
        val before = setUnhandledExceptionHook(null)
        setUnhandledExceptionHook { e ->
            runCatching {
                if (Files.list(dir).size < 10) Files.write("$dir/crash-${(NSDate().timeIntervalSince1970 * 1000).toLong()}.txt",
                    "${e::class.qualifiedName}: ${e.message}\n${IosStore.APP} on ${UIDevice.currentDevice.model}, iOS ${UIDevice.currentDevice.systemVersion}\n\n" +
                        e.stackTraceToString())
            }
            before?.invoke(e)
        }
    }
}
