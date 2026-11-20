package sh.boulderme.ios

import kotlinx.cinterop.ExperimentalForeignApi
import platform.CoreGraphics.CGImageCreateWithImageInRect
import platform.CoreGraphics.CGImageGetHeight
import platform.CoreGraphics.CGImageGetWidth
import platform.CoreGraphics.CGImageRelease
import platform.CoreGraphics.CGRectMake
import platform.UIKit.NSLayoutConstraint
import platform.UIKit.UIColor
import platform.UIKit.UIImage
import platform.UIKit.UIImageView
import platform.UIKit.UIView
import platform.UIKit.UIViewContentMode
import platform.UIKit.navigationController
import platform.UIKit.secondaryLabelColor
import platform.UIKit.secondarySystemBackgroundColor
import platform.darwin.DISPATCH_QUEUE_PRIORITY_DEFAULT
import platform.darwin.dispatch_async
import platform.darwin.dispatch_get_global_queue
import platform.darwin.dispatch_get_main_queue
import sh.boulderme.core.CaptureRecord
import sh.boulderme.core.HoldReview
import sh.boulderme.core.HoldView
import kotlin.math.max
import kotlin.math.min

/** A capture's record (capture.json), or null if it can't be read. */
fun captureRecord(app: App, id: String): CaptureRecord? = HoldReview.parse(Files.readText("${app.store.captureDir(id)}/capture.json"))

/**
 * Before uploading: the holds the app found by itself, each with where it came from and a piece of the photo it's
 * seen best in, ringed, to keep or leave out. Those tapped are listed too, kept. Done writes the record (HoldReview.keep).
 */
class HoldsForm(private val app: App, private val id: String) : Form("Check its holds") {
    private val record = captureRecord(app, id)
    private val holds = record?.let { HoldReview.holds(it) } ?: emptyList()
    private var keep = holds.filter { it.found }.map { it.id }.toSet()
    private val crops = HashMap<String, Pair<UIImage, DoubleArray>?>()      // each hold's piece of photo, and where in it the hold is

    override fun build() {
        val r = record
        if (r == null) { text("This capture's record can't be read."); return }
        HoldReview.said(r)?.let { text(it) }
        val found = holds.count { it.found }
        if (found > 0) text("Those the app found are only guesses: untick any that isn't a hold of the climb (the ring shows which). " +
            "Only those ticked are uploaded and added to the mesh.", UIColor.secondaryLabelColor, small = true)
        val dropped = found - keep.size
        button(if (dropped == 0) "Keep all ${holds.size}" else "Keep ${holds.size - dropped}, leave out $dropped", Look.FILLED) {
            Files.write("${app.store.captureDir(id)}/capture.json", HoldReview.keep(r, keep).json())
            screen.navigationController?.popViewControllerAnimated(true)
        }
        row(makeButton("Tick all", Look.PLAIN) { keep = holds.filter { it.found }.map { it.id }.toSet(); rebuild() },
            makeButton("Untick all", Look.PLAIN) { keep = emptySet(); rebuild() })
        for (h in holds) {
            val picture = crop(h.id, h.view)
            val toggle = if (h.found) makeButton(if (h.id in keep) "✓ Kept" else "Left out", if (h.id in keep) Look.TINTED else Look.PLAIN) {
                keep = if (h.id in keep) keep - h.id else keep + h.id; rebuild()
            } else makeLabel("Kept", GOOD, size = 13.0)
            row(picture, makeLabel("${h.id}\n${h.how}", size = 13.0), toggle)
        }
    }

    /** A square piece of the photo round a hold, ringed; read off the main thread the first time it's asked for. */
    private fun crop(key: String, v: HoldView?): UIView {
        val box = UIImageView()
        box.backgroundColor = UIColor.secondarySystemBackgroundColor
        box.contentMode = UIViewContentMode.UIViewContentModeScaleAspectFill
        box.clipsToBounds = true
        box.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activateConstraints(listOf(box.widthAnchor.constraintEqualToConstant(SIDE), box.heightAnchor.constraintEqualToConstant(SIDE)))
        if (v == null) return box
        val ring = UIView()
        ring.translatesAutoresizingMaskIntoConstraints = false
        ring.layer.borderColor = UIColor.colorWithRed(1.0, green = 0.0, blue = 1.0, alpha = 1.0).CGColor
        ring.layer.borderWidth = 2.0
        ring.hidden = true
        box.addSubview(ring)
        fun show(got: Pair<UIImage, DoubleArray>?) {
            if (got == null) return
            box.image = got.first
            val (cx, cy, rr) = got.second.toList()
            val d = max(rr * 2 * SIDE, 10.0)
            ring.layer.cornerRadius = d / 2
            NSLayoutConstraint.activateConstraints(listOf(ring.widthAnchor.constraintEqualToConstant(d), ring.heightAnchor.constraintEqualToConstant(d),
                ring.centerXAnchor.constraintEqualToAnchor(box.leadingAnchor, constant = cx * SIDE),
                ring.centerYAnchor.constraintEqualToAnchor(box.topAnchor, constant = cy * SIDE)))
            ring.hidden = false
        }
        if (crops.containsKey(key)) show(crops[key])
        else {
            val path = "${app.store.captureDir(id)}/photos/${v.photo}"
            dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT.toLong(), 0u)) {
                val got = cut(path, v)
                dispatch_async(dispatch_get_main_queue()) {
                    crops[key] = got
                    show(got)
                }
            }
        }
        return box
    }

    companion object {
        const val SIDE = 96.0

        /**
         * The piece of the photo at `path` round the hold `v` (a few times its size across), and where the hold is in it:
         * its middle and radius, as parts of the piece's side.
         */
        @OptIn(ExperimentalForeignApi::class)
        fun cut(path: String, v: HoldView): Pair<UIImage, DoubleArray>? {
            val whole = UIImage.imageWithContentsOfFile(path)?.CGImage ?: return null
            val w = CGImageGetWidth(whole).toDouble(); val h = CGImageGetHeight(whole).toDouble()
            if (w <= 0 || h <= 0) return null
            val k = w / v.w         // the lens is for the picture as stored; scaled, should the file be another size
            val u = v.u * k; val y = v.v * k; val r = v.rPx * k
            val side = min(min(w, h), max(r * 6, min(w, h) * 0.2))
            val x0 = (u - side / 2).coerceIn(0.0, w - side); val y0 = (y - side / 2).coerceIn(0.0, h - side)
            val piece = CGImageCreateWithImageInRect(whole, CGRectMake(x0, y0, side, side)) ?: return null
            val image = UIImage.imageWithCGImage(piece)
            CGImageRelease(piece)
            return image to doubleArrayOf((u - x0) / side, (y - y0) / side, max(r * 1.3, 6.0) / side)
        }
    }
}
