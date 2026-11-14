import XCTest
@testable import VantageCompanionCore

final class APIModelDecodingTests: XCTestCase {

    // MARK: - Health

    func testDecodeHealthResponse() throws {
        let json = """
        {"status":"ok","tools":14,"agents":["assistant","explorer"],"llm_enabled":true,
        "llm_source":"api","ai_provider":"openai","pending_confirmations":0}
        """
        let data = json.data(using: .utf8)!
        let health = try JSONDecoder().decode(HealthResponse.self, from: data)
        XCTAssertEqual(health.status, "ok")
        XCTAssertEqual(health.tools, 14)
        XCTAssertEqual(health.llm_enabled, true)
        XCTAssertEqual(health.ai_provider, "openai")
    }

    func testDecodeHealthResponseMinimal() throws {
        let json = "{\"status\":\"ok\",\"tools\":0,\"agents\":[],\"llm_enabled\":false}"
        let health = try JSONDecoder().decode(HealthResponse.self, from: Data(json.utf8))
        XCTAssertEqual(health.status, "ok")
    }

    // MARK: - Command

    func testCommandRequestEncoding() throws {
        let cmd = CommandRequest(text: "hello", deviceId: "phone_abc123")
        let data = try JSONEncoder().encode(cmd)
        let dict = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertEqual(dict["text"] as? String, "hello")
        XCTAssertEqual(dict["device_id"] as? String, "phone_abc123")
        XCTAssertEqual(dict["source"] as? String, "mobile")
    }

    func testCommandResponseDecoding() throws {
        let json = """
        {"result":"Done","status":"completed","verification":{"status":"verified","verifications_passed":3,"verifications_failed":0}}
        """
        let resp = try JSONDecoder().decode(CommandResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.result, "Done")
        XCTAssertEqual(resp.status, "completed")
        XCTAssertEqual(resp.verification?.status, "verified")
    }

    func testCommandResponseError() throws {
        let json = "{\"error\":\"Something went wrong\"}"
        let resp = try JSONDecoder().decode(CommandResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.error, "Something went wrong")
        XCTAssertNil(resp.result)
    }

    // MARK: - Confirmation

    func testConfirmationDecoding() throws {
        let json = """
        {"id":"c1","action":"delete_file","target":"/tmp/test.txt","risk":"high","status":"pending",
        "created_at":"2025-01-01T00:00:00Z","expires_at":"2025-01-01T00:05:00Z"}
        """
        let conf = try JSONDecoder().decode(Confirmation.self, from: Data(json.utf8))
        XCTAssertEqual(conf.id, "c1")
        XCTAssertEqual(conf.action, "delete_file")
        XCTAssertEqual(conf.riskLevel, "high")
    }

    // MARK: - Device

    func testDeviceDecoding() throws {
        let json = """
        {"id":"d1","device_id":"phone_abc","device_name":"iPhone","state":"paired",
        "paired_at":"2025-01-01T00:00:00Z","last_seen":"2025-01-01T01:00:00Z",
        "surface":"phone","fingerprint":"ABCD 1234"}
        """
        let device = try JSONDecoder().decode(DeviceInfo.self, from: Data(json.utf8))
        XCTAssertEqual(device.id, "d1")
        XCTAssertEqual(device.displayName, "iPhone")
        XCTAssertTrue(device.isPaired)
        XCTAssertTrue(device.isPhone)
    }

    // MARK: - Session

    func testSessionResponseDecoding() throws {
        let json = "{\"auth_enabled\":true,\"session_token\":\"tok_abc\",\"expires_in\":3600,\"surface\":\"phone\"}"
        let resp = try JSONDecoder().decode(SessionResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.session_token, "tok_abc")
        XCTAssertEqual(resp.surface, "phone")
    }

    // MARK: - Task

    func testTaskListDecoding() throws {
        let json = """
        {"tasks":[{"id":"t1","intent":"test","status":"completed","created_at":"2025-01-01T00:00:00Z"}],"count":1}
        """
        let resp = try JSONDecoder().decode(TaskListResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.count, 1)
        XCTAssertEqual(resp.tasks?.first?.intent, "test")
    }

    // MARK: - AnyCodable

    func testAnyCodableString() throws {
        let json = "\"hello\"".data(using: .utf8)!
        let value = try JSONDecoder().decode(AnyCodable.self, from: json)
        if case .string(let s) = value { XCTAssertEqual(s, "hello") }
        else { XCTFail("Expected string") }
    }

    func testAnyCodableInt() throws {
        if case .int(let i) = try JSONDecoder().decode(AnyCodable.self, from: "42".data(using: .utf8)!) {
            XCTAssertEqual(i, 42)
        } else { XCTFail("Expected int") }
    }

    func testAnyCodableDict() throws {
        let json = "{\"key\":\"value\"}".data(using: .utf8)!
        let value = try JSONDecoder().decode(AnyCodable.self, from: json)
        if case .dictionary(let d) = value {
            if case .string(let s) = d["key"] { XCTAssertEqual(s, "value") }
            else { XCTFail("Expected string value") }
        } else { XCTFail("Expected dict") }
    }
}