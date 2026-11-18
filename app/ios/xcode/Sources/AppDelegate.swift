import UIKit
import BoulderMeshKit

// The whole app is in Kotlin (../../src/iosMain, built into BoulderMeshKit): this only puts its first screen in a window,
// and hands it the links it's opened by. The window is a scene's (SceneDelegate, below; Info.plist's
// UIApplicationSceneManifest): built with the iOS 27 SDK, an app without scenes dies at launch on iOS 27.
@main
class AppDelegate: UIResponder, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        return true
    }
}

class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let scene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: scene)
        window.rootViewController = MainKt.rootViewController()
        window.makeKeyAndVisible()
        self.window = window
        // opened by a link while not running: once the window is up, as its alerts need it
        if let url = connectionOptions.urlContexts.first?.url {
            DispatchQueue.main.async { _ = MainKt.openLink(url: url.absoluteString) }
        }
    }

    // the site's "Sign the app in" on this phone: bouldermesh://pair?code=…&server=… (Main.kt, openLink())
    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        for context in URLContexts { _ = MainKt.openLink(url: context.url.absoluteString) }
    }
}
