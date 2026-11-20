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
import sh.boulderme.core.ReviewHold
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
            makeButton("Reject all", Look.PLAIN) { keep = emptySet(); rebuild() })
        for (h in holds) holdRow(h, "${app.store.captureDir(id)}/photos", h.id in keep, crops) { keep = if (h.id in keep) keep - h.id else keep + h.id; rebuild() }
    }
}

/**
 * Over the camera, while capturing a gym climb: the holds just found, to keep (asked for close up from then on) or
 * reject. `done` with those ticked; Reject all with none; Later with null.
 */
class CheckHoldsForm(private val holds: List<ReviewHold>, private val photos: String, private val done: (Set<String>?) -> Unit) : Form("Check the holds found") {
    private var keep = holds.map { it.id }.toSet()
    private val crops = HashMap<String, Pair<UIImage, DoubleArray>?>()
    private var answered = false

    private fun answer(with: Set<String>?) {
        if (answered) return
        answered = true
        screen.dismissViewControllerAnimated(true, completion = null)
        done(with)
    }

    override fun build() {
        text("Only guesses, by colour: untick any that isn't a hold of the climb (the ring shows which). Those kept are asked for close up; " +
            "the rest are left out.", UIColor.secondaryLabelColor, small = true)
        button(if (keep.size == holds.size) "Keep all ${holds.size}" else "Keep ${keep.size}, reject ${holds.size - keep.size}", Look.FILLED) { answer(keep) }
        row(makeButton("Reject all", Look.TINTED) { answer(emptySet()) }, makeButton("Later", Look.PLAIN) { answer(null) })
        for (h in holds) holdRow(h, photos, h.id in keep, crops) { keep = if (h.id in keep) keep - h.id else keep + h.id; rebuild() }
    }
}

/** A hold to check: a piece of the photo it's seen best in (photos: the capture's photos/), ringed, where it came from, and (if found) whether it's kept. */
fun Form.holdRow(h: ReviewHold, photos: String, kept: Boolean, crops: HashMap<String, Pair<UIImage, DoubleArray>?>, toggle: () -> Unit) {
    val picture = holdCrop(h.id, h.view, photos, crops)
    val right = if (h.found) makeButton(if (kept) "✓ Kept" else "Left out", if (kept) Look.TINTED else Look.PLAIN) { toggle() }
        else makeLabel("Kept", GOOD, size = 13.0)
    row(picture, makeLabel("${h.id}\n${h.how}", size = 13.0), right)
}

private const val SIDE = 96.0

/** A square piece of the photo round a hold, ringed; read off the main thread the first time it's asked for (kept in `crops`). */
private fun holdCrop(key: String, v: HoldView?, photos: String, crops: HashMap<String, Pair<UIImage, DoubleArray>?>): UIView {
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
        val path = "$photos/${v.photo}"
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

/**
 * The piece of the photo at `path` round the hold `v` (a few times its size across), and where the hold is in it:
 * its middle and radius, as parts of the piece's side.
 */
@OptIn(ExperimentalForeignApi::class)
private fun cut(path: String, v: HoldView): Pair<UIImage, DoubleArray>? {
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
