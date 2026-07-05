# P1g — Serviceization(systemd + dollosctl 一鍵起停)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 讓整套 DollOS(daemon + discord-bridge 兩程序)以 systemd user services 常駐,並提供 `dollosctl` 一鍵 install/start/stop/restart/status/logs —— 這是 dogfood 上線的最後一塊(goal「P1 文字存在感+語料底盤+服務化上線開始 dogfood」的「服務化上線」)。

**Architecture:** 兩個 systemd **user** service(`dollos-daemon.service` 跑 daemon WS server;`dollos-bridge.service` 跑 discord-bridge,`Wants=`+`After=` daemon,bridge 本身已自動重連故 ordering 是軟依賴、非硬阻塞)。新增 `dollosctl` console script(`src/dollos/ctl/`)—— thin CLI 包 `systemctl --user` + `journalctl --user`:`install` 依當前環境(venv python 絕對路徑、working dir、config 路徑)生成 unit 檔寫進 `~/.config/systemd/user/` 並 `daemon-reload`;`start/stop/restart` 一次起停兩個 unit;`status` 顯示兩者狀態;`logs [daemon|bridge] [-f]` 包 journalctl;`uninstall` 停止+移除。單元測試覆蓋 unit-file 生成內容 + subprocess 命令構造(mock);真 systemd 起停 = live-smoke(使用者機器,§smoke)。

**Tech Stack:** Python 3.13、`argparse`、`subprocess`(mockable)、systemd user units、`pathlib`。無新第三方依賴。

## Global Constraints

- **systemd --user,非 system**:Doll 活在使用者電腦、per-user、無需 root。unit 進 `~/.config/systemd/user/`,以 `systemctl --user` 管理。**不**碰 `/etc/systemd/system`、不需 sudo。
- **No fallback**:`dollosctl` 對 systemctl/journalctl 失敗要明確報錯(exit code + stderr 透傳),不靜默吞、不假裝成功。
- **install 時解析絕對路徑**:unit 的 `ExecStart` 用**當前 venv 的 python 絕對路徑**(`sys.executable`)+ **絕對 working dir** + **絕對 config 路徑**,不靠 PATH / 相對路徑(systemd 環境無 shell PATH 慣例)。
- **daemon 先於 bridge(軟)**:bridge `Wants=dollos-daemon.service` + `After=dollos-daemon.service`;bridge 已有重連,故 daemon 慢起不致命,但 ordering 讓正常路徑乾淨。**不**用 `Requires=`(硬綁會讓 daemon 掛掉時 bridge 也被拖死,反而更差 —— bridge 重連能撐過 daemon 重啟)。
- **config 路徑不寫死**:`dollosctl install --daemon-config <path> --bridge-config <path>` 參數化;預設指向 repo root 的 `config.toml` / bridge config。token 等敏感值留在 on-device config、**不進 git、不進 unit 檔**(unit 只引用 config 路徑)。
- **冪等 install**:重跑 install 覆寫 unit 檔 + daemon-reload,不重複堆疊。
- **Restart 策略**:unit 設 `Restart=on-failure` + `RestartSec`,讓崩潰自動拉起(bridge 崩潰不傷 daemon,systemd 各自重啟 —— 對齊 spec §3.2「bridge 崩潰不傷 daemon,systemd 自動拉起」)。

## 範圍界定

**本 plan 只加一個概念:服務化(systemd user units + dollosctl)。**

**不含**:實際 dogfood 啟動(需使用者真 bot token + 私人伺服器 + 執行 —— 本質使用者動作);跨平台 service(Win/Mac 的 launchd/service — Linux systemd 先,其餘 §7 deferred);語音(P3)。

---

## File Structure

- **Create** `src/dollos/ctl/__init__.py`、`src/dollos/ctl/cli.py`(argparse + dispatch)、`src/dollos/ctl/units.py`(unit-file 生成)、`src/dollos/ctl/systemctl.py`(subprocess wrappers,mockable)。
- **Create** `tests/test_ctl_units.py`、`tests/test_ctl_cli.py`。
- **Modify** `pyproject.toml` — `[project.scripts]` 加 `dollosctl = "dollos.ctl.cli:main"`。
- **Create** `docs/dollosctl-smoke.md` — live-smoke checklist(真 systemd,使用者機器)。

---

## Task 1: unit-file 生成(`units.py`)

**Files:** Create `src/dollos/ctl/__init__.py`、`src/dollos/ctl/units.py`;Test `tests/test_ctl_units.py`

**Interfaces:**
- Produces:
  - `@dataclass class UnitParams: python: str; working_dir: str; daemon_config: str; bridge_config: str; data_root: str; daemon_ws: str = "ws://127.0.0.1:9876"; retention_days: int = 30; restart_sec: int = 3`。
  - `render_daemon_unit(p: UnitParams) -> str`、`render_bridge_unit(p: UnitParams) -> str` — 回 unit 檔內容字串。
  - `resolve_params(*, daemon_config: Path, bridge_config: Path, data_root: Path, python: str | None = None, working_dir: Path | None = None) -> UnitParams`(python 預設 `sys.executable`、working_dir 預設 `Path.cwd()`,全部 `.resolve()` 成絕對)。

daemon unit(`dollos-daemon.service`):
```
[Unit]
Description=DollOS daemon (event loop + memory + IPC WS server)
After=network.target

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={python} -m dollos --config {daemon_config}
Restart=on-failure
RestartSec={restart_sec}

[Install]
WantedBy=default.target
```
bridge unit(`dollos-bridge.service`):
```
[Unit]
Description=DollOS Discord bridge
After=dollos-daemon.service network.target
Wants=dollos-daemon.service

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={python} -m dollos.discord_bridge --daemon {daemon_ws} --config {bridge_config} --data-root {data_root} --retention-days {retention_days}
Restart=on-failure
RestartSec={restart_sec}

[Install]
WantedBy=default.target
```

- [ ] **Step 1: 失敗測試** — `tests/test_ctl_units.py`:
  - `render_daemon_unit` 含 `ExecStart={python} -m dollos --config {daemon_config}`、`Restart=on-failure`、`WantedBy=default.target`;路徑是傳入的絕對值。
  - `render_bridge_unit` 含 `--daemon {daemon_ws}`、`--config {bridge_config}`、`--data-root {data_root}`、`Wants=dollos-daemon.service`、`After=dollos-daemon.service`(軟依賴,**非** `Requires=`)。斷言 `"Requires=" not in bridge_unit`。
  - `resolve_params` 把相對 config 路徑 resolve 成絕對;python 預設 `sys.executable`。

```python
def test_bridge_unit_soft_deps_daemon_not_hard():
    p = UnitParams(python="/venv/bin/python", working_dir="/wd", daemon_config="/c/d.toml",
                   bridge_config="/c/b.toml", data_root="/wd/data")
    u = render_bridge_unit(p)
    assert "Wants=dollos-daemon.service" in u and "After=dollos-daemon.service" in u
    assert "Requires=" not in u  # hard-dep would drag bridge down on daemon restart
    assert "--daemon ws://127.0.0.1:9876" in u and "--config /c/b.toml" in u

def test_resolve_params_absolutizes(tmp_path):
    p = resolve_params(daemon_config=Path("config.toml"), bridge_config=Path("b.toml"),
                       data_root=Path("data"))
    assert Path(p.daemon_config).is_absolute() and Path(p.working_dir).is_absolute()
    assert p.python == sys.executable
```

- [ ] **Step 2-6:** 跑 fail → 實作 `units.py`(f-string 模板 + resolve)→ 跑綠 → 全套回歸 → Commit `feat(ctl): systemd user-unit generation for daemon + bridge (P1g Task 1)`。

---

## Task 2: systemctl/journalctl subprocess wrappers(`systemctl.py`)

**Files:** Create `src/dollos/ctl/systemctl.py`;Test `tests/test_ctl_systemctl.py`

**Interfaces:**
- Produces(全部走 `--user`;`_run(argv) -> subprocess.CompletedProcess` 集中一處,便於 mock):
  - `daemon_reload()`、`enable_now(unit)`/`disable_now(unit)`、`start(unit)`/`stop(unit)`/`restart(unit)`、`status(unit) -> str`(回 `systemctl --user status` 輸出)、`is_active(unit) -> bool`、`journal(unit, *, follow=False, lines=200)`(構造 `journalctl --user -u {unit} [-f] -n {lines}` 並 exec/run)。
  - 命令構造要**可測**:`build_systemctl_argv(verb, unit) -> list[str]`、`build_journal_argv(unit, follow, lines) -> list[str]` 純函式回 argv,`_run` 才真 subprocess。

- [ ] **Step 1: 失敗測試** — `tests/test_ctl_systemctl.py`(mock `subprocess`):
  - `build_systemctl_argv("start", "dollos-daemon.service")` == `["systemctl", "--user", "start", "dollos-daemon.service"]`。
  - `build_journal_argv("dollos-bridge.service", follow=True, lines=50)` == `["journalctl", "--user", "-u", "dollos-bridge.service", "-f", "-n", "50"]`(follow=False 時無 `-f`)。
  - `_run` 非零 exit → 明確 raise/回傳含 stderr(no-fallback:不吞)。
  - `is_active` 解析 `systemctl --user is-active` 的 `active`/`inactive`。
- [ ] **Step 2-6:** 跑 fail → 實作(argv builders + `_run` 集中 subprocess,`--user` 貫穿)→ 跑綠 → 全套回歸 → Commit `feat(ctl): systemctl/journalctl --user wrappers with testable argv builders (P1g Task 2)`。

---

## Task 3: install / uninstall(`cli.py` install 分支)

**Files:** Create `src/dollos/ctl/cli.py`(先只 install/uninstall + 骨架);Test `tests/test_ctl_install.py`

**Interfaces:**
- Consumes: Task 1 `units.py`、Task 2 `systemctl.py`。
- Produces: `install(*, unit_dir: Path, daemon_config, bridge_config, data_root, python=None, working_dir=None)` —— 生成兩 unit 寫進 `unit_dir`(預設 `~/.config/systemd/user/`)、`daemon_reload()`;`uninstall(*, unit_dir)` —— `stop` 兩者 + 刪 unit 檔 + `daemon_reload()`。`_user_unit_dir() -> Path` 回 `Path.home()/".config/systemd/user"`。

- [ ] **Step 1: 失敗測試** — `tests/test_ctl_install.py`(unit_dir=tmp_path,mock systemctl):
  - `install(unit_dir=tmp)` 寫出 `tmp/dollos-daemon.service` + `tmp/dollos-bridge.service`,內容含解析後絕對路徑;`daemon_reload` 被呼叫。
  - 冪等:重跑 install 覆寫(不追加、不報錯),檔案數仍 2。
  - `uninstall(unit_dir=tmp)` 呼叫 `stop` 兩 unit + 刪兩檔 + daemon_reload;缺檔時不炸(冪等)。
  - 敏感值不進 unit:token 不出現在 unit 檔(unit 只有 config 路徑)。
- [ ] **Step 2-6:** 跑 fail → 實作 install/uninstall + `_user_unit_dir`(install 時 `unit_dir.mkdir(parents=True, exist_ok=True)`)→ 跑綠 → 全套回歸 → Commit `feat(ctl): dollosctl install/uninstall — write user units + daemon-reload (P1g Task 3)`。

---

## Task 4: start/stop/restart/status/logs + CLI dispatch(`cli.py` 完成)

**Files:** Modify `src/dollos/ctl/cli.py`(完成 argparse + 全 subcommand);Modify `pyproject.toml`(`dollosctl` script);Test `tests/test_ctl_cli.py`

**Interfaces:**
- Produces: `main(argv=None) -> int` —— argparse subcommands `install`/`uninstall`/`start`/`stop`/`restart`/`status`/`logs`。`start/stop/restart` 對 **兩個 unit** 依序作用(start:daemon 先 bridge 後;stop:反序)。`status` 印兩者。`logs` 收 positional `{daemon|bridge}` + `-f/--follow` + `-n/--lines`。`pyproject [project.scripts]` 加 `dollosctl = "dollos.ctl.cli:main"`。

- [ ] **Step 1: 失敗測試** — `tests/test_ctl_cli.py`(mock systemctl 層,斷 dispatch 正確):
  - `main(["start"])` → `start("dollos-daemon.service")` 先於 `start("dollos-bridge.service")`。
  - `main(["stop"])` → 反序(bridge 先 stop)。
  - `main(["status"])` → 兩 unit 的 status 都被查。
  - `main(["logs", "bridge", "-f"])` → journal argv 針對 `dollos-bridge.service`、follow=True。
  - `main(["logs", "daemon", "-n", "50"])` → lines=50。
  - 未知 subcommand / 缺參數 → 非零 exit(argparse)。
  - `main(["install", "--daemon-config", "...", "--bridge-config", "...", "--data-root", "..."])` → install 被以解析參數呼叫。
- [ ] **Step 2-6:** 跑 fail → 實作 argparse dispatch + start/stop/restart/status/logs(呼 Task 2 wrappers)+ pyproject script → 跑綠(`uv run dollosctl --help` 可執行)→ 全套回歸 → Commit `feat(ctl): dollosctl start/stop/restart/status/logs + console script (P1g Task 4)`。

---

## Task 5: live-smoke checklist + README/CLAUDE 更新

**Files:** Create `docs/dollosctl-smoke.md`;Modify `CLAUDE.md`(Build/Run 加 dollosctl)

**背景**:真 systemd 起停無法在 CI 測(無 user session / 無真服務);本 task 產出使用者機器上的 live-smoke checklist,並把 dollosctl 納入 Build/Run 文件。

- [ ] **Step 1:** 寫 `docs/dollosctl-smoke.md`:
  - 前置:`uv sync`(裝 `dollosctl`)、備妥 `config.toml`(daemon)+ bridge config(含 token/owner_discord_id/allowlist)。
  - 步驟:`dollosctl install --daemon-config config.toml --bridge-config <bridge>.toml --data-root data` → `systemctl --user daemon-reload`(install 已做)→ `dollosctl start` → `dollosctl status`(兩 unit `active (running)`)→ `dollosctl logs daemon -f`(見 event loop 起來)→ 在測試伺服器 @ 她 → 她回(端到端)→ `dollosctl restart` → 確認重連 → `dollosctl stop` → `dollosctl uninstall`。
  - 驗收點:daemon 崩潰(kill)→ systemd `Restart=on-failure` 自動拉起;bridge 崩潰不傷 daemon;開機自啟(`systemctl --user enable` / `loginctl enable-linger` 註記)。
- [ ] **Step 2:** `CLAUDE.md` Build/Run 段加 dollosctl 一鍵起停(取代手動 `python -m dollos` + `python -m dollos.discord_bridge` 兩條)。
- [ ] **Step 3:** 全套 `uv run pytest tests/ -q`(最後 task,全綠 minus 3 torch)。
- [ ] **Step 4:** Commit `docs(ctl): dollosctl live-smoke checklist + Build/Run wiring (P1g Task 5)`。

---

## Self-Review

- [x] systemd user units(daemon + bridge,軟 ordering)→ Task 1
- [x] Restart=on-failure(崩潰自動拉起,對齊 spec §3.2)→ Task 1
- [x] install 解析絕對路徑(sys.executable + cwd + config)→ Task 1/3
- [x] systemctl/journalctl --user wrappers(可測 argv)→ Task 2
- [x] dollosctl install/uninstall/start/stop/restart/status/logs → Task 3/4
- [x] token 不進 unit/git(unit 只引用 config 路徑)→ Task 3 constraint + 測試
- [x] 冪等 install → Task 3
- [x] console script → Task 4 pyproject
- [x] live-smoke(真 systemd 使用者機器)→ Task 5 checklist(CI 不可測 systemd,明文)

**Placeholder scan:** 每 code step 有實際 unit 模板 / argv 斷言。`UnitParams` 型別貫穿。
**Type consistency:** `render_*_unit(UnitParams)`、`build_*_argv(...) -> list[str]`、`install(*, unit_dir, ...)` Task 1→4 一致。
**跨 task:** Task 3 consume Task 1(units)+ Task 2(systemctl);Task 4 consume 全部 + 加 dispatch;Task 5 文件。

---

## 執行銜接

依 `feedback_subagent_driven_default`:直接進 `superpowers:subagent-driven-development`,每 task fresh implementer + reviewer(sonnet),whole-branch review 用 opus(查:unit 軟依賴非硬綁、絕對路徑、token 不外洩、subprocess 失敗不吞、冪等)。worktree `.worktrees/p1g-service/` on branch `p1g-service`。**真 systemd 起停是 live-smoke(使用者機器),CI 只測 unit 生成 + 命令構造**。完成 merge 後 P1 code 全備 —— dogfood 上線只差使用者跑 `dollosctl install && dollosctl start` + 真 bot token/伺服器(本質使用者動作)。之後 P2(真實數據調注意力)→ P3(語音)。
