import UIKit
import BoulderMeshKit

// The whole app is in Kotlin (../../src/iosMain, built into BoulderMeshKit): this only puts its first screen in a window.
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
}
