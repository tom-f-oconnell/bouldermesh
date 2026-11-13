package sh.boulderme.ios

import platform.UIKit.UINavigationController
import platform.UIKit.UIViewController

/** What the app's screens share. */
class App {
    val store = IosStore()
    val api = IosApi(store)
    val sensors = IosSensors()
    val uploader = Uploader(store, api)
}

/** The app's first screen, in its navigation: what the Swift side shows in its window. */
fun rootViewController(): UIViewController {
    val app = App()
    app.sensors.start()
    return UINavigationController(rootViewController = Home(app).screen)
}
