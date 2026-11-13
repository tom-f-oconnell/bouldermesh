package sh.boulderme.ios

import platform.Foundation.NSTimer
import platform.UIKit.NSLayoutConstraint
import platform.UIKit.UIAction
import platform.UIKit.UIAlertAction
import platform.UIKit.UIAlertActionStyleCancel
import platform.UIKit.UIAlertActionStyleDefault
import platform.UIKit.UIAlertActionStyleDestructive
import platform.UIKit.UIAlertController
import platform.UIKit.UIAlertControllerStyleAlert
import platform.UIKit.UIButton
import platform.UIKit.UIButtonConfiguration
import platform.UIKit.UIColor
import platform.UIKit.UIControlEventEditingChanged
import platform.UIKit.UIFont
import platform.UIKit.UILabel
import platform.UIKit.UILayoutConstraintAxisHorizontal
import platform.UIKit.UILayoutConstraintAxisVertical
import platform.UIKit.UIProgressView
import platform.UIKit.UIProgressViewStyle
import platform.UIKit.UIScrollView
import platform.UIKit.UIStackView
import platform.UIKit.UITextAutocapitalizationType
import platform.UIKit.UITextAutocorrectionType
import platform.UIKit.UITextBorderStyle
import platform.UIKit.UITextField
import platform.UIKit.UIView
import platform.UIKit.UIViewController
import platform.UIKit.navigationController
import platform.UIKit.secondaryLabelColor
import platform.UIKit.systemBackgroundColor
import platform.UIKit.systemGreenColor
import platform.UIKit.systemOrangeColor
import platform.UIKit.systemRedColor

// red is for what's wrong, green for what's right, orange for a warning: nothing else is coloured with them
val GOOD: UIColor get() = UIColor.systemGreenColor
val WARN: UIColor get() = UIColor.systemOrangeColor
val BAD: UIColor get() = UIColor.systemRedColor

enum class Look { FILLED, TINTED, PLAIN }

/** A button that does `action` when pressed. */
fun makeButton(title: String, look: Look = Look.TINTED, enabled: Boolean = true, color: UIColor? = null, action: () -> Unit): UIButton {
    val config = when (look) {
        Look.FILLED -> UIButtonConfiguration.filledButtonConfiguration()
        Look.TINTED -> UIButtonConfiguration.tintedButtonConfiguration()
        Look.PLAIN -> UIButtonConfiguration.plainButtonConfiguration()
    }
    if (color != null) { if (look == Look.FILLED) config.baseBackgroundColor = color else config.baseForegroundColor = color }
    val b = UIButton.buttonWithConfiguration(config, primaryAction = UIAction.actionWithTitle(title, image = null, identifier = null) { _ -> action() })
    b.enabled = enabled
    return b
}

fun makeLabel(text: String, color: UIColor? = null, bold: Boolean = false, size: Double = 16.0): UILabel {
    val l = UILabel()
    l.text = text
    l.numberOfLines = 0
    l.font = if (bold) UIFont.boldSystemFontOfSize(size) else UIFont.systemFontOfSize(size)
    if (color != null) l.textColor = color
    return l
}

fun makeRow(vararg views: UIView): UIStackView {
    val row = UIStackView()
    row.axis = UILayoutConstraintAxisHorizontal
    row.spacing = 8.0
    for (v in views) row.addArrangedSubview(v)
    val rest = UIView()      // takes up what's left, so that the others keep their own widths
    rest.setContentHuggingPriority(1f, forAxis = UILayoutConstraintAxisHorizontal)
    row.addArrangedSubview(rest)
    return row
}

/** Asks a yes or no. */
fun UIViewController.confirm(title: String, message: String, yes: String, no: String, danger: Boolean = false, then: () -> Unit) {
    val alert = UIAlertController.alertControllerWithTitle(title, message, UIAlertControllerStyleAlert)
    alert.addAction(UIAlertAction.actionWithTitle(no, UIAlertActionStyleCancel, null))
    alert.addAction(UIAlertAction.actionWithTitle(yes, if (danger) UIAlertActionStyleDestructive else UIAlertActionStyleDefault) { _ -> then() })
    presentViewController(alert, animated = true, completion = null)
}

/** Asks for a line of text. */
fun UIViewController.ask(title: String, placeholder: String, ok: String, then: (String) -> Unit) {
    val alert = UIAlertController.alertControllerWithTitle(title, null, UIAlertControllerStyleAlert)
    alert.addTextFieldWithConfigurationHandler { field -> field?.placeholder = placeholder }
    alert.addAction(UIAlertAction.actionWithTitle("Cancel", UIAlertActionStyleCancel, null))
    alert.addAction(UIAlertAction.actionWithTitle(ok, UIAlertActionStyleDefault) { _ ->
        val text = ((alert.textFields?.firstOrNull() as? UITextField)?.text ?: "").trim()
        if (text.isNotEmpty()) then(text)
    })
    presentViewController(alert, animated = true, completion = null)
}

/**
 * A screen that's a column of text, fields and buttons, scrolling if it's long: what its Form builds. (A screen is an
 * Objective-C class, which Kotlin can't yet make a base class of its own from; so each screen's own part is a Form.)
 */
class FormController(private val form: Form) : UIViewController(nibName = null, bundle = null) {
    internal val stack = UIStackView()
    internal var timer: NSTimer? = null

    override fun viewDidLoad() {
        super.viewDidLoad()
        title = form.heading
        view.backgroundColor = UIColor.systemBackgroundColor
        val scroll = UIScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(scroll)
        stack.axis = UILayoutConstraintAxisVertical
        stack.spacing = 10.0
        stack.translatesAutoresizingMaskIntoConstraints = false
        scroll.addSubview(stack)
        NSLayoutConstraint.activateConstraints(listOf(
            scroll.topAnchor.constraintEqualToAnchor(view.safeAreaLayoutGuide.topAnchor),
            scroll.bottomAnchor.constraintEqualToAnchor(view.bottomAnchor),
            scroll.leadingAnchor.constraintEqualToAnchor(view.leadingAnchor),
            scroll.trailingAnchor.constraintEqualToAnchor(view.trailingAnchor),
            stack.topAnchor.constraintEqualToAnchor(scroll.contentLayoutGuide.topAnchor, constant = 12.0),
            stack.bottomAnchor.constraintEqualToAnchor(scroll.contentLayoutGuide.bottomAnchor, constant = -24.0),
            stack.leadingAnchor.constraintEqualToAnchor(scroll.frameLayoutGuide.leadingAnchor, constant = 16.0),
            stack.trailingAnchor.constraintEqualToAnchor(scroll.frameLayoutGuide.trailingAnchor, constant = -16.0),
        ))
    }

    override fun viewWillAppear(animated: Boolean) {
        super.viewWillAppear(animated)
        form.appear()
        form.rebuild()
    }

    override fun viewWillDisappear(animated: Boolean) {
        super.viewWillDisappear(animated)
        form.every(0.0) {}
    }
}

/**
 * What's on one screen: build() adds its text, fields and buttons; rebuild() clears it and builds it again, which is
 * how anything on it changes.
 */
abstract class Form(val heading: String) {
    val screen = FormController(this)

    abstract fun build()

    /** Each time the screen comes into view, before it's built. */
    open fun appear() {}

    fun rebuild() {
        for (v in screen.stack.arrangedSubviews) (v as? UIView)?.removeFromSuperview()
        build()
    }

    /** Does `what` every so many seconds while this screen shows; 0 for no more. */
    fun every(seconds: Double, what: () -> Unit) {
        screen.timer?.invalidate()
        screen.timer = if (seconds > 0) NSTimer.scheduledTimerWithTimeInterval(seconds, repeats = true) { _ -> what() } else null
    }

    fun push(next: Form) { screen.navigationController?.pushViewController(next.screen, animated = true) }

    fun add(v: UIView): UIView { screen.stack.addArrangedSubview(v); return v }
    fun text(s: String, color: UIColor? = null, bold: Boolean = false, small: Boolean = false) = add(makeLabel(s, color, bold, if (small) 13.0 else 16.0)) as UILabel
    fun section(s: String) = add(makeLabel(s.uppercase(), UIColor.secondaryLabelColor, size = 12.0))
    fun button(title: String, look: Look = Look.TINTED, enabled: Boolean = true, action: () -> Unit) = add(makeRow(makeButton(title, look, enabled, action = action)))
    fun row(vararg views: UIView) = add(makeRow(*views))

    fun progress(value: Double): UIProgressView {
        val p = UIProgressView(progressViewStyle = UIProgressViewStyle.UIProgressViewStyleDefault)
        p.progress = value.toFloat().coerceIn(0f, 1f)
        add(p)
        return p
    }

    fun field(placeholder: String, value: String, secret: Boolean = false, onChange: (String) -> Unit): UITextField {
        val f = UITextField()
        f.placeholder = placeholder
        f.text = value
        f.borderStyle = UITextBorderStyle.UITextBorderStyleRoundedRect
        f.secureTextEntry = secret
        f.autocapitalizationType = UITextAutocapitalizationType.UITextAutocapitalizationTypeNone
        f.autocorrectionType = UITextAutocorrectionType.UITextAutocorrectionTypeNo
        f.addAction(UIAction.actionWithHandler { _ -> onChange(f.text ?: "") }, forControlEvents = UIControlEventEditingChanged)
        add(f)
        return f
    }
}
