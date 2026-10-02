"""Enforce the rule that `firmware/` is only what the robot runs.

    python -m tools.check_layout

Two things go wrong on their own over time: a development module drifts into
`firmware/`, and something in `firmware/` grows an import of `tools/` or of a
path inside `docs/`. Either one means the code that works on a laptop stops
working once it is copied onto an SD card, and the way that shows up is a
device that boots to a black screen.

Run it before a release. It reads the source and does not import the firmware,
except the loader of the assistant's data tables, which is standard library only.
"""

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMWARE = os.path.join(ROOT, "firmware")

# Modules the robot may not have. Anything in firmware/ that imports one of
# these has to do it inside a function, guarded, or not at all.
DEV_ONLY = {"tools", "tkinter", "PIL.ImageTk", "matplotlib", "pytest"}

# Third-party packages firmware/ may import at module level. Pillow is the
# renderer; everything else has to be imported late so a broken virtualenv
# still boots a face.
RUNTIME_OK = {"PIL"}

STDLIB = set(sys.stdlib_module_names)


def _guarded(node):
    """Does this try/except catch an import failing and carry on?"""
    for handler in node.handlers:
        names = []
        if isinstance(handler.type, ast.Name):
            names = [handler.type.id]
        elif isinstance(handler.type, ast.Tuple):
            names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
        if {"ImportError", "ModuleNotFoundError", "Exception"} & set(names):
            return True
    return False


def _imports(path):
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)

    # Imports inside `try: ... except ImportError:` are the pattern this rule
    # is asking for, wherever they sit, so collect their lines and skip them.
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and _guarded(node):
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    guarded.add(inner.lineno)

    # Module level only, plus whatever a top-level if/try wraps. An import
    # inside a function is the documented way to reach something optional, so
    # it is not a finding.
    top = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            top |= {(a.name, node.lineno) for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            top.add((node.module or "", node.lineno))
        elif isinstance(node, (ast.If, ast.Try)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Import):
                    top |= {(a.name, inner.lineno) for a in inner.names}
                elif isinstance(inner, ast.ImportFrom) and inner.level == 0:
                    top.add((inner.module or "", inner.lineno))
    return sorted((name, line) for name, line in top if line not in guarded)


MIGRATIONS = os.path.join(FIRMWARE, "storage", "migrations")

_DESTRUCTIVE = re.compile(r"\b(DROP|RENAME)\b", re.IGNORECASE)


def check_migrations(directory=MIGRATIONS):
    """Migrations only add. A release that is rolled back has to open a
    database a newer release already migrated (plan 06), and that stops being
    true the day a migration drops or renames something. Also numbered without
    gaps, so a missing file is noticed here rather than as a device that skips
    a step."""
    problems = []
    if not os.path.isdir(directory):
        return problems
    versions = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".sql"):
            continue
        match = re.match(r"^(\d+)_", name)
        if not match:
            problems.append("firmware/storage/migrations/%s is not numbered "
                            "like 001_name.sql" % name)
            continue
        versions.append(int(match.group(1)))
        with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
            text = fh.read()
        # Words in comments are not statements.
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
        found = _DESTRUCTIVE.search(text)
        if found:
            problems.append(
                "firmware/storage/migrations/%s contains %s; migrations only "
                "add, or a rolled-back release cannot open the database"
                % (name, found.group(1).upper()))
    if versions != list(range(1, len(versions) + 1)):
        problems.append("firmware/storage/migrations is not numbered 1, 2, 3 "
                        "without gaps: %s" % versions)
    return problems


def check_assistant_tables():
    """The provider and price tables ship with the firmware and are read at
    boot. A typo in one is a device that cannot reach its model, found by the
    owner, so the same checks the loader makes run here and fail the build
    (plan 04). Imports the loader, which is standard library only."""
    from firmware.assistant import table

    problems = []
    try:
        providers = table.load_providers()
        prices = table.load_prices()
    except table.TableError as exc:
        return ["firmware/assistant: %s" % problem
                for problem in str(exc).split("; ")]
    problems += ["firmware/assistant: %s" % p
                 for p in table.cross_check(providers, prices)]
    return problems


def check():
    problems = []

    for folder, _, files in os.walk(FIRMWARE):
        if "__pycache__" in folder:
            continue
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")

            for module, line in _imports(path):
                root = module.split(".")[0]
                if module in DEV_ONLY or root in DEV_ONLY:
                    problems.append(
                        "%s:%d imports %s, which the robot does not have"
                        % (rel, line, module))
                elif root and root not in STDLIB and root != "firmware" \
                        and root not in RUNTIME_OK:
                    problems.append(
                        "%s:%d imports %s at module level; import it inside "
                        "the function that needs it so a missing package "
                        "cannot stop the device booting" % (rel, line, module))

            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
            if '"docs/' in source or "'docs/" in source or '"../../docs' in source:
                problems.append(
                    "%s reads a path inside docs/, which is not installed on "
                    "the robot" % rel)

    problems += check_migrations()
    problems += check_assistant_tables()

    baked = os.path.join(FIRMWARE, "face", "data", "faces.json")
    if not os.path.exists(baked):
        problems.append(
            "firmware/face/data/faces.json is missing; run "
            "python -m tools.face.bake")

    return problems


def main(argv=None):
    problems = check()
    for line in problems:
        print(line)
    if problems:
        print("\n%d problem(s): firmware/ must run on a robot with no docs/, "
              "no tools/ and no display server." % len(problems))
        return 1
    print("firmware/ is clean: nothing in it needs docs/, tools/, or a "
          "desktop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
