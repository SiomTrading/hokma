"""Static checks of the blue-green runtime layout in the installers (Windows part is never run here)."""
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[2] / "installer"
SH = (INSTALLER / "install.sh").read_text()
PS = (INSTALLER / "install.ps1").read_text()


def test_installers_build_in_the_inactive_slot_and_switch_last():
    assert 'SLOT_DIR="$HOKMA_DATA/runtime/$SLOT"' in SH
    assert SH.index("write-manifest") < SH.index("verify-install") < SH.index('mv -f "$HOKMA_DATA/current.tmp"')
    assert SH.index('mv -f "$HOKMA_DATA/current.tmp"') < SH.index(' init-config')
    assert '$SlotDir = Join-Path $HokmaData ("runtime\\" + $Slot)' in PS
    assert PS.index("write-manifest") < PS.index("verify-install") < PS.index("Move-Item -Force -Path $tmpPointer")
    assert PS.index("Move-Item -Force -Path $tmpPointer") < PS.index("init-config")


def test_launchers_read_the_active_slot_and_validate_it():
    assert "runtime/\\$SLOT/venv/bin/hokma" in SH and "a|b) ;;" in SH
    assert "runtime\\%HOKMA_SLOT%\\venv\\Scripts\\hokma.exe" in PS
    assert 'if `"%HOKMA_SLOT%`"==`"a`" goto run' in PS and 'if `"%HOKMA_SLOT%`"==`"b`" goto run' in PS


def test_installers_use_kit_sync_not_a_blind_copy():
    assert "kit-sync" in SH and "kit-sync" in PS
    assert "copy_no_overwrite" not in SH and "Copy-Item $_.FullName $dest" not in PS


def test_the_installed_app_does_not_carry_the_double_click_installer():
    """Bug 1113: Defender quarantines Instalar-Hokma.cmd; the runtime copy (installer and updater) leaves it out."""
    root = Path(__file__).resolve().parents[2]
    assert '"/XF", "Instalar-Hokma.cmd"' in (root / "installer" / "install.ps1").read_text(encoding="utf-8")
    assert 'ignore_patterns("Instalar-Hokma.cmd")' in (root / "backend" / "hokma" / "update" / "runner.py").read_text(encoding="utf-8")
