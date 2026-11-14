import Foundation

/// A minimal, provider-agnostic client for any OpenAI-compatible endpoint.
///
/// It speaks exactly two shapes, which is the whole OpenAI-compatible contract:
///   * `GET  {baseURL}/models`          -> `{ "data": [ { "id": "model" }, … ] }`
///   * `POST {baseURL}/chat/completions` -> `{ "choices": [ { "message": … } ] }`
///
/// Every failure is reported as a typed `AIClientError` -- a live request that did not
/// return a usable answer never masquerades as success.
struct AIClient {

    /// How a call can fail. Kept distinct so the UI can say *which* thing went wrong
    /// (a missing key is not a bad key is not an unreachable host).
    enum AIClientError: LocalizedError, Equatable {
        case notConfigured            // no base URL, or no key
        case invalidBaseURL(String)   // the typed base URL is not a URL
        case badStatus(Int, String)   // provider answered a non-2xx with this body
        case emptyResponse            // 2xx but no usable content
        case transport(String)        // could not reach the host at all

        var errorDescription: String? {
            switch self {
            case .notConfigured:
                return "No API key is set. Add one in AI settings."
            case .invalidBaseURL(let value):
                return "The base URL \(value.isEmpty ? "(empty)" : value) is not a valid URL."
            case .badStatus(let code, let body):
                let detail = body.trimmingCharacters(in: .whitespacesAndNewlines)
                return "Provider returned \(code)." + (detail.isEmpty ? "" : " \(String(detail.prefix(200)))")
            case .emptyResponse:
                return "The provider returned no message."
            case .transport(let message):
                return "Could not reach the provider: \(message)"
            }
        }
    }

    /// One model advertised by the provider.
    struct Model: Identifiable, Hashable {
        let id: String
    }

    /// A chat message in the OpenAI shape.
    struct ChatMessage: Codable {
        let role: String
        let content: String
    }

    let baseURL: String
    let apiKey: String
    var session: URLSession = .shared

    /// Resolve the settings into a client, or throw `.notConfigured` when a key or
    /// endpoint is missing -- so a caller never issues an unauthenticated request.
    ///
    /// `@MainActor` because `AISettingsManager` is main-actor-isolated (it drives
    /// SwiftUI); both callers (the settings view and the Siri intent) already run there.
    @MainActor
    static func from(_ settings: AISettingsManager) throws -> AIClient {
        let key = settings.apiKey()
        guard !settings.baseURL.isEmpty, let key, !key.isEmpty else {
            throw AIClientError.notConfigured
        }
        return AIClient(baseURL: settings.baseURL, apiKey: key)
    }

    private func endpoint(_ path: String) throws -> URL {
        let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !trimmed.isEmpty, let url = URL(string: trimmed + path) else {
            throw AIClientError.invalidBaseURL(baseURL)
        }
        return url
    }

    private func authorizedRequest(_ url: URL, method: String) -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        // Bounded so an unresponsive host cannot hold the UI (or a Siri intent) open.
        request.timeoutInterval = 30
        return request
    }

    // MARK: - Models

    /// `GET {baseURL}/models`, parsed per the OpenAI spec.
    func fetchModels() async throws -> [Model] {
        let url = try endpoint("/models")
        let request = authorizedRequest(url, method: "GET")

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw AIClientError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw AIClientError.transport("no HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw AIClientError.badStatus(http.statusCode, String(data: data, encoding: .utf8) ?? "")
        }

        struct ModelsResponse: Decodable {
            struct Entry: Decodable { let id: String }
            let data: [Entry]
        }
        do {
            let decoded = try JSONDecoder().decode(ModelsResponse.self, from: data)
            return decoded.data.map { Model(id: $0.id) }
        } catch {
            // A 2xx that does not match the documented shape is a real answer the
            // client cannot use -- reported, not papered over with an empty list.
            throw AIClientError.badStatus(http.statusCode, "unreadable model list")
        }
    }

    // MARK: - Chat

    /// `POST {baseURL}/chat/completions`; returns the assistant message text.
    func sendMessage(
        _ userText: String,
        model: String,
        systemPrompt: String,
        temperature: Double = 0.7
    ) async throws -> String {
        let url = try endpoint("/chat/completions")
        var request = authorizedRequest(url, method: "POST")

        var messages: [ChatMessage] = []
        if !systemPrompt.isEmpty {
            messages.append(ChatMessage(role: "system", content: systemPrompt))
        }
        messages.append(ChatMessage(role: "user", content: userText))

        let payload: [String: Any] = [
            "model": model,
            "messages": messages.map { ["role": $0.role, "content": $0.content] },
            "temperature": temperature,
            "stream": false,
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw AIClientError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw AIClientError.transport("no HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw AIClientError.badStatus(http.statusCode, String(data: data, encoding: .utf8) ?? "")
        }

        struct ChatResponse: Decodable {
            struct Choice: Decodable {
                struct Message: Decodable { let content: String }
                let message: Message
            }
            let choices: [Choice]
        }
        guard
            let decoded = try? JSONDecoder().decode(ChatResponse.self, from: data),
            let content = decoded.choices.first?.message.content,
            !content.isEmpty
        else {
            throw AIClientError.emptyResponse
        }
        return content
    }
}