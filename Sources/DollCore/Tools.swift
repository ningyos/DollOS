/// Tool schemas. Which side a tool is on is fixed here, never decided at run time.
///
/// Internal: reads/writes only DollOS's own data; runs inside the turn.
/// External: touches anything outside DollOS; always becomes a job, and the turn ends.
enum Tools {
    private static func fn(_ name: String, _ desc: String, _ props: [String: JSONValue],
                           _ required: [String]) -> JSONValue {
        .object([
            "type": .string("function"),
            "function": .object([
                "name": .string(name),
                "description": .string(desc),
                "parameters": .object([
                    "type": .string("object"),
                    "properties": .object(props),
                    "required": .array(required.map(JSONValue.string)),
                ]),
            ]),
        ])
    }

    private static let s: JSONValue = .object(["type": .string("string")])

    static let internalTools: [(String, JSONValue)] = [
        ("Recall", fn("Recall", "Search your memory notes. Use keywords of 3+ characters. Returns up to 5 notes.",
                      ["query": s], ["query"])),
        ("NoteMemory", fn("NoteMemory", "Save one durable fact worth remembering long-term. Not for small talk.",
                          ["text": s], ["text"])),
        ("PinSelf", fn("PinSelf", "Record a correction or standing request from your owner (how to address them, "
                           + "what they like, how you should behave). Pinned items are always in front of you.",
                       ["text": s], ["text"])),
        ("OpenLine", fn("OpenLine", "Open a work line: something you will keep pushing over time (a project, a paper, "
                            + "a search). Fails when too many lines are active.",
                        ["title": s, "next_step": s], ["title", "next_step"])),
        ("UpdateLine", fn("UpdateLine", "Update a work line after you moved it forward. status: active, paused or done.",
                          ["line_id": s,
                           "status": .object(["type": .string("string"),
                                              "enum": .array(["active", "paused", "done"].map(JSONValue.string))]),
                           "next_step": s, "summary": s],
                          ["line_id", "status", "next_step", "summary"])),
        ("CancelJob", fn("CancelJob", "Cancel one of your running jobs.", ["job_id": s], ["job_id"])),
    ]

    static let externalTools: [(String, JSONValue)] = [
        ("Shell", fn("Shell", "Run a shell command on the owner's Mac (cwd: your workspace). It runs as a "
                         + "background job: this turn ends right after you call it, and the output arrives later as a "
                         + "new event. Put line_id when the command belongs to a work line.",
                     ["command": s, "line_id": s], ["command"])),
    ]

    static let internalNames = Set(internalTools.map(\.0))
    static let externalNames = Set(externalTools.map(\.0))
    static let ownerTools = (internalTools + externalTools).sorted { $0.0 < $1.0 }.map(\.1)
}
