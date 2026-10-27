import SwiftUI

@main
struct bciApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    var body: some Scene {
        WindowGroup {
            ContentView()
                .onOpenURL { url in
                    guard url.scheme == "synaptimesh", url.host == "return" else { return }
                    print("[URL] Returned from Shortcut: \(url.absoluteString)")
                }
        }
    }
}