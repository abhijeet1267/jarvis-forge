import Foundation

/// Holds the tail of a child process's output, read from a pipe on its queue.
///
/// A `Pipe` whose reading end is never drained fills and stalls the child; that is
/// how a refused port became an invisible failure — the backend's own message
/// ("port already in use") sat in the buffer while the UI reported a generic
/// timeout. This drains continuously and keeps the last few kilobytes, which is
/// what an error message actually needs.
///
/// Deliberately its own file: declared inside `BackendManager.swift` it inherited
/// that file's `@MainActor`, which made the type unusable from the pipe's queue and
/// from the termination handler. `nonisolated` cannot override an inferred actor on
/// a class, so the fix is to not be in that context at all.
final class OutputCapture: @unchecked Sendable {
    private let lock = NSLock()
    private var buffer = Data()
    private let limit = 8 * 1024

    func append(_ chunk: Data) {
        lock.lock(); defer { lock.unlock() }
        buffer.append(chunk)
        if buffer.count > limit { buffer.removeFirst(buffer.count - limit) }
    }

    func tail() -> String {
        lock.lock(); defer { lock.unlock() }
        return String(data: buffer, encoding: .utf8) ?? ""
    }
}