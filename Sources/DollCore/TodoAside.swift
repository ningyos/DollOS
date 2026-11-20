import Foundation

/// Waiting is not busy (2026-11-20, owner; soak run: from 07:01 a list waited for the owner to wake,
/// and for nine hours, 動力 at 0.97, she never did a thing of her own). While a list is 等待中 she
/// counts as idle (`Doll.idleForWander`): 動力's wandering comes, and a `todo.set` with a sub-goal
/// starts her own list and sets the waiting one aside. At most two lists: the one in hand and one
/// aside; a list is set aside only from 等待中 with none aside, so a third never starts.
///
///     her own list ends (last todo.done, or todo.fail then an empty todo.set)  → the waiting list is back in hand
///     a person's line or a watch wake while the waiting list is aside            → the waiting list is in hand for
///                                                     that turn (the owner comes first: what she does about his
///                                                     answer goes to its item); she takes it up → her own list
///                                                     waits aside until it ends; she does not → her own list
///                                                     is back in hand after the turn and the waiting one waits on
///
/// Each list keeps its own rounds, decision point, plan and estimate record, and its goal-tree link.
/// The timeline stores the aside list with every todo record, so a restart keeps both.
public struct ListAside: Sendable, Equatable, Codable {
    public var todo: TodoState
    /// Its goal-tree link (`GoalFile.link` while it is in hand).
    public var link: TodoLink?
}

/// What the program keeps in memory for a list while it runs (`Doll.itemSteps` …): it goes aside with
/// the list and comes back with it. A new process starts it empty, as for the list in hand.
struct ListMemory {
    var itemSteps: [(seq: Int64, line: String)] = []
    var itemApps: [AppIdentity] = []
    var itemChain: [ChainPart] = []
    var itemUsedComputer = false
    var itemActed = false
    var listFor: (to: Addressee, channel: Channel)?
    var listReply: Int64 = 0
    var listReported = false
    var latestRead: Doll.LatestRead?
}

extension Doll {
    var listMemory: ListMemory {
        get {
            ListMemory(itemSteps: itemSteps, itemApps: itemApps, itemChain: itemChain, itemUsedComputer: itemUsedComputer,
                       itemActed: itemActed, listFor: listFor, listReply: listReply, listReported: listReported, latestRead: latestRead)
        }
        set {
            itemSteps = newValue.itemSteps
            itemApps = newValue.itemApps
            itemChain = newValue.itemChain
            itemUsedComputer = newValue.itemUsedComputer
            itemActed = newValue.itemActed
            listFor = newValue.listFor
            listReply = newValue.listReply
            listReported = newValue.listReported
            latestRead = newValue.latestRead
        }
    }

    /// The list in hand becomes `list` (with what was in memory for it); returns the one it replaced.
    private func putInHand(_ list: ListAside, _ memory: ListMemory) -> (ListAside, ListMemory) {
        let was = (ListAside(todo: todo, link: goalFile.link), listMemory)
        todo = list.todo
        listMemory = memory
        warnedTurns = 0
        forgetSeen()
        if drivesConfig.enabled {
            goalFile.link = list.link
            do { try saveGoals() } catch { log("ERROR", "goals: saving \(goalsURL.path) failed: \(error)") }
        }
        return was
    }

    /// The waiting list goes aside; nothing is in hand (`todoSet` starts her own list next).
    func setAside() {
        let (was, memory) = putInHand(ListAside(todo: TodoState(), link: nil), ListMemory())
        aside = was
        asideMemory = memory
        log("INFO", "todo: 「\(was.todo.current ?? "")」等待中 set aside for a list of her own")
    }

    /// The list in hand and the one aside change places.
    func swapAside() {
        guard let other = aside else { return }
        let (was, memory) = putInHand(other, asideMemory)
        aside = was
        asideMemory = memory
    }

    /// The list in hand ended: the one aside is back in hand.
    func takeBackAside() {
        guard let other = aside else { return }
        _ = putInHand(other, asideMemory)
        aside = nil
        asideMemory = ListMemory()
        log("INFO", "turn \(turnIndex): 「\(todo.current ?? "")」back in hand (\(todo.phase.label))")
    }

    /// Her call works on the waiting item: it is 進行中 again, and this reply is one of its rounds.
    func resumeWaiting(_ tool: String) {
        guard todo.phase == .waiting else { return }
        todo.phase = .doing
        todo.rounds += 1
        if todo.decideAt == 0 { todo.decideAt = computerConfig.decideAfterRounds }
        swappedForLine = false
        log("INFO", "turn \(turnIndex): todo 等待中 → 進行中「\(todo.current ?? "")」(\(tool))")
    }

    /// 【狀況】's words for the list aside; empty when there is none.
    func asideNote() -> String {
        guard let a = aside else { return "" }
        let item = "「\(a.todo.current ?? "")」"
        if todo.phase == .waiting, a.todo.phase != .waiting {
            return "；你自己的事\(item)先放在旁邊：這則回覆沒接著做「\(todo.current ?? "")」，它就繼續等，\(item)回到你手上"
        }
        return "；旁邊放著\(a.todo.phase.label)的\(item)：手上這份做完或結束，它就回到你手上"
            + (a.todo.phase == .waiting ? "；有人說話，程式會先把它拿回你手上" : "")
    }

    /// What her result says when the list in hand ends with one aside; empty when there is none.
    func backNote() -> String {
        guard let a = aside else { return "" }
        return "放在旁邊的「\(a.todo.current ?? "")」回到你手上（\(a.todo.phase.label)）。"
    }
}
