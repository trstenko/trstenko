"""Reading and checking a `.trsttool` file.

A `.trsttool` is a zip of `tool.json` (what it is and what it asks for),
`tool.py` (the code) and optionally `tests.py` and `README.md`. Nothing in it
is run here. Everything the owner is shown before agreeing, and everything the
model is told about the tool, comes from `tool.json`; `tool.py` is parsed to
see that the functions it promises are there, and that is all.

`PackageError` is written for the person installing the file.
"""

import ast
import io
import ipaddress
import json
import os
import re
import zipfile

FORMAT = 1
EXTENSION = ".trsttool"
MEMBERS = ("tool.json", "tool.py", "tests.py", "README.md")
INSTALLED = ("tool.json", "tool.py")
MAX_BYTES = 256 * 1024
MAX_TOOLS = 2
MAX_HOSTS = 5
MAX_DESCRIPTION = 200

_ID = re.compile(r"^[a-z0-9]+(?:[-.][a-z0-9]+)*$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_TOOL_NAME = re.compile(r"^[a-z]+_[a-z_]+$")
_FUNCTION = re.compile(r"^[a-z][a-z0-9_]*$")
_HOST = re.compile(r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                   r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_LOCAL_ENDINGS = (".local", ".lan", ".internal", ".home.arpa", ".localhost",
                  ".test", ".invalid")
PARAM_TYPES = ("string", "integer", "number", "boolean")
PARAM_KEYS = {"type", "description", "enum", "optional", "min", "max",
              "max_length"}
PERMISSIONS = {"internet", "storage"}


class PackageError(Exception):
    pass


class Package:
    def __init__(self, manifest, files):
        self.manifest = manifest
        self.files = files                  # {"tool.py": bytes, ...}
        self.id = manifest["id"]
        self.name = manifest["name"]
        self.version = manifest["version"]
        self.hosts = tuple(manifest["permissions"].get("internet") or ())
        self.storage = bool(manifest["permissions"].get("storage"))
        self.tools = manifest["tools"]


def _text(manifest, key, limit, required=True):
    value = manifest.get(key, "")
    if not isinstance(value, str):
        raise PackageError("%s must be text" % key)
    value = value.strip()
    if required and not value:
        raise PackageError("tool.json needs \"%s\"" % key)
    if len(value) > limit:
        raise PackageError("%s is longer than %d characters" % (key, limit))
    return value


def check_host(host):
    """A public host name, lower case, or PackageError. No addresses and no
    names that only mean something on the owner's own network: a tool that
    could ask for `192.168.1.1` could talk to the router."""
    if not isinstance(host, str):
        raise PackageError("a host must be text")
    host = host.strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PackageError("%s is an address; a tool lists host names" % host)
    if not _HOST.match(host) or host == "localhost" \
            or host.endswith(_LOCAL_ENDINGS) \
            or host.rsplit(".", 1)[-1].isdigit():
        raise PackageError("%s is not a public host name" % host)
    return host


def check_manifest(manifest, reserved=()):
    """The manifest, tidied, or PackageError saying what to fix."""
    if not isinstance(manifest, dict):
        raise PackageError("tool.json must be an object")
    if manifest.get("format") != FORMAT:
        raise PackageError("this file is format %r; this device reads "
                           "format %d" % (manifest.get("format"), FORMAT))
    out = {"format": FORMAT}
    out["id"] = _text(manifest, "id", 40)
    if not _ID.match(out["id"]):
        raise PackageError("id may hold a-z, 0-9, dots and dashes")
    out["name"] = _text(manifest, "name", 40)
    out["version"] = _text(manifest, "version", 20)
    if not _VERSION.match(out["version"]):
        raise PackageError("version must look like 1.0.0")
    out["author"] = _text(manifest, "author", 60, required=False)
    out["about"] = _text(manifest, "about", 300, required=False)

    asked = manifest.get("permissions", {})
    if not isinstance(asked, dict) or set(asked) - PERMISSIONS:
        raise PackageError("permissions may hold \"internet\" and "
                           "\"storage\" only")
    hosts = asked.get("internet") or []
    if not isinstance(hosts, list) or len(hosts) > MAX_HOSTS:
        raise PackageError("internet must be a list of at most %d hosts"
                           % MAX_HOSTS)
    out["permissions"] = {"internet": [check_host(h) for h in hosts],
                          "storage": asked.get("storage") is True}

    tools = manifest.get("tools")
    if not isinstance(tools, list) or not 1 <= len(tools) <= MAX_TOOLS:
        raise PackageError("tools must list 1 to %d tools" % MAX_TOOLS)
    out["tools"] = [_check_tool(t, reserved) for t in tools]
    names = [t["name"] for t in out["tools"]]
    if len(set(names)) != len(names):
        raise PackageError("two tools have the same name")
    return out


def _check_tool(entry, reserved):
    if not isinstance(entry, dict):
        raise PackageError("each tool must be an object")
    name = entry.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.match(name) \
            or len(name) > 40:
        raise PackageError("a tool's name must be verb_noun in lower case, "
                           "like get_weather")
    if name in reserved:
        raise PackageError("%s is the name of a built-in tool" % name)
    description = entry.get("description")
    if not isinstance(description, str) or not description.strip():
        raise PackageError("%s needs a description: it is what the model "
                           "reads to decide when to use it" % name)
    description = " ".join(description.split())
    if len(description) > MAX_DESCRIPTION:
        raise PackageError("%s: the description is %d characters, at most %d"
                           % (name, len(description), MAX_DESCRIPTION))
    function = entry.get("function", name)
    if not isinstance(function, str) or not _FUNCTION.match(function):
        raise PackageError("%s: function must be the name of a function in "
                           "tool.py" % name)
    params = entry.get("params") or {}
    if not isinstance(params, dict) or len(params) > 6:
        raise PackageError("%s: params must be an object of at most 6 "
                           "arguments" % name)
    clean = {}
    for key, p in params.items():
        if not isinstance(key, str) or not _FUNCTION.match(key) \
                or key in ("ctx", "confirmed"):
            raise PackageError("%s: %r is not a usable argument name"
                               % (name, key))
        if not isinstance(p, dict) or set(p) - PARAM_KEYS:
            raise PackageError("%s.%s may hold %s" % (
                name, key, ", ".join(sorted(PARAM_KEYS))))
        if p.get("type") not in PARAM_TYPES:
            raise PackageError("%s.%s: type must be one of %s" % (
                name, key, ", ".join(PARAM_TYPES)))
        if not isinstance(p.get("description"), str) \
                or not p["description"].strip() \
                or len(p["description"]) > 120:
            raise PackageError("%s.%s needs a description of at most 120 "
                               "characters" % (name, key))
        if "enum" in p and (not isinstance(p["enum"], list) or not p["enum"]
                            or len(p["enum"]) > 12
                            or not all(isinstance(v, str) and len(v) <= 30
                                       for v in p["enum"])):
            raise PackageError("%s.%s: enum must be a short list of text "
                               "values" % (name, key))
        clean[key] = dict(p)
    return {"name": name, "description": description, "function": function,
            "params": clean}


def check_code(source, tools):
    """`tool.py` parses and defines each promised function at the top level,
    taking `ctx` and the declared arguments. Parsing runs nothing."""
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text, "tool.py")
    except UnicodeDecodeError:
        raise PackageError("tool.py is not UTF-8 text")
    except SyntaxError as exc:
        raise PackageError("tool.py line %s: %s" % (exc.lineno, exc.msg))
    defined = {node.name: node for node in tree.body
               if isinstance(node, ast.FunctionDef)}
    for tool in tools:
        node = defined.get(tool["function"])
        if node is None:
            raise PackageError("tool.py has no function called %s"
                               % tool["function"])
        args = [a.arg for a in node.args.args + node.args.kwonlyargs]
        if not args or args[0] != "ctx":
            raise PackageError("%s must take ctx as its first argument"
                               % tool["function"])
        missing = [p for p in tool["params"] if p not in args[1:]]
        if missing and node.args.kwarg is None:
            raise PackageError("%s does not take the argument %s that "
                               "tool.json declares" % (tool["function"],
                                                       missing[0]))


def read(data, reserved=()):
    """A `Package` from the bytes of a `.trsttool`, or PackageError."""
    if len(data) > MAX_BYTES:
        raise PackageError("the file is larger than %d KB"
                           % (MAX_BYTES // 1024))
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PackageError("this is not a .trsttool file")
    files, total = {}, 0
    with archive:
        for info in archive.infolist():
            if info.filename not in MEMBERS:
                raise PackageError("unexpected file in the package: %s"
                                   % info.filename[:60])
            if info.filename in files:
                raise PackageError("%s is in the package twice"
                                   % info.filename)
            total += info.file_size
            if total > MAX_BYTES:
                raise PackageError("the package unpacks to more than %d KB"
                                   % (MAX_BYTES // 1024))
            with archive.open(info) as fh:
                content = fh.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise PackageError("the package unpacks to more than %d KB"
                                   % (MAX_BYTES // 1024))
            files[info.filename] = content
    for needed in INSTALLED:
        if needed not in files:
            raise PackageError("the package has no %s" % needed)
    try:
        manifest = json.loads(files["tool.json"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PackageError("tool.json is not valid JSON")
    manifest = check_manifest(manifest, reserved)
    check_code(files["tool.py"], manifest["tools"])
    return Package(manifest, files)


def pack(manifest, files):
    """The bytes of a `.trsttool`. `files` maps member names to bytes; the
    manifest is written from `manifest`, tidied. Checked the same way a device
    would on the way out, so an export that would be refused is refused
    here."""
    manifest = check_manifest(manifest)
    members = {"tool.json": json.dumps(manifest, indent=2,
                                       ensure_ascii=False).encode("utf-8")}
    for name, content in files.items():
        if name not in MEMBERS or name == "tool.json":
            raise PackageError("%s does not belong in a package" % name)
        members[name] = content
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in MEMBERS:
            if name in members:
                # A fixed date, so the same tool always packs to the same
                # bytes.
                info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, members[name])
    data = out.getvalue()
    read(data)
    return data


def unpack(package, folder):
    """Write what a device keeps of a package: the manifest and the code."""
    os.makedirs(folder, exist_ok=True)
    for name in INSTALLED:
        content = (json.dumps(package.manifest, indent=2,
                              ensure_ascii=False).encode("utf-8")
                   if name == "tool.json" else package.files[name])
        tmp = os.path.join(folder, name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(content)
        os.replace(tmp, os.path.join(folder, name))


def load(folder, reserved=()):
    """The package unpacked in `folder`, checked again: the card may have been
    edited since it was installed."""
    files = {}
    for name in INSTALLED:
        try:
            with open(os.path.join(folder, name), "rb") as fh:
                files[name] = fh.read(MAX_BYTES + 1)
        except OSError:
            raise PackageError("%s is missing" % name)
        if len(files[name]) > MAX_BYTES:
            raise PackageError("%s is too large" % name)
    try:
        manifest = json.loads(files["tool.json"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PackageError("tool.json is not valid JSON")
    manifest = check_manifest(manifest, reserved)
    check_code(files["tool.py"], manifest["tools"])
    return Package(manifest, files)
