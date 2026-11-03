import Darwin
import Foundation
import Synchronization

/// Shell jobs run as `/bin/sh -c <command>` started with posix_spawn and
/// POSIX_SPAWN_SETSID: the child leads a new session, so its pid is also its
/// process group id, and `killpg(pid, SIGKILL)` stops it and everything it started.
/// stdin is /dev/null; stdout and stderr share one pipe.
enum Shell {
    struct Result: Sendable {
        let output: Data
        let exitCode: Int32       // negative: killed by that signal
        let timedOut: Bool
    }

    static func spawn(_ command: String, cwd: String) throws -> (pid: pid_t, fd: Int32) {
        var fds: [Int32] = [0, 0]
        guard pipe(&fds) == 0 else { throw ShellError("pipe: \(String(cString: strerror(errno)))") }
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_adddup2(&actions, fds[1], 1)
        posix_spawn_file_actions_adddup2(&actions, fds[1], 2)
        posix_spawn_file_actions_addchdir(&actions, cwd)
        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        defer { posix_spawnattr_destroy(&attr) }
        // CLOEXEC_DEFAULT: the child inherits only 0, 1 and 2, not the database or other jobs' pipes.
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETSID | POSIX_SPAWN_CLOEXEC_DEFAULT))
        let argv = ["/bin/sh", "-c", command].map { strdup($0) } + [nil]
        defer { argv.forEach { free($0) } }
        var pid: pid_t = 0
        let rc = posix_spawn(&pid, "/bin/sh", &actions, &attr, argv, environ)
        close(fds[1])
        guard rc == 0 else {
            close(fds[0])
            throw ShellError("posix_spawn: \(String(cString: strerror(rc)))")
        }
        return (pid, fds[0])
    }

    static func killGroup(_ pgid: pid_t) {
        _ = killpg(pgid, SIGKILL)   // ESRCH (already gone) is fine
    }

    /// Reads the pipe to EOF and reaps the child on a blocking thread, off the cooperative pool.
    /// After `timeout` seconds the whole group is killed and the output is dropped.
    static func collect(pid: pid_t, fd: Int32, timeout: Double) async -> Result {
        let finished = Finished()
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
            finished.state.withLock { s in
                if !s.done { s.timedOut = true; killGroup(pid) }
            }
        }
        return await withCheckedContinuation { cont in
            DispatchQueue.global().async {
                var out = Data()
                var buf = [UInt8](repeating: 0, count: 65536)
                while true {
                    let n = read(fd, &buf, buf.count)
                    if n > 0 { out.append(buf, count: n) } else if n < 0 && errno == EINTR { continue } else { break }
                }
                close(fd)
                var status: Int32 = 0
                while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
                let timedOut = finished.state.withLock { s in s.done = true; return s.timedOut }
                let exitCode = status & 0x7f == 0 ? (status >> 8) & 0xff : -(status & 0x7f)
                cont.resume(returning: Result(output: timedOut ? Data() : out, exitCode: exitCode, timedOut: timedOut))
            }
        }
    }

    private final class Finished: Sendable {
        struct State { var done = false; var timedOut = false }
        let state = Mutex(State())
    }
}

struct ShellError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}
