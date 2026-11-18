package sh.boulderme.ios

import kotlinx.cinterop.ExperimentalForeignApi
import kotlinx.cinterop.useContents
import platform.CoreGraphics.CGRectMake
import platform.Foundation.NSURLComponents
import platform.Foundation.NSURLQueryItem
import platform.Foundation.NSUserDefaults
import platform.UIKit.UIApplication
import platform.UIKit.UIScreen
import platform.darwin.dispatch_async
import platform.UIKit.UINavigationController
import platform.UIKit.UIViewController
import platform.darwin.DISPATCH_TIME_NOW
import platform.darwin.dispatch_after
import platform.darwin.dispatch_get_main_queue
import platform.darwin.dispatch_time

/** What the app's screens share. */
class App {
    val store = IosStore()
    val api = IosApi(store)
    val sensors = IosSensors()
    val uploader = Uploader(store, api)
    val statuses = Statuses(store, api)

    init { Net.onWifi }     // starts watching the network, so that it's known before it's asked
}

/**
 * The app's first screen, in its navigation: what the Swift side shows in its window.
 *
 * For trying it in a simulator with nobody to press anything, arguments at launch open a screen straight away
 * (app/ci/screens-ios.sh): `-screen browse|settings|calibrate|capture|viewer|upload|photo`, with `-get YES` to download
 * the first mesh listed, `-mesh <id>` for the mesh to capture (the first downloaded if not given; `none` for a rock with
 * no mesh), `-capture <id>` for the capture upload and photo show (the newest if not given; viewer: the first mesh
 * downloaded), and
 * `-server <address>`, which iOS itself reads into the setting of that name for that launch (so do `-terms 1` and
 * `-sign_in_asked NO`, which shows the first opening's prompt to sign in).
 */
fun rootViewController(): UIViewController {
    Crashes.watch()
    val app = App()
    app.sensors.start()
    val home = Home(app)
    running = app to home
    val nav = UINavigationController(rootViewController = home.screen)
    val args = NSUserDefaults.standardUserDefaults
    when (args.stringForKey("screen")) {
        "browse" -> nav.pushViewController(Browse(app, getFirst = args.boolForKey("get")).screen, animated = false)
        "settings" -> nav.pushViewController(Settings(app).screen, animated = false)
        "calibrate" -> nav.pushViewController(Calibrate(app).screen, animated = false)
        "viewer" -> app.store.downloaded().firstOrNull()?.let { nav.pushViewController(ViewerController(app, it.mesh, it.name), animated = false) }
        "upload", "photo" -> (args.stringForKey("capture") ?: app.store.captureIds().firstOrNull())?.let { id ->
            nav.pushViewController(UploadForm(app, id).screen, animated = false)
            val first = app.store.contents(id).first.firstOrNull { it.photos.isNotEmpty() }
            if (args.stringForKey("screen") == "photo" && first != null) nav.pushViewController(PhotoForm(app, id, first.photos.first(), first.scenery, first.title).screen, animated = false)
        }
        "capture" -> dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 1_000_000_000L), dispatch_get_main_queue()) {     // once the window is up
            val mesh = args.stringForKey("mesh")?.takeIf { it != "none" } ?: app.store.downloaded().firstOrNull()?.mesh?.takeIf { args.stringForKey("mesh") == null }
            home.capture(mesh, mesh?.let { app.store.plan(it)?.name } ?: "A rock")
        }
    }
    // the window in the screen's top half only, for an account whose role asks for it: as kept, then as the server says
    dispatch_async(dispatch_get_main_queue()) { topHalf(app.store.topHalf) }
    if (app.store.token.isNotEmpty()) app.api.account { topHalf(it) }
    return nav
}

/**
 * Draws the whole app, alerts and all, in the top of the screen (HALF_SHARE of it, for a phone whose screen's lower
 * part is broken), or in all of it.
 */
@OptIn(ExperimentalForeignApi::class)
fun topHalf(on: Boolean) {
    val window = UIApplication.sharedApplication.keyWindow ?: return
    val (w, h) = UIScreen.mainScreen.bounds.useContents { size.width to size.height }
    window.setFrame(CGRectMake(0.0, 0.0, w, if (on) h * sh.boulderme.core.HALF_SHARE else h))
}

private var running: Pair<App, Home>? = null

/**
 * Signs in with a code (or a link with one), crediting photos to the account's name if they're credited to nobody yet;
 * then done(what to say, whether it worked), on the main thread.
 */
fun signIn(app: App, code: String, done: (String, Boolean) -> Unit) = app.api.pair(code, app.store.credit) { token, why ->
    if (token == null) return@pair done("Signing in failed: $why", false)
    app.store.token = token
    app.store.signInAsked = true
    app.api.account { half ->
        topHalf(half)
        done(app.store.credit.takeIf { it.isNotBlank() }?.let { "Signed in: photos are credited to $it." } ?: "Signed in: this phone can upload now.", true)
    }
}

/**
 * A link the app was opened by (AppDelegate.swift, SceneDelegate): the site's "Sign the app in" on this phone (frontend/index.html,
 * appsHtml()), bouldermesh://pair?code=<a pairing code of the account's>&server=<the site>. A link for a server other
 * than the app's is refused, so a link from elsewhere can't send this phone's captures somewhere else. Answers whether
 * it was one of the app's.
 */
fun openLink(url: String): Boolean {
    val (app, home) = running ?: return false
    val link = NSURLComponents.componentsWithString(url) ?: return false
    if (link.scheme != "bouldermesh" || link.host != "pair") return false
    val query = link.queryItems?.mapNotNull { it as? NSURLQueryItem }?.associate { it.name to it.value } ?: emptyMap()
    val code = query["code"]?.takeIf { it.isNotBlank() } ?: return true
    val server = query["server"]?.trimEnd('/')
    if (server != null && server != app.store.server.trimEnd('/')) {
        home.screen.tell("Not signed in", "That link is for $server, and this app sends to ${app.store.server}: change Server in Settings first.")
        return true
    }
    signIn(app, code) { said, ok ->
        home.rebuild()
        home.screen.tell(if (ok) "Signed in" else "Not signed in", said)
    }
    return true
}
