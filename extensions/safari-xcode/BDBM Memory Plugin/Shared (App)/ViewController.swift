import Foundation
import WebKit

#if os(iOS)
import UIKit
typealias PlatformViewController = UIViewController
#elseif os(macOS)
import Cocoa
import SafariServices
typealias PlatformViewController = NSViewController
#endif

nonisolated struct LocalServiceHealthResponse: Decodable {
    let product: String
    let status: String
    let protocolVersion: Int
    let version: String
    let ready: Bool
    let transport: String

    enum CodingKeys: String, CodingKey {
        case product
        case status
        case protocolVersion = "protocol_version"
        case version
        case ready
        case transport
    }

    var isValid: Bool {
        product == "biomem" &&
            status == "success" &&
            protocolVersion == 1 &&
            !version.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty &&
            ready &&
            transport == "http"
    }
}

nonisolated struct LocalServiceHealthValidator {
    static func isValid(statusCode: Int, headers: [AnyHashable: Any], data: Data) -> Bool {
        guard statusCode == 200,
              let contentType = headers.first(where: {
                  String(describing: $0.key).caseInsensitiveCompare("Content-Type") == .orderedSame
              })?.value as? String,
              let mediaType = contentType.split(separator: ";", maxSplits: 1).first,
              mediaType.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "application/json",
              let payload = try? JSONDecoder().decode(LocalServiceHealthResponse.self, from: data) else {
            return false
        }

        return payload.isValid
    }
}

final class ViewController: PlatformViewController, WKNavigationDelegate, WKScriptMessageHandler {

    @IBOutlet private var webView: WKWebView!

    private var extensionBundleIdentifier: String {
        let hostIdentifier = Bundle.main.bundleIdentifier ?? "com.bleedingdev.biomem.safari"
        return "\(hostIdentifier).Extension"
    }

    private let localServiceHealthURL = URL(string: "http://127.0.0.1:8766/api/health")!
    private var healthCheckTask: URLSessionDataTask?

    override func viewDidLoad() {
        super.viewDidLoad()

        webView.navigationDelegate = self
        webView.configuration.userContentController.add(self, name: "controller")

#if os(iOS)
        webView.scrollView.isScrollEnabled = false
#endif

        guard let pageURL = Bundle.main.url(forResource: "Main", withExtension: "html") else {
            assertionFailure("Missing bundled Safari host page")
            return
        }

        webView.loadFileURL(pageURL, allowingReadAccessTo: Bundle.main.resourceURL!)
    }

    deinit {
        healthCheckTask?.cancel()
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "controller")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
#if os(iOS)
        webView.evaluateJavaScript("show('ios')")
#elseif os(macOS)
        refreshStatus()
#endif
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let command = message.body as? String else {
            return
        }

#if os(macOS)
        switch command {
        case "open-preferences":
            openSafariExtensionSettings()
        case "refresh-status":
            refreshStatus()
        default:
            break
        }
#endif
    }

#if os(macOS)
    private func refreshStatus() {
        webView.evaluateJavaScript("show('mac')")
        refreshExtensionStatus()
        refreshLocalServiceStatus()
    }

    private func refreshExtensionStatus() {
        SFSafariExtensionManager.getStateOfSafariExtension(withIdentifier: extensionBundleIdentifier) { [weak self] state, error in
            DispatchQueue.main.async {
                guard let self else {
                    return
                }

                guard error == nil, let state else {
                    self.webView.evaluateJavaScript("setExtensionState('unavailable')")
                    return
                }

                let extensionState = state.isEnabled ? "enabled" : "disabled"
                self.webView.evaluateJavaScript("setExtensionState('\(extensionState)')")
            }
        }
    }

    private func refreshLocalServiceStatus() {
        healthCheckTask?.cancel()
        webView.evaluateJavaScript("setLocalServiceState('checking')")

        var request = URLRequest(url: localServiceHealthURL)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 2

        healthCheckTask = URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            let isHealthy: Bool

            if error == nil,
               let response = response as? HTTPURLResponse,
               let data {
                isHealthy = LocalServiceHealthValidator.isValid(
                    statusCode: response.statusCode,
                    headers: response.allHeaderFields,
                    data: data
                )
            } else {
                isHealthy = false
            }

            DispatchQueue.main.async {
                guard let self else {
                    return
                }
                let serviceState = isHealthy ? "running" : "offline"
                self.webView.evaluateJavaScript("setLocalServiceState('\(serviceState)')")
            }
        }
        healthCheckTask?.resume()
    }

    private func openSafariExtensionSettings() {
        SFSafariApplication.showPreferencesForExtension(withIdentifier: extensionBundleIdentifier) { error in
            guard error == nil else {
                DispatchQueue.main.async {
                    self.webView.evaluateJavaScript("setExtensionState('unavailable')")
                }
                return
            }

            DispatchQueue.main.async {
                NSApp.terminate(self)
            }
        }
    }
#endif
}
