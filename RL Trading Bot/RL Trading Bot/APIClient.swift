//
//  APIClient.swift — async HTTP client for the TradeCommand server,
//  plus the Keychain-backed Face ID / Touch ID quick-arm PIN store.
//

import Foundation
import LocalAuthentication
import Security

enum APIError: LocalizedError {
    case badURL
    case http(Int, String)
    case needsArm
    case server(String)
    case network(String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .badURL: return "Server URL is not set — open Settings."
        case .http(let code, let msg): return "HTTP \(code): \(msg)"
        case .needsArm: return "Not armed — enter your PIN."
        case .server(let m): return m
        case .network(let m): return m
        case .decoding(let m): return "Response decoding failed: \(m)"
        }
    }
}

final class APIClient {
    var baseURL: URL?
    var armToken: String?

    private let session: URLSession
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    init() {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 30
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.waitsForConnectivity = false
        session = URLSession(configuration: cfg)
    }

    func get<T: Decodable>(_ path: String, as type: T.Type = T.self) async throws -> T {
        try await request(path, method: "GET", body: nil)
    }

    @discardableResult
    func post<T: Decodable>(_ path: String, body: [String: Any?] = [:],
                            as type: T.Type = T.self) async throws -> T {
        let clean = body.compactMapValues { $0 }
        let data = try JSONSerialization.data(withJSONObject: clean)
        return try await request(path, method: "POST", body: data)
    }

    private func request<T: Decodable>(_ path: String, method: String,
                                       body: Data?) async throws -> T {
        guard let base = baseURL,
              let url = URL(string: path, relativeTo: base) else { throw APIError.badURL }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.httpBody = body
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let armToken { req.setValue(armToken, forHTTPHeaderField: "X-Arm-Token") }
        let (data, resp): (Data, URLResponse)
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw APIError.network(error.localizedDescription)
        }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401,
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           obj["arm_required"] as? Bool == true {
            throw APIError.needsArm
        }
        guard (200...299).contains(code) else {
            let msg = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])
                .flatMap { $0["error"] as? String } ?? String(data: data, encoding: .utf8) ?? ""
            throw APIError.http(code, String(msg.prefix(300)))
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.decoding("\(T.self): \(error)")
        }
    }
}

// MARK: - Keychain PIN store (Face ID / Touch ID quick-arm)

enum PINStore {
    private static let service = "com.sankarshan.rltradingbot.pin"

    static var isEnabled: Bool {
        var query = baseQuery
        query[kSecReturnData as String] = false
        // don't trigger biometry just to check existence
        query[kSecUseAuthenticationContext as String] = {
            let ctx = LAContext()
            ctx.interactionNotAllowed = true
            return ctx
        }()
        let status = SecItemCopyMatching(query as CFDictionary, nil)
        return status == errSecSuccess || status == errSecInteractionNotAllowed
    }

    static func save(pin: String) throws {
        delete()
        guard let data = pin.data(using: .utf8) else { return }
        var query = baseQuery
        guard let access = SecAccessControlCreateWithFlags(
            nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            .userPresence, nil) else {
            throw APIError.server("could not create keychain access control")
        }
        query[kSecAttrAccessControl as String] = access
        query[kSecValueData as String] = data
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw APIError.server("keychain save failed (\(status))")
        }
    }

    /// Prompts Face ID / Touch ID (or device password) and returns the PIN.
    static func retrieve(reason: String) async throws -> String {
        let ctx = LAContext()
        ctx.localizedReason = reason
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecUseAuthenticationContext as String] = ctx
        return try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global().async {
                var item: CFTypeRef?
                let status = SecItemCopyMatching(query as CFDictionary, &item)
                if status == errSecSuccess, let data = item as? Data,
                   let pin = String(data: data, encoding: .utf8) {
                    cont.resume(returning: pin)
                } else if status == errSecUserCanceled {
                    cont.resume(throwing: APIError.server("cancelled"))
                } else {
                    cont.resume(throwing: APIError.server("quick-arm unavailable (\(status)) — enter PIN manually"))
                }
            }
        }
    }

    static func delete() {
        SecItemDelete(baseQuery as CFDictionary)
    }

    private static var baseQuery: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: "trade-pin"]
    }
}
