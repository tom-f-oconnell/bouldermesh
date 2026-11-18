import UIKit
import BoulderMeshKit

// The whole app is in Kotlin (../../src/iosMain, built into BoulderMeshKit): this only puts its first screen in a window,
// and hands it the links it's opened by.
@main
class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = MainKt.rootViewController()
        window.makeKeyAndVisible()
        self.window = window
        return true
    }

    // the site's "Sign the app in" on this phone: bouldermesh://pair?code=…&server=… (Main.kt, openLink())
    func application(_ app: UIApplication, open url: URL, options: [UIApplication.OpenURLOptionsKey: Any] = [:]) -> Bool {
        return MainKt.openLink(url: url.absoluteString)
    }
}
