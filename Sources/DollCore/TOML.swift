import Foundation

/// A strict parser for the small TOML subset DollOS files use:
/// `[section]` headers, `key = value` lines, `#` comments, and values that are
/// basic strings (`"..."`), multi-line basic strings (`"""..."""`), integers,
/// floats, booleans, or single-line arrays of those. Everything else is an error.
public enum TOMLValue: Sendable, Equatable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case array([TOMLValue])
}

public struct TOMLError: Error, CustomStringConvertible {
    public let description: String
}

public enum TOML {
    public static func parse(_ text: String) throws -> [String: [String: TOMLValue]] {
        var p = Parser(chars: Array(text.replacingOccurrences(of: "\r\n", with: "\n")))
        return try p.document()
    }
}

private struct Parser {
    let chars: [Character]
    var i = 0
    var line = 1

    func fail(_ msg: String) -> TOMLError { TOMLError(description: "line \(line): \(msg)") }
    var peek: Character? { i < chars.count ? chars[i] : nil }
    func starts(_ s: String) -> Bool { chars[i...].starts(with: s) }
    mutating func advance() {
        if chars[i] == "\n" { line += 1 }
        i += 1
    }
    mutating func skipSpaces() { while peek == " " || peek == "\t" { advance() } }

    /// After a header or value: spaces, an optional comment, then end of line or file.
    mutating func endOfLine() throws {
        skipSpaces()
        if peek == "#" { while let c = peek, c != "\n" { advance() } }
        guard peek == nil || peek == "\n" else { throw fail("unexpected \(peek!)") }
        if peek == "\n" { advance() }
    }

    mutating func bareName() throws -> String {
        var s = ""
        while let c = peek, c.isASCII, c.isLetter || c.isNumber || c == "_" || c == "-" { s.append(c); advance() }
        guard !s.isEmpty else { throw fail("expected a bare name") }
        return s
    }

    mutating func document() throws -> [String: [String: TOMLValue]] {
        var out: [String: [String: TOMLValue]] = [:]
        var section: String?
        while peek != nil {
            skipSpaces()
            if peek == "\n" || peek == "#" { try endOfLine(); continue }
            if peek == "[" {
                advance()
                let name = try bareName()
                guard peek == "]" else { throw fail("only plain [section] headers are supported") }
                advance()
                guard out[name] == nil else { throw fail("duplicate section [\(name)]") }
                out[name] = [:]
                section = name
                try endOfLine()
                continue
            }
            let key = try bareName()
            guard let section else { throw fail("key \(key) is outside a section") }
            skipSpaces()
            guard peek == "=" else { throw fail("expected = after \(key)") }
            advance()
            skipSpaces()
            guard out[section]![key] == nil else { throw fail("duplicate key \(key)") }
            out[section]![key] = try value(allowArray: true)
            try endOfLine()
        }
        return out
    }

    mutating func value(allowArray: Bool) throws -> TOMLValue {
        if starts("\"\"\"") { return .string(try multiline()) }
        if peek == "\"" { return .string(try basic()) }
        if allowArray, peek == "[" {
            advance()
            var items: [TOMLValue] = []
            skipSpaces()
            while peek != "]" {
                items.append(try value(allowArray: false))
                skipSpaces()
                if peek == "," { advance(); skipSpaces() } else if peek != "]" { throw fail("expected , or ]") }
            }
            advance()
            return .array(items)
        }
        var word = ""
        while let c = peek, c.isASCII, c.isLetter || c.isNumber || "+-._".contains(c) { word.append(c); advance() }
        if word == "true" { return .bool(true) }
        if word == "false" { return .bool(false) }
        if let n = Int(word) { return .int(n) }
        if word.contains("."), let d = Double(word) { return .double(d) }
        throw fail("unsupported value \(word.isEmpty ? String(peek ?? " ") : word)")
    }

    mutating func escape() throws -> Character {
        advance()
        guard let c = peek else { throw fail("unterminated escape") }
        advance()
        switch c {
        case "\"": return "\""
        case "\\": return "\\"
        case "n": return "\n"
        case "t": return "\t"
        default: throw fail("unsupported escape \\\(c)")
        }
    }

    mutating func basic() throws -> String {
        advance()
        var s = ""
        while true {
            guard let c = peek, c != "\n" else { throw fail("unterminated string") }
            if c == "\"" { advance(); return s }
            if c == "\\" { s.append(try escape()) } else { s.append(c); advance() }
        }
    }

    mutating func multiline() throws -> String {
        i += 3
        if peek == "\n" { advance() }  // a newline right after the opening quotes is trimmed
        var s = ""
        while true {
            guard let c = peek else { throw fail("unterminated multi-line string") }
            if starts("\"\"\"") { i += 3; return s }
            if c == "\\" { s.append(try escape()) } else { s.append(c); advance() }
        }
    }
}
