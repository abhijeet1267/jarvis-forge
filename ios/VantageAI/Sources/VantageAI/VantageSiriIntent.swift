import AppIntents
import Foundation
import SwiftUI

/// The opinionated, user-facing intent Siri and Shortcuts can run.
///
/// "Hey Siri, ask Vantage …" -> routes the spoken/typed query through the configured
/// OpenAI-compatible provider and answers with a dialog (spoken) and a snippet (visual).
///
/// It runs `openAppWhenRun = false`: the whole point of a Siri intent is that the user
/// does not have to open an app, so it must do its work headless and speak the result.
@available(iOS 16.0, macOS 13.0, *)
struct AskVantageAIIntent: AppIntent {

    static var title: LocalizedStringResource = "Ask Vantage"
    static var description = IntentDescription(
        "Ask the Vantage assistant a question using your configured AI provider."
    )

    /// Fully headless: the answer is returned as dialog, not a launched window.
    static var openAppWhenRun: Bool = false

    @Parameter(title: "Question", requestValueDialog: "What should I ask Vantage?")
    var question: String

    static var parameterSummary: some ParameterSummary {
        Summary("Ask Vantage \(\.$question)")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog & ShowsSnippetView {
        let settings = await AISettingsManager.shared

        // Configuration is checked here, before any request, so the user gets an
        // actionable message rather than a silent failure inside the intent runner.
        let client: AIClient
        do {
            client = try await AIClient.from(settings)
        } catch {
            let message = (error as? LocalizedError)?.errorDescription
                ?? "Vantage is not configured with an AI provider."
            return .result(dialog: "\(message)")
        }

        let model = await settings.selectedModel
        guard !model.isEmpty else {
            return .result(dialog: "No model is selected. Open Vantage AI settings and pick one.")
        }

        do {
            let reply = try await client.sendMessage(
                question,
                model: model,
                systemPrompt: await settings.systemPrompt,
                temperature: await settings.temperature
            )
            return .result(
                dialog: IntentDialog(stringLiteral: reply),
                view: VantageAnswerSnippet(question: question, answer: reply)
            )
        } catch {
            // A failed call is reported as a failure, never as an invented answer.
            let message = (error as? LocalizedError)?.errorDescription
                ?? "Vantage could not reach the provider."
            return .result(dialog: "\(message)")
        }
    }
}

/// The visual result Siri shows for an answer: the question and the assistant's reply.
@available(iOS 16.0, macOS 13.0, *)
struct VantageAnswerSnippet: View {
    let question: String
    let answer: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(question)
                .font(.footnote)
                .foregroundStyle(.secondary)
            Text(answer)
                .font(.body)
        }
        .padding()
    }
}

/// Phrases Siri matches so the intent fires naturally ("ask Vantage …", "tell Vantage …").
@available(iOS 16.0, macOS 13.0, *)
struct VantageAppShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: AskVantageAIIntent(),
            phrases: [
                "Ask \(.applicationName) a question",
                "Ask \(.applicationName) \(\.$question)",
                "Tell \(.applicationName) \(\.$question)",
            ],
            shortTitle: "Ask Vantage",
            systemImageName: "sparkles"
        )
    }
}