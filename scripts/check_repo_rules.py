"""Repository rules, run in CI and by the tests:  python scripts/check_repo_rules.py

1. No reference to where code or ideas came from: no external repository or author names, no phrases that
   announce an origin. The forbidden names and phrases are stored as sha256 hashes of their normalised form
   (lowercase words), so this file does not contain them. Words are compared as 1 to 4 word sequences, across
   line breaks, in file contents AND in file paths.
2. No handwritten Spanish. Code, comments, docs, folder names and log lines are English; what the student reads
   lives in the message catalogs (SPANISH_ALLOWED). Detected by accented letters, inverted punctuation and a
   list of common Spanish words.

3. No internal developer notes in the repository: a `dev-notes/` folder fails the check, because the release
   package is a `git archive` of the tree and would publish it.

Exit code 1 and one line per violation (path:line: rule: text) when something is found.
"""
import hashlib
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_HASHES = {
    "8cfde6efdfc4ed5ab1f6acbbd1ba49bf31932f84d0a4c090eb41c7d151e8b180",
    "60dc2d5aede9c753d57d9fd5af8fd6b8767a28f4eebefa45d7e744537031fabb",
    "3eee4f30151db45a6507b8fde7c56d0f345e141fa35d5aa0998dfebc6f2d66fb",
    "149a60264d5b2aa35b5e68a6a804564773e1a5351a798192b95db7af611ee984",
    "7188a6f2799a013e73e2149570019b19185a6345243dfd6023530e5ae87148c3",
    "a636d7121733e52bf9e8b8a80045c183cf9dad0b4bfd8c859ddbf4b27314274a",
    "5292b23a17cdde67a75b5b2acbb3f75a21117e131a5de99ab9cbc1b307f4da76",
    "a33697267e0d289794cacf4677a89c069936d6ca302145c7d67f06c028fca167",
    "b225390a8984c8de4206c746772ab5ef2abb0a02bcb043da10f6d41c274d1a02",
    "d19424b31a4f8b6bcbdbdc78bddc298a0b82dc77ead01af376953960e7372bb0",
    "9ac027634f10a5fe738e0c2397c976386a565f229bd8323a49214ca66d6db441",
    "556d1f14c80f008eb61334df7417e0adb53464a22552ce44913c435fd39f3fe5",
    "4d404a612057588d85352f0d4b8185cd9b3c617ab29bd22e6059e3a7b8f93ed3",
    "73669dc190274e81e5c206ff7fc18a95e410279a8196f9444671788e33060e36",
    "6c6a1de7abaa2e7ef7ac80c58fe9df9e4d54acfc6b9626b1e294cbfe47210236",
    "9c818179f6e7c32e3059e827fc2bdd521ee00670593abb1879d30dd71709a695",
    "70f427fc0619e543dce66011fd374852735fc48a6e9303d99965a50f9da8fca7",
    "b9feb93c33722d4c8b76f9967e6ab633761786e4d96ebb8ead98af2627697b3d",
}
MAX_PHRASE_WORDS = 4

# Message catalogs (what the student reads) and this checker with its test may contain Spanish.
SPANISH_ALLOWED = (
    "backend/hokma/catalog/",
    "installer/messages/",
    "installer/Instalar-Hokma.cmd",
    "dashboard/src/i18n/",
    "kit/template/docs/learn/es/",
    "scripts/check_repo_rules.py",
    "backend/tests/test_repo_rules.py",
)
# Not authored text: generated or third-party content.
# The only place where names of third-party authors may appear: the license notices required by those licenses.
ORIGIN_EXEMPT = ("legal/",)
SKIPPED = ("dashboard/dist/", "uv.lock", "dashboard/package-lock.json")
BINARY_SUFFIXES = {".svg", ".png", ".jpg", ".ico", ".woff", ".woff2", ".zip", ".gz"}

ACCENTED = re.compile("[áéíóúñüÁÉÍÓÚÑ¿¡]")
SPANISH_WORDS = {
    "carpeta", "carpetas", "archivo", "archivos", "alumno", "alumnos", "instalador", "instalacion", "actualizacion",
    "configuracion", "solicitud", "solicitudes", "aprobar", "ejecutar", "ejecuta", "vuelve", "repite", "falta",
    "aviso", "rechazada", "pendiente", "pendientes", "usuario", "contrasena", "motor", "llave", "ruta", "rutas",
    "enlace", "enlaces", "sesion", "borrar", "acepto", "cuando", "tambien", "tienes", "puede", "porque",
}


def normalise(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def words_with_lines(text: str) -> list[tuple[str, int]]:
    out = []
    for number, line in enumerate(text.split("\n"), 1):
        out += [(w, number) for w in re.findall(r"[a-z0-9]+", normalise(line))]
    return out


def origin_hits(text: str) -> list[tuple[int, str]]:
    words = words_with_lines(text)
    hits = []
    for i in range(len(words)):
        for n in range(1, MAX_PHRASE_WORDS + 1):
            if i + n > len(words):
                break
            phrase = " ".join(w for w, _ in words[i:i + n])
            if hashlib.sha256(phrase.encode()).hexdigest() in FORBIDDEN_HASHES:
                hits.append((words[i][1], phrase))
    return hits


def spanish_hits(text: str) -> list[tuple[int, str]]:
    hits = []
    for number, line in enumerate(text.split("\n"), 1):
        found = ACCENTED.search(line)
        if found:
            hits.append((number, f"accented or inverted character {found.group()!r}"))
            continue
        for word in re.findall(r"[a-z]+", normalise(line)):
            if word in SPANISH_WORDS:
                hits.append((number, f"Spanish word {word!r}"))
                break
    return hits


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def check(root: Path, files: list[str] | None = None) -> list[str]:
    problems: list[str] = []
    if (root / "dev-notes").exists():
        problems.append("dev-notes/:0: internal notes must live outside the repository (the release archive would publish them)")
    for rel in files if files is not None else tracked_files(root):
        if not (root / rel).exists() or rel.startswith(SKIPPED) or Path(rel).suffix in BINARY_SUFFIXES:
            continue
        allowed_spanish = rel.startswith(SPANISH_ALLOWED)
        exempt = rel.startswith(ORIGIN_EXEMPT)
        for line, what in ([] if exempt else origin_hits(rel.replace("/", " ").replace(".", " "))):
            problems.append(f"{rel}:0: origin reference in the path: {what}")
        if not allowed_spanish:
            for line, what in spanish_hits(rel.replace("/", " ").replace("-", " ").replace("_", " ")):
                problems.append(f"{rel}:0: Spanish in the path: {what}")
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line, what in ([] if exempt else origin_hits(text)):
            problems.append(f"{rel}:{line}: origin reference: {what}")
        if not allowed_spanish:
            for line, what in spanish_hits(text):
                problems.append(f"{rel}:{line}: handwritten Spanish: {what}")
    return problems


def main() -> int:
    problems = check(ROOT)
    for p in problems:
        print(p)
    if problems:
        print(f"{len(problems)} problem(s). Origin references and handwritten Spanish are not allowed here.",
              file=sys.stderr)
        return 1
    print("repo rules ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
