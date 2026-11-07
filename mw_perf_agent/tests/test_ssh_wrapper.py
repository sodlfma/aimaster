from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph.build import build_graph, memory_checkpointer
from tools.ssh_wrapper import (
    RemoteConfigError,
    build_ssh_argv,
    collect_gc_stats_cmd,
    fetch_from_homes,
    format_ssh_error,
    parse_user_host,
    resolve_remote_path,
    split_wrapper_output,
    validate_host,
)

FAKE_SSH = Path(__file__).resolve().parent / "fixtures" / "fake_sshrun.py"
WRAPPER = f"{sys.executable} {FAKE_SSH} {{host}} cat {{path}}"


def test_resolve_remote_path_appends_standard_relatives():
    assert resolve_remote_path("/sw/tomcat", "shl/tomcat.env") == "/sw/tomcat/shl/tomcat.env"
    assert resolve_remote_path("/sw/apache", "conf/httpd-mpm.conf") == "/sw/apache/conf/httpd-mpm.conf"


def test_resolve_remote_path_keeps_explicit_files():
    assert resolve_remote_path("/opt/was/shl/tomcat.env", "shl/tomcat.env") == "/opt/was/shl/tomcat.env"


def test_validate_host_blocks_injection():
    with pytest.raises(ValueError):
        validate_host("was01; rm -rf /")
    with pytest.raises(ValueError):
        validate_host("")


def test_split_wrapper_output_separator_convention():
    stdout = "=== /sw/tomcat/shl/tomcat.env ===\nJAVA_OPTS=x\n=== /sw/apache/conf/httpd-mpm.conf ===\nMaxRequestWorkers 400\n"
    parts = split_wrapper_output(stdout)
    assert parts["/sw/tomcat/shl/tomcat.env"].strip() == "JAVA_OPTS=x"
    assert "MaxRequestWorkers 400" in parts["/sw/apache/conf/httpd-mpm.conf"]


def test_fetch_from_homes_via_fake_sshrun():
    configs = fetch_from_homes(
        "was01.corp.local",
        "/sw/apache",
        "/sw/tomcat",
        ssh_wrapper_cmd=WRAPPER,
        timeout=10,
    )
    assert "maxThreads=200" in configs.files[configs.tomcat_remote]
    assert "MaxRequestWorkers" in configs.files[configs.apache_remote]
    raw = configs.to_raw_inputs()
    assert "HOST=was01.corp.local" in raw["web_mon_cron.env"]
    assert "maxThreads=200" in raw["tomcat.env"]


def test_fetch_from_homes_reports_wrapper_failure():
    with pytest.raises(RemoteConfigError) as exc:
        fetch_from_homes(
            "fail.example",
            "/sw/apache",
            "/sw/tomcat",
            ssh_wrapper_cmd=WRAPPER,
            timeout=10,
        )
    assert "exit 1" in str(exc.value) or "Permission denied" in str(exc.value)
    assert exc.value.configs is not None
    assert exc.value.configs.errors


def test_ssh_homes_feed_sc001_diagnosis(isolated_settings, no_llm):
    configs = fetch_from_homes(
        "was01.corp.local",
        "/sw/apache",
        "/sw/tomcat",
        ssh_wrapper_cmd=WRAPPER,
        timeout=10,
    )
    graph = build_graph(memory_checkpointer())
    cfg = {"configurable": {"thread_id": "t-ssh"}}
    snap = None
    for _mode, _chunk in graph.stream(
        {"run_id": "ssh", "raw_inputs": configs.to_raw_inputs(), "llm_enabled": False, "options": {"dry_run": True}},
        cfg,
        stream_mode=["updates", "custom"],
    ):
        snap = graph.get_state(cfg)
    assert snap is not None
    v = snap.values
    assert v["thresholds"].t_db_lock == 50
    assert v["thresholds"].t_queue == 200
    assert v["thresholds"].t_drop == 300
    assert [s.target_concurrency for s in v["load_plan"].steps] == [40, 55, 250, 360]
    assert v["target_info"].host == "was01.corp.local"


def test_builtin_ssh_argv_includes_account_and_accept_new(isolated_settings, tmp_path):
    from config.settings import update_settings

    key = tmp_path / "id_ed25519"
    key.write_text("dummy-key\n", encoding="utf-8")
    key.chmod(0o600)
    update_settings(
        ssh_wrapper_cmd="",
        ssh_user="ec2-user",
        ssh_port=2222,
        ssh_identity_file=str(key),
        ssh_strict_host_key="accept-new",
    )
    cmd = build_ssh_argv("was01.corp.local", "/sw/tomcat/shl/tomcat.env")
    assert cmd[0] == "ssh"
    assert "BatchMode=yes" in cmd
    assert "StrictHostKeyChecking=accept-new" in cmd
    assert cmd[cmd.index("-p") + 1] == "2222"
    assert cmd[cmd.index("-i") + 1] == str(key)
    assert "IdentitiesOnly=yes" in cmd
    assert cmd[cmd.index("-l") + 1] == "ec2-user"
    assert cmd[-3:] == ["was01.corp.local", "cat", "/sw/tomcat/shl/tomcat.env"]


def test_parse_user_at_host(isolated_settings):
    from config.settings import update_settings

    update_settings(ssh_user="")
    host, user = parse_user_host("ec2-user@was01.corp.local")
    assert host == "was01.corp.local"
    assert user == "ec2-user"


def test_explicit_user_wins_over_inline(isolated_settings):
    host, user = parse_user_host("inline@was01.corp.local", user="explicit")
    assert host == "was01.corp.local"
    assert user == "explicit"


def test_user_at_host_reaches_ssh_argv(isolated_settings):
    from config.settings import update_settings

    update_settings(ssh_wrapper_cmd="", ssh_user="")
    cmd = build_ssh_argv("ec2-user@was01.corp.local", "/sw/tomcat/shl/tomcat.env")
    assert cmd[cmd.index("-l") + 1] == "ec2-user"
    assert "was01.corp.local" in cmd
    assert "ec2-user@was01.corp.local" not in cmd


def test_format_ssh_error_host_key():
    msg = format_ssh_error(255, "Host key verification failed.\n")
    assert "exit 255" in msg
    assert "accept-new" in msg
    assert "known_hosts" in msg


def test_format_ssh_error_permission_denied():
    msg = format_ssh_error(255, "Permission denied (password).")
    assert "개인키" in msg or "인증" in msg


def test_format_ssh_error_pubkey_only_server(isolated_settings):
    msg = format_ssh_error(
        255,
        "webwas@3.35.11.139: Permission denied (publickey,gssapi-keyex,gssapi-with-mic).\n",
    )
    assert "비밀번호 SSH를 허용하지 않습니다" in msg
    assert "개인키" in msg
    assert "publickey" in msg


def test_missing_identity_file_raises(isolated_settings, tmp_path):
    from config.settings import update_settings

    update_settings(ssh_wrapper_cmd="", ssh_identity_file=str(tmp_path / "no-such-key"))
    with pytest.raises(ValueError, match="개인키"):
        build_ssh_argv("was01.corp.local", "/sw/tomcat/shl/tomcat.env")


def test_fetch_records_host_key_hint(isolated_settings, monkeypatch):
    from config.settings import update_settings

    update_settings(ssh_wrapper_cmd="")

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "ssh"
        assert "StrictHostKeyChecking=accept-new" in cmd
        return SimpleNamespace(returncode=255, stdout="", stderr="Host key verification failed.\r\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RemoteConfigError) as exc:
        fetch_from_homes("was01.corp.local", "/sw/apache", "/sw/tomcat", ssh_wrapper_cmd="", timeout=5)
    assert "Host key verification failed" in str(exc.value)
    assert "accept-new" in str(exc.value)
    assert exc.value.configs is not None
    assert exc.value.configs.errors


def test_collect_gc_stats_cmd_builtin(isolated_settings):
    from config.settings import update_settings

    update_settings(ssh_wrapper_cmd="", ssh_user="wasadm")
    cmd = collect_gc_stats_cmd("was01.corp.local", 1234, 5000, 10)
    assert cmd[-6:] == ["was01.corp.local", "jstat", "-gcutil", "1234", "5000", "10"]
    assert cmd[cmd.index("-l") + 1] == "wasadm"


def test_password_disables_batchmode_and_stays_off_argv(isolated_settings):
    from config.settings import get_settings, update_settings
    from tools.ssh_wrapper import ssh_exec_env

    update_settings(ssh_wrapper_cmd="", ssh_user="wasadm", ssh_password="s3cret!")
    cmd = build_ssh_argv("was01.corp.local", "/sw/tomcat/shl/tomcat.env")
    joined = " ".join(cmd)
    assert "BatchMode=yes" not in cmd
    assert "PreferredAuthentications=publickey,password,keyboard-interactive" in cmd
    assert "PubkeyAuthentication=no" not in cmd
    assert "s3cret!" not in joined
    env = ssh_exec_env()
    assert env["MW_SSH_PASSWORD"] == "s3cret!"
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    assert Path(env["SSH_ASKPASS"]).is_file()
    assert get_settings().as_dict()["ssh_password"] == "***"


def test_password_with_identity_keeps_pubkey_then_password(isolated_settings, tmp_path):
    from config.settings import update_settings

    key = tmp_path / "id_ed25519"
    key.write_text("dummy-key\n", encoding="utf-8")
    update_settings(
        ssh_wrapper_cmd="",
        ssh_identity_file=str(key),
        ssh_password="s3cret!",
    )
    cmd = build_ssh_argv("was01.corp.local", "/sw/tomcat/shl/tomcat.env")
    assert "PreferredAuthentications=publickey,password,keyboard-interactive" in cmd
    assert "PubkeyAuthentication=no" not in cmd
    assert cmd[cmd.index("-i") + 1] == str(key)


def test_fetch_passes_askpass_env(isolated_settings, monkeypatch):
    from config.settings import update_settings

    update_settings(ssh_wrapper_cmd="", ssh_user="wasadm", ssh_password="s3cret!")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        captured["start_new_session"] = kwargs.get("start_new_session")
        return SimpleNamespace(returncode=255, stdout="", stderr="Permission denied (password).")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RemoteConfigError) as exc:
        fetch_from_homes("was01.corp.local", "/sw/apache", "/sw/tomcat", ssh_wrapper_cmd="", timeout=5)
    assert captured["env"]["MW_SSH_PASSWORD"] == "s3cret!"
    assert "s3cret!" not in " ".join(captured["cmd"])
    assert captured["start_new_session"] is True
    assert "ASKPASS" in str(exc.value) or "비밀번호" in str(exc.value)


def test_ssh_askpass_prints_password_only(monkeypatch):
    from tools.ssh_askpass import main

    monkeypatch.setenv("MW_SSH_PASSWORD", "s3cret!")
    from io import StringIO

    buf = StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    assert main(["ssh_askpass.py", "Password:"]) == 0
    assert buf.getvalue() == "s3cret!"
    assert main(["ssh_askpass.py", "Are you sure you want to continue connecting (yes/no)?"]) == 1
