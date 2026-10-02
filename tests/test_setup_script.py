"""两个接入脚本的回归测试。

* `docs/examples/mtbots-remote-setup.sh`：远端一次性准备（建用户/装守卫/写 authorized_keys）
* `scripts/setup-remote-host.sh`：bot 这侧的向导（ssh/scp 用桩替换，不联网、不碰真实主机）

远端准备脚本的「真跑」路径要 root（要 chown 目标用户），普通用户下自动跳过；
CI 里是普通用户，所以 CI 覆盖 dry-run 与参数校验，root 环境再覆盖落盘结果。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REMOTE_SETUP = REPO / "docs" / "examples" / "mtbots-remote-setup.sh"
WIZARD = REPO / "scripts" / "setup-remote-host.sh"
GUARD = REPO / "docs" / "examples" / "mtbots-compose-guard.sh"

#: ssh 桩：记录调用，并对向导需要的几个远端命令给出确定答案
SSH_STUB = """#!/bin/sh
log=$FAKE_DIR/ssh.log
printf '%s\\n' "$*" >> "$log"
last=""
for a in "$@"; do last=$a; done
case "$last" in
  'printf %s "$HOME"') printf '/home/mtbots'; exit 0 ;;
esac
case "$last" in
  *'docker compose version'*) echo 'Docker Compose version v2.35.1'; exit 0 ;;
  *'sudo -n install'*) exit 1 ;;
esac
# 模拟「远端还没有这把公钥」：deny-login 标记存在时，连 true 都失败
case "$last" in
  true) [ -f "$FAKE_DIR/deny-login" ] && exit 255 ;;
esac
exit 0
"""

SCP_STUB = """#!/bin/sh
log=$FAKE_DIR/scp.log
printf '%s\\n' "$*" >> "$log"
prev=""; last=""
for a in "$@"; do prev=$last; last=$a; done
mkdir -p "$FAKE_DIR/upload"
cp "$prev" "$FAKE_DIR/upload/$(basename "$last")"
exit 0
"""

SUDO_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DIR/sudo.log"
exit 1
"""

CURL_STUB = """#!/bin/sh
# curl -fsSL URL -o DEST  → 从 FIXTURE_DIR 里按 basename 取
url=""; dest=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) dest=$2; shift 2 ;;
    http*) url=$1; shift ;;
    *) shift ;;
  esac
done
printf '%s\\n' "$url" >> "$FAKE_DIR/curl.log"
cp "$FIXTURE_DIR/$(basename "$url")" "$dest"
"""

COPY_ID_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DIR/ssh-copy-id.log"
exit 0
"""


def _run(script: Path, *args: str, cwd: Path, env: dict, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(script), *args],
        cwd=str(cwd),
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
    )


@unittest.skipUnless(os.name == "posix", "脚本测试只在 POSIX 上跑")
class WizardScriptTest(unittest.TestCase):
    """`scripts/setup-remote-host.sh`：用桩 ssh/scp 走完整流程。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mtbots-wizard-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "mbots"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "docs" / "examples").mkdir(parents=True)
        (self.root / "data").mkdir()
        (self.root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        shutil.copy2(WIZARD, self.root / "scripts" / "setup-remote-host.sh")
        shutil.copy2(GUARD, self.root / "docs" / "examples" / "mtbots-compose-guard.sh")
        shutil.copy2(REMOTE_SETUP, self.root / "docs" / "examples" / "mtbots-remote-setup.sh")

        self.fake = base / "fake"
        self.fake.mkdir()
        for name, body in (
            ("ssh", SSH_STUB),
            ("scp", SCP_STUB),
            ("sudo", SUDO_STUB),
            ("ssh-copy-id", COPY_ID_STUB),
            ("curl", CURL_STUB),
        ):
            path = self.fake / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

        self.env = dict(os.environ)
        self.env["PATH"] = "%s:%s" % (self.fake, os.environ.get("PATH", ""))
        self.env["FAKE_DIR"] = str(self.fake)
        self.env["FIXTURE_DIR"] = str(base / "fixtures")
        (base / "fixtures").mkdir()
        shutil.copy2(GUARD, base / "fixtures" / "mtbots-compose-guard.sh")
        shutil.copy2(REMOTE_SETUP, base / "fixtures" / "mtbots-remote-setup.sh")
        self.script = self.root / "scripts" / "setup-remote-host.sh"
        self.hosts_file = self.root / "data" / "docker-hosts.json"

    # ---------- 工具 ----------
    def _wizard(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return _run(self.script, *args, cwd=self.root, env=self.env, stdin=stdin)

    def _hosts(self) -> dict:
        return json.loads(self.hosts_file.read_text(encoding="utf-8"))

    def _upload(self, name: str) -> str:
        path = self.fake / "upload" / name
        self.assertTrue(path.exists(), "没上传 %s（上传目录：%s）" % (name, sorted(p.name for p in (self.fake / "upload").glob("*")) if (self.fake / "upload").exists() else []))
        return path.read_text(encoding="utf-8")

    def _log(self, name: str) -> str:
        path = self.fake / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # ---------- 用例 ----------
    def test_existing_mode_writes_hosts_and_guarded_key_line(self):
        proc = self._wizard(
            "--mode", "existing",
            "--host", "10.0.0.5",
            "--user", "admin",
            "--id", "vps",
            "--label", "Oracle 东京",
            "--roots", "/opt,/srv",
            "--yes",
            "--no-restart",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        hosts = self._hosts()["hosts"]
        self.assertEqual([h["id"] for h in hosts], ["local", "vps"])
        entry = hosts[1]
        self.assertEqual(entry["kind"], "ssh")
        self.assertEqual(entry["target"], "admin@10.0.0.5")
        self.assertEqual(entry["port"], 22)
        self.assertEqual(entry["identity"], "/app/data/ssh/id_ed25519")
        self.assertEqual(entry["known_hosts"], "/app/data/ssh/known_hosts")
        self.assertEqual(entry["strict"], "accept-new")
        self.assertEqual(entry["roots"], ["/opt", "/srv"])

        # sudo -n 不可用（桩故意失败）→ 守卫落到 ~/.local/bin，且写进 command=
        line = self._upload("mtbots-ak-line").strip()
        self.assertTrue(line.startswith('command="/home/mtbots/.local/bin/mtbots-compose-guard",restrict ssh-ed25519 '), line)
        self.assertIn(".local/bin", self._log("ssh.log"))
        self.assertIn("mtbots-compose-guard.sh", self._log("scp.log"))
        self.assertIn("mtbots-apply-ak.sh", self._log("scp.log"))

    def test_rerun_same_id_does_not_duplicate(self):
        args = ("--mode", "existing", "--host", "10.0.0.5", "--user", "admin",
                "--id", "vps", "--label", "Oracle", "--yes", "--no-restart")
        first = self._wizard(*args)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        line_before = self._upload("mtbots-ak-line")

        second = self._wizard(*args)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

        hosts = self._hosts()["hosts"]
        self.assertEqual([h["id"] for h in hosts], ["local", "vps"], "同 id 必须替换而不是追加")
        self.assertEqual(self._upload("mtbots-ak-line"), line_before, "同一把 key 的行内容必须稳定")

    def test_second_host_appends_and_keeps_local_once(self):
        for host_id in ("vps1", "vps2"):
            proc = self._wizard("--mode", "existing", "--host", "10.0.0.%s" % host_id[-1],
                                "--user", "admin", "--id", host_id, "--yes", "--no-restart")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        hosts = self._hosts()["hosts"]
        self.assertEqual([h["id"] for h in hosts], ["local", "vps1", "vps2"])

    def test_create_mode_drives_remote_setup_script(self):
        proc = self._wizard(
            "--mode", "create",
            "--host", "10.0.0.5",
            "--login-user", "root",
            "--user", "mtbots",
            "--id", "vps",
            "--yes",
            "--no-restart",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        ssh_log = self._log("ssh.log")
        self.assertIn("tar xzf - -C /tmp/mtbots-setup", ssh_log, "应该把脚本+公钥打包上传")
        self.assertIn("sudo sh /tmp/mtbots-setup/mtbots-remote-setup.sh", ssh_log)
        self.assertIn("--user 'mtbots'", ssh_log)
        self.assertIn("--pubkey /tmp/mtbots-setup/id_ed25519.pub", ssh_log)
        self.assertIn("--guard /tmp/mtbots-setup/mtbots-compose-guard.sh", ssh_log)
        self.assertIn("--guard-dest '/usr/local/bin'", ssh_log)
        self.assertNotIn("ssh-copy-id", self._log("ssh-copy-id.log"), "create 模式不需要 ssh-copy-id")

        self.assertEqual([h["id"] for h in self._hosts()["hosts"]], ["local", "vps"])

    def test_dry_run_touches_nothing(self):
        proc = self._wizard("--mode", "existing", "--host", "10.0.0.5", "--user", "admin",
                            "--id", "vps", "--yes", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("dry-run", proc.stdout)
        self.assertFalse(self.hosts_file.exists(), "dry-run 不能写主机清单")
        self.assertFalse((self.root / "data" / "ssh").exists(), "dry-run 不能生成密钥")
        self.assertEqual(self._log("ssh.log"), "", "dry-run 不能连远端")

    def test_invalid_id_is_rejected(self):
        proc = self._wizard("--mode", "existing", "--host", "10.0.0.5", "--user", "admin",
                            "--id", "Bad ID", "--yes", "--no-restart")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("主机 id", proc.stderr)
        self.assertFalse(self.hosts_file.exists())

    def test_piped_run_uses_cwd_as_project_root(self):
        """`curl … | sh`（stdin 是脚本）时项目根取当前目录，且不再往 stdin 要输入。"""
        project = Path(self.tmp.name) / "plain-project"
        (project / "data").mkdir(parents=True)
        (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        script_text = self.script.read_text(encoding="utf-8")

        proc = subprocess.run(
            ["sh", "-s", "--",
             "--mode", "existing", "--host", "10.0.0.5", "--user", "admin",
             "--id", "vps", "--label", "VPS", "--roots", "", "--no-guard",
             "--yes", "--no-restart"],
            cwd=str(project),
            env=self.env,
            input=script_text,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("curl 模式", proc.stdout)

        hosts = json.loads((project / "data" / "docker-hosts.json").read_text(encoding="utf-8"))
        self.assertEqual([h["id"] for h in hosts["hosts"]], ["local", "vps"])
        self.assertNotIn("roots", hosts["hosts"][1], "空 --roots 不能被写成 []")
        self.assertFalse(self.hosts_file.exists(), "不能写到别的项目根里")

    def test_bash_process_substitution_uses_cwd_as_project_root(self):
        """`bash <(curl …)`（$0 是 /dev/fd/63）：项目根取当前目录。"""
        project = Path(self.tmp.name) / "ps-project"
        (project / "data").mkdir(parents=True)
        (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

        inner = ('bash <(cat %s) --mode existing --host 10.0.0.5 --user admin --id vps '
                 '--label V --roots "" --no-guard --yes --dry-run' % shlex.quote(str(self.script)))
        proc = subprocess.run(
            ["bash", "-c", inner], cwd=str(project), env=self.env,
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("curl 模式", proc.stdout)
        self.assertIn("项目根：%s" % project, proc.stdout)
        self.assertFalse((project / "data" / "docker-hosts.json").exists(), "dry-run 不落盘")

    def test_fetches_companions_when_repo_files_are_absent(self):
        """脚本旁边没有 docs/examples 时，按 --ref 从 GitHub 取守卫与远端准备脚本。"""
        bare = Path(self.tmp.name) / "bare"
        (bare / "scripts").mkdir(parents=True)
        (bare / "data").mkdir()
        (bare / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        shutil.copy2(WIZARD, bare / "scripts" / "setup-remote-host.sh")

        proc = subprocess.run(
            ["sh", str(bare / "scripts" / "setup-remote-host.sh"),
             "--mode", "create", "--host", "10.0.0.5", "--login-user", "root", "--user", "mtbots",
             "--id", "vps", "--label", "VPS", "--roots", "/opt", "--ref", "v1.2.1",
             "--yes", "--no-restart"],
            cwd=str(bare),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        urls = self._log("curl.log")
        self.assertIn("https://raw.githubusercontent.com/MbAIGC/MTBots/v1.2.1/docs/examples/mtbots-compose-guard.sh", urls)
        self.assertIn("https://raw.githubusercontent.com/MbAIGC/MTBots/v1.2.1/docs/examples/mtbots-remote-setup.sh", urls)
        self.assertIn("sudo sh /tmp/mtbots-setup/mtbots-remote-setup.sh", self._log("ssh.log"))

        hosts = json.loads((bare / "data" / "docker-hosts.json").read_text(encoding="utf-8"))
        self.assertEqual([h["id"] for h in hosts["hosts"]], ["local", "vps"])

    def test_login_failure_points_at_remote_one_liner(self):
        """密钥登不上时，要把「远端自己跑那条 curl」摆在眼前，而不是只丢一个密码提示。"""
        (self.fake / "deny-login").write_text("1", encoding="utf-8")
        (self.fake / "ssh-copy-id").write_text(
            "#!/bin/sh\nexit 1\n", encoding="utf-8"
        )
        (self.fake / "ssh-copy-id").chmod(0o755)

        proc = self._wizard("--mode", "existing", "--host", "10.0.0.5", "--user", "admin",
                            "--id", "vps", "--label", "V", "--roots", "", "--no-guard",
                            "--yes", "--no-restart")
        self.assertNotEqual(proc.returncode, 0)
        err = proc.stderr
        self.assertIn("远端还没有这把公钥", err)
        self.assertIn("sudo bash <(curl -fsSL https://raw.githubusercontent.com/MbAIGC/MTBots/", err)
        self.assertIn("mtbots-remote-setup.sh", err)
        self.assertFalse(self.hosts_file.exists())

    def test_local_host_can_be_omitted(self):
        proc = self._wizard("--mode", "existing", "--host", "10.0.0.5", "--user", "admin",
                            "--id", "vps", "--no-local", "--yes", "--no-restart")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([h["id"] for h in self._hosts()["hosts"]], ["vps"])


@unittest.skipUnless(os.name == "posix", "脚本测试只在 POSIX 上跑")
class RemoteSetupScriptTest(unittest.TestCase):
    """`docs/examples/mtbots-remote-setup.sh`：远端那侧的全部操作。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mtbots-remote-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.bin = self.base / "bin"
        self.pub = self.base / "id_ed25519.pub"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "mtbots@bot", "-f", str(self.base / "id_ed25519")],
            check=True, capture_output=True,
        )
        self.pub.write_text((self.base / "id_ed25519.pub").read_text(encoding="utf-8"), encoding="utf-8")
        self.args = (
            "--user", os.environ.get("USER") or "root",
            "--home", str(self.home),
            "--pubkey", str(self.pub),
            "--guard", str(GUARD),
            "--guard-dest", str(self.bin),
            "--no-useradd",
        )

    def _run(self, *extra: str, stdin: str = "") -> subprocess.CompletedProcess:
        return _run(REMOTE_SETUP, *self.args, *extra, cwd=self.base, env=dict(os.environ), stdin=stdin)

    def _ak(self) -> str:
        return (self.home / ".ssh" / "authorized_keys").read_text(encoding="utf-8")

    def test_pubkey_line_and_guard_url_dry_run_is_offline(self):
        """`curl|sh` 场景：公钥用字符串给、守卫用 URL 给；dry-run 不联网也能看出要装什么。"""
        pub_line = self.pub.read_text(encoding="utf-8").strip()
        proc = _run(
            REMOTE_SETUP,
            "--user", os.environ.get("USER") or "root",
            "--home", str(self.home),
            "--pubkey-line", pub_line,
            "--guard-url", "https://example.invalid/mtbots-compose-guard.sh",
            "--guard-dest", str(self.bin),
            "--no-useradd",
            "--dry-run",
            cwd=self.base,
            env=dict(os.environ),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("https://example.invalid/mtbots-compose-guard.sh", proc.stdout)
        self.assertIn('command="%s/mtbots-compose-guard",restrict ssh-ed25519 %s'
                      % (self.bin, pub_line.split()[1]), proc.stdout)
        self.assertFalse((self.home / ".ssh").exists())

    def test_interactive_mode_collects_user_and_pubkey(self):
        """不给参数、强制交互时：账号与公钥从提问里拿（--ask 便于无终端环境/测试）。"""
        answers = "mtbots\n%s\nn\n" % self.pub.read_text(encoding="utf-8").strip()
        proc = _run(
            REMOTE_SETUP, "--ask", "--home", str(self.home), "--guard-dest", str(self.bin),
            "--no-useradd", "--dry-run",
            cwd=self.base, env=dict(os.environ), stdin=answers,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("要授权/创建的远端账号", proc.stderr + proc.stdout)
        self.assertIn("目标账号：mtbots", proc.stdout)
        self.assertIn("ssh-ed25519", proc.stdout)

    def test_interactive_yes_installs_guard_from_default_url(self):
        """交互里同意装守卫 → 用脚本自带的官方 URL（dry-run 只打印，不联网）。"""
        answers = "mtbots\n%s\ny\n/srv/bin\n" % self.pub.read_text(encoding="utf-8").strip()
        proc = _run(
            REMOTE_SETUP, "--ask", "--home", str(self.home), "--no-useradd", "--dry-run",
            cwd=self.base, env=dict(os.environ), stdin=answers,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("raw.githubusercontent.com/MbAIGC/MTBots/", proc.stdout)
        self.assertIn('command="/srv/bin/mtbots-compose-guard",restrict', proc.stdout)

    def test_truncated_pubkey_is_rejected_early(self):
        """手粘公钥被截断时，要在写文件之前就报出来（不要等 ssh 连不上再猜）。"""
        proc = _run(REMOTE_SETUP, "--user", "root", "--home", str(self.home),
                    "--pubkey-line", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKG6lncl0UM4cTKs8Hw",
                    "--guard", str(GUARD), "--guard-dest", str(self.bin), "--no-useradd",
                    "--dry-run", cwd=self.base, env=dict(os.environ))
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("公钥解析失败", proc.stderr)
        self.assertFalse((self.home / ".ssh").exists())

    def test_missing_pubkey_source_is_rejected(self):
        proc = _run(REMOTE_SETUP, "--user", "root", "--home", str(self.home), "--dry-run",
                    cwd=self.base, env=dict(os.environ))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("公钥", proc.stderr)

    @unittest.skipUnless(os.geteuid() == 0, "写 authorized_keys/chown 需要 root")
    def test_pubkey_line_writes_the_same_key(self):
        pub_line = self.pub.read_text(encoding="utf-8").strip()
        proc = _run(REMOTE_SETUP, "--user", os.environ.get("USER") or "root",
                    "--home", str(self.home), "--pubkey-line", pub_line,
                    "--guard", str(GUARD), "--guard-dest", str(self.bin),
                    "--no-useradd", cwd=self.base, env=dict(os.environ))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        line = self._ak().strip()
        # 脚本按「类型 + blob」写入（注释丢掉），与文件来源无关
        self.assertEqual(line, 'command="%s/mtbots-compose-guard",restrict %s'
                              % (self.bin, " ".join(pub_line.split()[:2])))

    def test_dry_run_prints_plan_without_writing(self):
        proc = self._run("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("dry-run", proc.stdout)
        self.assertIn("command=", proc.stdout)
        self.assertFalse((self.home / ".ssh").exists())
        self.assertFalse(self.bin.exists())

    def test_requires_pubkey(self):
        proc = _run(REMOTE_SETUP, "--user", "root", cwd=self.base, env=dict(os.environ))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--pubkey", proc.stderr)

    @unittest.skipUnless(os.geteuid() == 0, "写 authorized_keys/chown 需要 root")
    def test_applies_guard_line_and_is_idempotent(self):
        first = self._run()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        guard = self.bin / "mtbots-compose-guard"
        self.assertTrue(guard.exists())
        self.assertEqual(guard.stat().st_mode & 0o777, 0o755)

        line = self._ak().strip()
        blob = self.pub.read_text(encoding="utf-8").split()[1]
        self.assertEqual(line, 'command="%s",restrict ssh-ed25519 %s' % (guard, blob))
        self.assertEqual((self.home / ".ssh" / "authorized_keys").stat().st_mode & 0o777, 0o600)

        second = self._run()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self._ak().strip(), line, "同一把 key 只能有一行")
        self.assertEqual(len(self._ak().strip().splitlines()), 1)
        backups = list((self.home / ".ssh").glob("authorized_keys.bak.*"))
        self.assertEqual(len(backups), 1, "只在真正改动时备份一次")

    @unittest.skipUnless(os.geteuid() == 0, "写 authorized_keys/chown 需要 root")
    def test_old_sshd_falls_back_to_long_options(self):
        """远端 sshd < 7.2 不认 restrict：自动改成 no-port-forwarding,… 长格式。"""
        fake = self.base / "bin-sshd"
        fake.mkdir()
        sshd = fake / "sshd"
        sshd.write_text("#!/bin/sh\necho 'OpenSSH_6.6.1p1, OpenSSL 1.0.1f'\n", encoding="utf-8")
        sshd.chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (fake, os.environ.get("PATH", "")))

        proc = _run(REMOTE_SETUP, *self.args, cwd=self.base, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("不支持 restrict", proc.stderr)

        line = self._ak().strip()
        self.assertNotIn("restrict", line)
        self.assertIn('command="%s/mtbots-compose-guard",no-port-forwarding,no-agent-forwarding,'
                      'no-X11-forwarding,no-pty,no-user-rc ssh-ed25519' % self.bin, line)

    @unittest.skipUnless(os.geteuid() == 0, "写 authorized_keys/chown 需要 root")
    def test_without_guard_writes_plain_line(self):
        proc = self._run("--guard", "")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        line = self._ak().strip()
        self.assertTrue(line.startswith("ssh-ed25519 "), line)
        self.assertNotIn("command=", line)

    @unittest.skipUnless(os.geteuid() == 0, "写 authorized_keys/chown 需要 root")
    def test_keeps_other_keys_in_authorized_keys(self):
        ssh_dir = self.home / ".ssh"
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        other = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOTHERKEY someone@else"
        (ssh_dir / "authorized_keys").write_text(other + "\n", encoding="utf-8")

        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        content = self._ak()
        self.assertIn(other, content, "别人的 key 不能被删掉")
        self.assertEqual(len([ln for ln in content.splitlines() if ln.strip()]), 2)


if __name__ == "__main__":
    unittest.main()
