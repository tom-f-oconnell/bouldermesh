package sh.boulderme.ios

import kotlinx.cinterop.ExperimentalForeignApi
import kotlinx.cinterop.addressOf
import kotlinx.cinterop.usePinned
import platform.AVFAudio.AVAudioPlayer
import platform.AVFAudio.AVAudioSession
import platform.AVFAudio.AVAudioSessionCategoryOptionMixWithOthers
import platform.AVFAudio.AVAudioSessionCategoryPlayback
import platform.AVFAudio.setActive
import platform.Foundation.NSData
import platform.Foundation.dataWithBytes
import sh.boulderme.core.Chime

/** Plays the capture's sounds (Chime), even with the ring switch on silent, over any music already playing. */
@OptIn(ExperimentalForeignApi::class)
class Sounds {
    private val ready by lazy {
        runCatching {
            AVAudioSession.sharedInstance().setCategory(AVAudioSessionCategoryPlayback, AVAudioSessionCategoryOptionMixWithOthers, null)
            AVAudioSession.sharedInstance().setActive(true, null)
        }
    }
    private fun player(samples: ShortArray): AVAudioPlayer? {
        val wav = Chime.wav(samples)
        val data = wav.usePinned { NSData.dataWithBytes(it.addressOf(0), wav.size.toULong()) }
        return AVAudioPlayer(data = data, error = null).also { it.prepareToPlay() }
    }
    private val ding by lazy { player(Chime.ding) }
    private val tick by lazy { player(Chime.tick) }

    fun ding() = play(ding)
    fun tick() = play(tick)

    private fun play(p: AVAudioPlayer?) {
        ready
        p ?: return
        p.stop(); p.currentTime = 0.0; p.play()
    }
}
