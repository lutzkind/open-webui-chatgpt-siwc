import AppKit
import CryptoKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var label: NSTextField!
    private var continueButton: NSButton!
    private var task: Process?
    private var connectURL: URL?
    private var bundleIsValid = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            try verifyBundle()
            bundleIsValid = true
        } catch {
            if ProcessInfo.processInfo.arguments.contains("--bundle-check") { exit(1) }
        }
        if ProcessInfo.processInfo.arguments.contains("--bundle-check") { exit(bundleIsValid ? 0 : 1) }

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 245),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Connect ChatGPT"
        label = NSTextField(wrappingLabelWithString: "Start from the Connect ChatGPT page in your Open WebUI instance.")
        label.frame = NSRect(x: 28, y: 92, width: 464, height: 105)
        window.contentView!.addSubview(label)

        continueButton = NSButton(title: "Continue with ChatGPT", target: self, action: #selector(beginAuthorization))
        continueButton.frame = NSRect(x: 310, y: 32, width: 182, height: 34)
        continueButton.bezelStyle = .rounded
        continueButton.isEnabled = false
        window.contentView!.addSubview(continueButton)

        let cancelButton = NSButton(title: "Cancel", target: self, action: #selector(cancelAuthorization))
        cancelButton.frame = NSRect(x: 215, y: 32, width: 82, height: 34)
        cancelButton.bezelStyle = .rounded
        window.contentView!.addSubview(cancelButton)

        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        guard bundleIsValid, urls.count == 1, let value = validatedConnectURL(urls[0]) else {
            label?.stringValue = "The Open WebUI connection address is invalid. Return to Open WebUI and start again."
            return
        }
        connectURL = value
        label.stringValue = "Connect to \(value.host ?? "your Open WebUI instance")? Continue only if you started this from an Open WebUI server you trust. OAuth will open in your browser."
        continueButton.isEnabled = true
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func beginAuthorization() {
        guard task == nil, bundleIsValid, let resources = Bundle.main.resourceURL, let connectURL else { return }
        #if arch(arm64)
        let architecture = "arm64"
        #else
        let architecture = "x64"
        #endif

        let process = Process()
        process.executableURL = resources.appendingPathComponent("runtime/\(architecture)/bin/node")
        process.arguments = [
            resources.appendingPathComponent("tools/authorize.mjs").path,
            "--web-connect",
            "--connect-url",
            connectURL.absoluteString
        ]
        process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": NSHomeDirectory()]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                self?.label.stringValue = process.terminationStatus == 0
                    ? "Connected. Return to Open WebUI to finish checking the provider."
                    : "The connection did not finish. Return to Open WebUI and try again."
                self?.task = nil
                self?.continueButton.isEnabled = false
            }
        }
        task = process
        continueButton.isEnabled = false
        label.stringValue = "Preparing a secure connection. Keep this companion open and approve the request in your browser."
        do {
            try process.run()
        } catch {
            task = nil
            label.stringValue = "The companion could not start. Download a fresh copy and try again."
        }
    }

    @objc private func cancelAuthorization() {
        if let task {
            task.terminate()
            self.task = nil
        }
        connectURL = nil
        continueButton.isEnabled = false
        label.stringValue = "Cancelled. Return to Open WebUI when you are ready to connect."
    }

    func applicationWillTerminate(_ notification: Notification) {
        task?.terminate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        task == nil
    }

    private func validatedConnectURL(_ deepLink: URL) -> URL? {
        guard let components = URLComponents(url: deepLink, resolvingAgainstBaseURL: false),
              components.scheme == "open-webui-chatgpt-siwc",
              components.host == "connect",
              let query = components.queryItems,
              query.count == 1,
              query[0].name == "connect_url",
              let raw = query[0].value,
              let connect = URL(string: raw),
              let target = URLComponents(url: connect, resolvingAgainstBaseURL: false),
              target.path == "/siwc/connect",
              target.user == nil,
              target.password == nil,
              target.query == nil,
              target.fragment == nil else { return nil }
        let loopbackHTTP = target.scheme == "http" && ["localhost", "127.0.0.1"].contains(target.host ?? "")
        guard target.scheme == "https" || loopbackHTTP else { return nil }
        return connect
    }

    private func verifyBundle() throws {
        guard let resources = Bundle.main.resourceURL else { throw CocoaError(.fileReadCorruptFile) }
        let manifestData = try Data(contentsOf: resources.appendingPathComponent("bundle-manifest.json"))
        let pins = try JSONDecoder().decode([String: String].self, from: manifestData)
        for (name, expected) in pins {
            let parts = name.split(separator: "/")
            guard !name.hasPrefix("/"), !parts.contains("..") else {
                throw CocoaError(.fileReadCorruptFile)
            }
            let data = try Data(contentsOf: resources.appendingPathComponent(name))
            let actual = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
            guard actual == expected else { throw CocoaError(.fileReadCorruptFile) }
        }
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
