# Skipped source files

Every source strategy file that was reviewed for this library has a rewritten rule in this folder. A file that cannot be rewritten gets one line here in the form (a list item, the file stem, a colon, the reason):

    - source-file-stem: the exact reason

No file was skipped, so there are no such lines. The test backend/tests/factory/test_library_b.py counts the YAML files of this folder plus the lines above and fails when the total is not the number of source files (61).
