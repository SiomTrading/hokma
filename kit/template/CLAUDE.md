@HOKMA.md

## Deleting

You never delete files or folders. When something is no longer needed, move it to the Hokma trash with the
`trash_move` tool (`paths`, `reason`). The owner can restore it from Settings > Trash, and only the owner empties
the trash.

Hokma refuses, in every mode, any command or tool call that deletes, empties a file with content, forces or deletes
remote Git history, writes outside the working folder or touches disks, reads stored keys or Hokma's own files, or
places a real-money order. When a call is refused, take another way; do not try to get around it.
