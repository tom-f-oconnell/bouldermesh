package sh.boulderme.ios

import platform.Foundation.NSUserDefaults
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
}

/**
 * The app's first screen, in its navigation: what the Swift side shows in its window.
 *
 * For trying it in a simulator with nobody to press anything, arguments at launch open a screen straight away
 * (app/ci/screens-ios.sh): `-screen browse|settings|calibrate|capture|viewer|upload|photo`, with `-get YES` to download
 * the first mesh listed, `-mesh <id>` for the mesh to capture (the first downloaded if not given; `none` for a rock with
 * no mesh), `-capture <id>` for the capture upload and photo show (the newest if not given; viewer: the first mesh
 * downloaded), and
 * `-server <address>`, which iOS itself reads into the setting of that name for that launch.
 */
fun rootViewController(): UIViewController {
    Crashes.watch()
    val app = App()
    app.sensors.start()
    val home = Home(app)
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
    return nav
}
