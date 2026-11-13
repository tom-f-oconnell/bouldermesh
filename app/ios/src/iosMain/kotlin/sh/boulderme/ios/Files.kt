package sh.boulderme.ios

import kotlinx.cinterop.addressOf
import kotlinx.cinterop.usePinned
import platform.Foundation.NSData
import platform.Foundation.NSDate
import platform.Foundation.NSDocumentDirectory
import platform.Foundation.NSFileManager
import platform.Foundation.NSFileModificationDate
import platform.Foundation.NSSearchPathForDirectoriesInDomains
import platform.Foundation.NSUserDomainMask
import platform.Foundation.dataWithBytes
import platform.Foundation.dataWithContentsOfFile
import platform.Foundation.timeIntervalSince1970
import platform.Foundation.writeToFile
import platform.posix.memcpy

fun NSData.toBytes(): ByteArray {
    val n = length.toInt()
    val out = ByteArray(n)
    if (n > 0) out.usePinned { memcpy(it.addressOf(0), bytes, length) }
    return out
}

fun ByteArray.toData(): NSData = if (isEmpty()) NSData() else usePinned { NSData.dataWithBytes(it.addressOf(0), size.toULong()) }

/** Files in the app's own folder, by path. */
object Files {
    private val manager get() = NSFileManager.defaultManager
    val docs: String = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, true).first() as String

    fun mkdirs(path: String) { manager.createDirectoryAtPath(path, withIntermediateDirectories = true, attributes = null, error = null) }
    fun exists(path: String): Boolean = manager.fileExistsAtPath(path)
    fun readBytes(path: String): ByteArray? = NSData.dataWithContentsOfFile(path)?.toBytes()
    fun readText(path: String): String? = readBytes(path)?.decodeToString()
    fun write(path: String, data: NSData): Boolean = data.writeToFile(path, atomically = true)
    fun write(path: String, bytes: ByteArray): Boolean = write(path, bytes.toData())
    fun write(path: String, text: String): Boolean = write(path, text.encodeToByteArray())
    fun list(path: String): List<String> = (manager.contentsOfDirectoryAtPath(path, error = null) ?: emptyList<Any?>()).mapNotNull { it as? String }.sorted()
    fun delete(path: String) { manager.removeItemAtPath(path, error = null) }
    fun modified(path: String): Double = (manager.attributesOfItemAtPath(path, error = null)?.get(NSFileModificationDate) as? NSDate)?.timeIntervalSince1970 ?: 0.0
}
