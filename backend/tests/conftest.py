"""Tests never probe optional native parts in child processes (task 3288); the probe tests turn it back on."""
import os

os.environ.setdefault("HOKMA_NATIVE_PROBE", "off")
