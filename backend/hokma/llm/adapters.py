"""Command line adapters of the agent engines (SPEC-017: OA-68).

A `CliSpec` says what one agent CLI can do (resume the last session, resume by id, run several in one folder) and how to
build the command of an unattended, one-shot turn: no screen, the answer on standard output. The unattended runner of a desk
uses it to launch the engine the student connected. The table is keyed by the engine kinds of `hokma.engines`; a kind
without a CLI (the direct API) has no spec, because it has nothing to launch.

The prompt is never placed where a leading dash could be read as an option: it follows `--` or is glued to its flag with `=`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from hokma.engines.registry import CLIS


class AdapterError(ValueError):
    """The command cannot be composed (unknown kind, a resume that the CLI cannot do, an empty prompt)."""


@dataclass(frozen=True)
class CliCapabilities:
    parallel_per_cwd: bool = True
    resume_last: bool = False
    resume_by_id: bool = False
    headless: bool = True
    structured_stream: bool = False         # the CLI can print its events as JSON lines


@dataclass(frozen=True)
class HeadlessRequest:
    prompt: str
    model: str | None = None
    resume: str | None = None               # a session id; "last" means the most recent session of the folder
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class CliSpec:
    kind: str
    binary: str
    label: str
    capabilities: CliCapabilities
    compose: Callable[[HeadlessRequest], list[str]]

    def command(self, request: HeadlessRequest, *, binary_path: str | None = None) -> list[str]:
        """The full argv: the CLI binary (or the resolved path of it) followed by its arguments."""
        if not request.prompt or not request.prompt.strip():
            raise AdapterError("the prompt is empty")
        if request.resume and request.resume != "last" and not self.capabilities.resume_by_id:
            raise AdapterError(f"{self.label} cannot resume a session by id")
        if request.resume == "last" and not self.capabilities.resume_last:
            raise AdapterError(f"{self.label} cannot resume the last session")
        return [binary_path or self.binary, *self.compose(request)]


def _claude(request: HeadlessRequest) -> list[str]:
    args = ["-p", "--output-format", "stream-json", "--verbose"]
    if request.model:
        args += ["--model", request.model]
    if request.resume == "last":
        args.append("--continue")
    elif request.resume:
        args += ["--resume", request.resume]
    return [*args, *request.extra_args, "--", request.prompt]


def _codex(request: HeadlessRequest) -> list[str]:
    args = ["exec"]
    if request.resume:
        args.append("resume")
        args.append("--last" if request.resume == "last" else request.resume)
    if request.model:
        args += ["--model", request.model]
    return [*args, *request.extra_args, "--", request.prompt]


def _gemini(request: HeadlessRequest) -> list[str]:
    args: list[str] = []
    if request.model:
        args += ["--model", request.model]
    return [*args, *request.extra_args, f"--prompt={request.prompt}"]


SPECS: dict[str, CliSpec] = {
    "claude_code": CliSpec("claude_code", CLIS["claude_code"], "Claude Code",
                           CliCapabilities(resume_last=True, resume_by_id=True, structured_stream=True), _claude),
    "codex": CliSpec("codex", CLIS["codex"], "Codex", CliCapabilities(resume_last=True, resume_by_id=True), _codex),
    "gemini_cli": CliSpec("gemini_cli", CLIS["gemini_cli"], "Gemini CLI", CliCapabilities(), _gemini),
}


def spec_for(kind: str) -> CliSpec:
    """The spec of an engine kind. Raises AdapterError for a kind that has no CLI."""
    try:
        return SPECS[kind]
    except KeyError:
        raise AdapterError(f"engine {kind!r} has no command line adapter") from None


def compose_headless(kind: str, request: HeadlessRequest, *, binary_path: str | None = None) -> list[str]:
    """The argv of an unattended turn on the engine `kind`."""
    return spec_for(kind).command(request, binary_path=binary_path)


def available(path: str | None = None) -> list[str]:
    """Engine kinds whose CLI is installed (looked up with `hokma.engines.registry.installed_clis`)."""
    from hokma.engines.registry import installed_clis

    return [kind for kind, found in installed_clis(path).items() if found and kind in SPECS]
