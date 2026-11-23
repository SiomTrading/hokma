"""Every row of the validation slice is reachable through the registry."""
from hokma.quant import registry

ROWS = ("SK-113 SK-042 SK-043 SK-044 SK-034 SK-035 SK-036 SK-037 SK-038 SK-039 SK-040 SK-041 SK-045 AH-67 INV-008 CC-04 FF-010 FF-011 "
        "FF-012 FM-01 FM-02 FM-03 FM-04 FM-05 FT-067 CTS-038 FT-084 FT-085 FT-086 FT-088").split()


def test_every_row_has_a_registered_function():
    covered = set()
    for spec in registry.list("validation"):
        covered |= set(spec.piece_ids)
    assert [r for r in ROWS if r not in covered] == []
    assert len(ROWS) == 30


def test_the_public_entry_points_exist():
    from hokma.quant import validation
    for name in ("split", "psr", "dsr", "pbo", "evidence"):
        assert callable(getattr(validation, name))
