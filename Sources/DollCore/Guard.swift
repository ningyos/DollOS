import Foundation

/// The owner's secrets kept from everyone else (2026-11-23, owner: 「secret 會進 context，我想測 doll
/// 保守秘密的能力」, 「我想加一道程式化的 filtering」, A then B).
///
/// Who sees them: by default the program hides them from the developer's turns (`[secrets]
/// hide_from_developer`, 2026-11-21); a test doll turns that off, and then keeping them is hers.
///
/// The filter (`[secrets] filter`): before anything she says goes to someone other than the owner
/// (`dollctl.reply` today), it is checked against his secrets — the secret lines of the day's
/// timeline and her `secret` memories:
///   A exact    a piece of `filter_chars` characters of a secret (letters, digits, CJK; spaces and
///              punctuation ignored, letters lowercased) that appears in no ordinary text (the day's
///              events that are not secret, her public and own memories, her system prompt);
///   B similar  a sentence of hers whose cosine to a secret is at least `filter_similarity`.
/// A hit: nothing is sent, she is told why; the log line is sealed.
extension Doll {
    var hideSecretsFromDeveloper: Bool { secretsConfig.hideFromDeveloper }

    /// The memory folders a turn with these events may read.
    func visibleFolders(_ events: [Event]) -> Set<MemoryFolder> {
        let audience = Self.audience(events)
        let sawSecret = events.contains(where: \.secret)
        if audience == .developer, !hideSecretsFromDeveloper { return Memory.visible(.owner, sawSecret: true) }
        return Memory.visible(audience, sawSecret: sawSecret)
    }

    /// Letters, digits and CJK only, letters lowercased: what the exact check compares.
    static func guardKey(_ s: String) -> [Character] {
        s.lowercased().filter { $0.isLetter || $0.isNumber }.map { $0 }
    }

    static func pieces(_ s: String, _ n: Int) -> Set<String> {
        let k = guardKey(s)
        guard k.count >= n else { return k.isEmpty ? [] : [String(k)] }
        return Set((0...(k.count - n)).map { String(k[$0..<($0 + n)]) })
    }

    /// The owner's secrets as text: the day's secret lines (his, not her replies) and her secret memories.
    func secretTexts() throws -> [String] {
        let lines = timeline.events.filter { $0.secret && $0.kind == .owner }.map(\.text)
        let memories = try memory.list(.secret).filter { $0.file.retired == nil }.map(\.file.body)
        return (lines + memories).map { $0.replacingOccurrences(of: "🔒", with: "").trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    /// Why `text` may not go to `to`, or nil. Only for someone other than the owner.
    func secretLeak(_ text: String, to: Addressee) throws -> String? {
        guard secretsConfig.filter, to != .owner else { return nil }
        let secrets = try secretTexts()
        guard !secrets.isEmpty else { return nil }
        let n = secretsConfig.filterChars
        // A: a distinctive piece. Ordinary text: everything she or the day holds that is not secret.
        var ordinary = Set<String>()
        for t in timeline.events.filter({ !$0.secret && !$0.sealed }).map(\.text)
            + (try memory.list(.public) + memory.list(.own)).map(\.file.body) + [systemPrompt()] {
            ordinary.formUnion(Self.pieces(t, n))
        }
        let said = Self.pieces(text, n)
        for s in secrets {
            if let hit = Self.pieces(s, n).subtracting(ordinary).intersection(said).sorted().first {
                return "exact「\(hit)」"
            }
        }
        // B: a sentence close in meaning.
        let sentences = text.split(whereSeparator: { "。！？!?\n".contains($0) }).map(String.init)
            .filter { Self.guardKey($0).count >= n }
        guard !sentences.isEmpty else { return nil }
        let sv = try embedder.embed(sentences, as: .query), kv = try embedder.embed(secrets, as: .document)
        var best = (0.0, "")
        for (i, a) in sv.enumerated() {
            for b in kv {
                let c = zip(a, b).reduce(0.0) { $0 + Double($1.0) * Double($1.1) }
                if c > best.0 { best = (c, sentences[i]) }
            }
        }
        return best.0 >= secretsConfig.filterSimilarity ? String(format: "similar %.2f「%@」", best.0, best.1) : nil
    }
}
