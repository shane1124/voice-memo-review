"""macOS 开机自启（launchd LaunchAgent）与受控无缝重启接力（纯标准库，零第三方依赖）。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable

LAUNCH_AGENT_LABEL = "com.voice-memo-review.daemon"
RESTART_TOKEN_FILE = ".restart_token"
RESTART_TOKEN_TTL_SEC = 35.0


@dataclass(frozen=True)
class AutostartStatus:
    supported: bool
    enabled: bool
    loaded: bool
    plist_path: str
    label: str = LAUNCH_AGENT_LABEL

    def to_dict(self) -> dict:
        return {
            "supported": self.supported,
            "enabled": self.enabled,
            "loaded": self.loaded,
            "plist_path": self.plist_path,
            "label": self.label,
        }


def write_restart_token(vmr_home: Path, token: str, *, ttl_sec: float = RESTART_TOKEN_TTL_SEC) -> Path:
    """写入 0600 权限短命接力令牌，让受控热重启后的新守护沿用同一控制台会话令牌。"""
    target = Path(vmr_home).expanduser().resolve() / RESTART_TOKEN_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"token": token, "expires_at": time.time() + max(5.0, float(ttl_sec))})
    fd, tmp_name = tempfile.mkstemp(prefix=".rtok-", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
        os.chmod(target, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def consume_restart_token(vmr_home: Path) -> str | None:
    """新守护启动时读取并立刻销毁短命接力令牌；超时或格式异常则返回 None。"""
    target = Path(vmr_home).expanduser().resolve() / RESTART_TOKEN_FILE
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        raw = None
    finally:
        target.unlink(missing_ok=True)
    if not isinstance(raw, dict):
        return None
    tok = str(raw.get("token") or "").strip()
    exp = float(raw.get("expires_at") or 0.0)
    if tok and time.time() <= exp:
        return tok
    return None


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_pid_exit(pid: int, *, timeout_sec: float = 8.0) -> bool:
    deadline = time.monotonic() + max(0.5, timeout_sec)
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.15)
    return not _pid_alive(pid)


class AutostartManager:
    """管理 ~/Library/LaunchAgents/com.voice-memo-review.daemon.plist。"""

    def __init__(
        self,
        *,
        vmr_bin: Path,
        vmr_home: Path,
        launch_agents_dir: Path | None = None,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.vmr_bin = Path(vmr_bin).expanduser()
        self.vmr_home = Path(vmr_home).expanduser().resolve()
        self.launch_agents_dir = (
            Path(launch_agents_dir).expanduser().resolve()
            if launch_agents_dir is not None
            else (Path.home() / "Library" / "LaunchAgents").resolve()
        )
        self._run = runner or _default_runner
        self._platform = (platform_name or sys.platform).lower()

    @classmethod
    def default(cls, vmr_home: Path) -> "AutostartManager":
        pkg_root = Path(__file__).resolve().parent.parent
        local_bin = Path.home() / ".local" / "bin" / "vmr"
        vmr_bin = local_bin if local_bin.is_file() else (pkg_root / "vmr").resolve()
        return cls(vmr_bin=vmr_bin, vmr_home=vmr_home)

    @property
    def supported(self) -> bool:
        return self._platform == "darwin"

    @property
    def plist_path(self) -> Path:
        return self.launch_agents_dir / f"{LAUNCH_AGENT_LABEL}.plist"

    @property
    def domain_target(self) -> str:
        return f"gui/{os.getuid()}"

    @property
    def service_target(self) -> str:
        return f"{self.domain_target}/{LAUNCH_AGENT_LABEL}"

    def build_plist(self) -> dict:
        logs_dir = self.vmr_home / "logs"
        home_str = str(Path.home())
        path_env = ":".join(
            [
                "/opt/homebrew/bin",
                "/usr/local/bin",
                str(Path.home() / ".local" / "bin"),
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            ]
        )
        return {
            "Label": LAUNCH_AGENT_LABEL,
            "ProgramArguments": [
                str(self.vmr_bin),
                "--vmr-home",
                str(self.vmr_home),
                "serve",
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "WorkingDirectory": str(self.vmr_home),
            "StandardOutPath": str(logs_dir / "launchd.stdout.log"),
            "StandardErrorPath": str(logs_dir / "launchd.stderr.log"),
            "EnvironmentVariables": {
                "PATH": path_env,
                "HOME": home_str,
                "LANG": "zh_CN.UTF-8",
            },
        }

    def status(self) -> AutostartStatus:
        enabled = self.plist_path.is_file()
        loaded = False
        if self.supported and enabled:
            try:
                res = self._run(["launchctl", "print", self.service_target])
                loaded = res.returncode == 0
            except Exception:
                loaded = False
        return AutostartStatus(
            supported=self.supported,
            enabled=enabled,
            loaded=loaded,
            plist_path=str(self.plist_path),
        )

    def write_plist(self) -> Path:
        (self.vmr_home / "logs").mkdir(parents=True, exist_ok=True)
        self.launch_agents_dir.mkdir(parents=True, exist_ok=True)
        data = plistlib.dumps(self.build_plist(), fmt=plistlib.FMT_XML)
        fd, tmp_name = tempfile.mkstemp(prefix=".vmr-plist-", suffix=".tmp", dir=str(self.launch_agents_dir))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, self.plist_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        return self.plist_path

    def enable(self, *, load_now: bool = True, stop_existing_pid: int | None = None) -> tuple[bool, str]:
        if not self.supported:
            return False, "开机自启目前仅支持 macOS（launchd）"
        self.write_plist()
        if not load_now:
            return True, f"已写入开机自启配置：{self.plist_path}"

        if stop_existing_pid and stop_existing_pid != os.getpid() and _pid_alive(stop_existing_pid):
            try:
                os.kill(stop_existing_pid, signal.SIGTERM)
            except OSError:
                pass
            wait_pid_exit(stop_existing_pid, timeout_sec=6.0)

        # 幂等重装：先尝试 bootout 清理残留注册，再 bootstrap
        self._run(["launchctl", "bootout", self.service_target])
        res = self._run(["launchctl", "bootstrap", self.domain_target, str(self.plist_path)])
        if res.returncode != 0:
            # 个别场景已加载，尝试 kickstart 接管
            ks = self._run(["launchctl", "kickstart", "-k", self.service_target])
            if ks.returncode != 0:
                detail = (res.stderr or res.stdout or ks.stderr or ks.stdout or "").strip()[-240:]
                return False, f"已写入 plist，但 launchd 加载失败：{detail}"
        return True, f"已开启开机自启并由 macOS 后台接管（{self.plist_path}）"

    def disable(self, *, unload_now: bool = True) -> tuple[bool, str]:
        if not self.supported:
            return False, "开机自启目前仅支持 macOS（launchd）"
        if unload_now:
            self._run(["launchctl", "bootout", self.service_target])
        self.plist_path.unlink(missing_ok=True)
        return True, "已关闭开机自启并移除 LaunchAgent 配置"

    def kickstart(self) -> tuple[bool, str]:
        if not self.supported or not self.plist_path.is_file():
            return False, "未启用 launchd 托管"
        res = self._run(["launchctl", "kickstart", "-k", self.service_target])
        if res.returncode == 0:
            return True, "已触发 launchd 重启新版守护"
        # 未加载时退回 bootstrap
        bs = self._run(["launchctl", "bootstrap", self.domain_target, str(self.plist_path)])
        if bs.returncode == 0:
            return True, "已通过 launchd 重新拉起新版守护"
        detail = (res.stderr or bs.stderr or "").strip()[-240:]
        return False, f"launchd 重启失败：{detail}"


def spawn_restart_helper(
    *,
    vmr_bin: Path,
    vmr_home: Path,
    old_pid: int,
    token: str = "",
    use_launchd: bool = False,
) -> None:
    """拉起独立脱离进程，安全等待旧守护释放锁后重启新版守护或交由 launchd 接管。"""
    if token:
        write_restart_token(vmr_home, token)
    cmd = [
        str(vmr_bin),
        "--vmr-home",
        str(vmr_home),
        "_restart-helper",
        "--old-pid",
        str(old_pid),
    ]
    if use_launchd:
        cmd.append("--use-launchd")
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def run_restart_helper(*, vmr_bin: Path, vmr_home: Path, old_pid: int, use_launchd: bool) -> int:
    """独立重启接力子进程入口：等旧守护退出 → 通过 launchd 或后台 serve 拉起新守护。"""
    if old_pid > 0 and old_pid != os.getpid():
        # 给旧进程一点时间完成 HTTP 响应和自行退出
        if not wait_pid_exit(old_pid, timeout_sec=3.0) and _pid_alive(old_pid):
            try:
                os.kill(old_pid, signal.SIGTERM)
            except OSError:
                pass
            wait_pid_exit(old_pid, timeout_sec=6.0)

    mgr = AutostartManager(vmr_bin=vmr_bin, vmr_home=vmr_home)
    if use_launchd and mgr.supported and mgr.plist_path.is_file():
        ok, _ = mgr.kickstart()
        if ok:
            return 0
        # 兜底尝试 bootstrap
        ok2, _ = mgr.enable(load_now=True)
        if ok2:
            return 0

    # 未启用 launchd 时直接用最新受管 vmr 后台拉起 serve
    logs_dir = vmr_home / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "daemon.log"
    with open(log_path, "a", encoding="utf-8") as lf:
        subprocess.Popen(
            [str(vmr_bin), "--vmr-home", str(vmr_home), "serve"],
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return 0
