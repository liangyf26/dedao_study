from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ScriptTests(unittest.TestCase):
    def test_run_script_records_scheduler_transcript(self):
        text = (ROOT / "scripts" / "run_dedao_sync.ps1").read_text(encoding="utf-8")

        self.assertIn("[CmdletBinding(PositionalBinding = $false)]", text)
        self.assertIn("[string]$ProjectRoot = \"\"", text)
        self.assertIn("$ProjectRoot = Join-Path $PSScriptRoot \"..\"", text)
        self.assertIn("Start-Transcript", text)
        self.assertIn("scheduled-{0}.log", text)
        self.assertIn("[ValidateSet(\"sync\", \"check\", \"retry-failed\", \"resummarize\")]", text)
        self.assertIn("[Parameter(ValueFromRemainingArguments = $true)]", text)
        self.assertIn("[string[]]$ExtraArgs = @()", text)
        self.assertIn("$Args = @($Command, \"--config\", $ConfigPath) + $ExtraArgs", text)
        self.assertIn("$Args = @(\"-m\", \"dedao_sync.cli\", $Command, \"--config\", $ConfigPath) + $ExtraArgs", text)
        self.assertIn("ExtraArgs:", text)

    def test_register_task_passes_config_to_run_script(self):
        text = (ROOT / "scripts" / "register_windows_task.ps1").read_text(encoding="utf-8")

        self.assertIn("[CmdletBinding(PositionalBinding = $false)]", text)
        self.assertIn("[string]$ProjectRoot = \"\"", text)
        self.assertIn("$ProjectRoot = Join-Path $PSScriptRoot \"..\"", text)
        self.assertIn("$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path", text)
        self.assertIn("$ScriptPath = (Resolve-Path -LiteralPath $ScriptPath).Path", text)
        self.assertIn("[string]$ConfigPath = \"config.yaml\"", text)
        self.assertIn("[string]$Command = \"sync\"", text)
        self.assertIn("[Parameter(ValueFromRemainingArguments = $true)]", text)
        self.assertIn("[string[]]$ExtraArgs = @()", text)
        self.assertIn("-ConfigPath `\"$ConfigPath`\"", text)
        self.assertIn("-Command `\"$Command`\"", text)
        self.assertIn("$ExtraArgsSwitch = if ($ExtraArgsArgument) { \" $ExtraArgsArgument\" } else { \"\" }", text)
        self.assertIn("New-ScheduledTaskTrigger -Daily", text)
        self.assertIn("-MultipleInstances IgnoreNew", text)

    def test_systemd_templates_define_user_timer(self):
        service = (ROOT / "templates" / "systemd" / "dedao-sync.service").read_text(encoding="utf-8")
        timer = (ROOT / "templates" / "systemd" / "dedao-sync.timer").read_text(encoding="utf-8")

        self.assertIn("Type=oneshot", service)
        self.assertIn("WorkingDirectory=%h/dedao-sync", service)
        self.assertIn("EnvironmentFile=%h/dedao-sync/.env", service)
        self.assertIn("ExecStart=%h/dedao-sync/.venv/bin/dedao-sync sync --config %h/dedao-sync/config.yaml", service)
        self.assertIn("OnCalendar=*-*-* 08:00:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_bootstrap_script_resolves_default_project_root_in_body(self):
        text = (ROOT / "scripts" / "bootstrap_windows.ps1").read_text(encoding="utf-8")

        self.assertIn("[CmdletBinding(PositionalBinding = $false)]", text)
        self.assertIn("[string]$ProjectRoot = \"\"", text)
        self.assertIn("$ProjectRoot = Join-Path $PSScriptRoot \"..\"", text)
        self.assertIn("$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path", text)

    def test_debian_deploy_doc_links_templates_and_gates_migration(self):
        text = (ROOT / "docs" / "DEBIAN_DEPLOY.md").read_text(encoding="utf-8")

        self.assertIn("templates/systemd/dedao-sync.service", text)
        self.assertIn("systemctl --user enable --now dedao-sync.timer", text)
        self.assertIn("Windows MVP 已连续稳定运行 7 天", text)
        self.assertIn("loginctl enable-linger", text)


if __name__ == "__main__":
    unittest.main()
