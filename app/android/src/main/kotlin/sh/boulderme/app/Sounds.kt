package sh.boulderme.app

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import sh.boulderme.core.Chime

/** Plays the capture's sounds (Chime), each set up once; at the media volume, so that the phone's volume keys set it. */
class Sounds {
    private fun track(samples: ShortArray): AudioTrack? = runCatching {
        AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
            .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(Chime.RATE).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setBufferSizeInBytes(samples.size * 2).setTransferMode(AudioTrack.MODE_STATIC).build()
            .also { it.write(samples, 0, samples.size) }
    }.getOrNull()

    private val ding by lazy { track(Chime.ding) }
    private val tick by lazy { track(Chime.tick) }

    fun ding() = play(ding)
    fun tick() = play(tick)

    private fun play(t: AudioTrack?) {
        t ?: return
        runCatching { t.stop(); t.reloadStaticData(); t.play() }
    }

    fun release() { runCatching { ding?.release(); tick?.release() } }
}
