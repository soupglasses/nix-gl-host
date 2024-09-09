import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from glob import glob

from nixglhost.util import log_info

__all__ = [
    "ResolvedLib",
    "LibraryPath",
    "CacheDirContent",
    "get_ld_paths",
    "resolve_libraries",
    "copy_and_patch_libs",
    "patch_dsos",
    "is_dso_cache_up_to_date",
    "cache_library_path",
    "generate_cache_ld_library_path",
    "generate_cache_metadata",
    "exec_binary",
]

IN_NIX_STORE = False
CACHE_VERSION = 3

if IN_NIX_STORE:
    # The following paths are meant to be substituted by Nix at build
    # time.
    PATCHELF_PATH = "@patchelf-bin@"
else:
    PATCHELF_PATH = "patchelf"


class ResolvedLib:
    """This data type encapsulate one host dynamically shared object
    together with some metadata helping us to uniquely identify it."""

    def __init__(
        self,
        name: str,
        dirpath: str,
        fullpath: str,
        last_modification: float | None = None,
        size: int | None = None,
    ):
        self.name: str = name
        self.dirpath: str = dirpath
        self.fullpath: str = fullpath
        if size is None or last_modification is None:
            stat = os.stat(fullpath)
            self.last_modification: float = stat.st_mtime
            self.size: int = stat.st_size
        else:
            self.last_modification = last_modification
            self.size = size

    def __repr__(self):
        return f"ResolvedLib<{self.name}, {self.dirpath}, {self.fullpath}, {self.last_modification}, {self.size}>"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "dirpath": self.dirpath,
            "fullpath": self.fullpath,
            "last_modification": self.last_modification,
            "size": self.size,
        }

    def __hash__(self):
        return hash(
            (self.name, self.dirpath, self.fullpath, self.last_modification, self.size)
        )

    def __eq__(self, o):
        return (
            self.name == o.name
            and self.fullpath == o.fullpath
            and self.dirpath == o.dirpath
            and self.last_modification == o.last_modification
            and self.size == o.size
        )

    @classmethod
    def from_dict(cls, d: dict):
        return ResolvedLib(
            d["name"], d["dirpath"], d["fullpath"], d["last_modification"], d["size"]
        )


class LibraryPath:
    """This data type encapsulates a directory containing some GL/Cuda
    dynamically shared objects."""

    def __init__(
        self,
        glx: list[ResolvedLib],
        cuda: list[ResolvedLib],
        generic: list[ResolvedLib],
        egl: list[ResolvedLib],
        path: str,
    ):
        self.glx = glx
        self.cuda = cuda
        self.generic = generic
        self.egl = egl
        self.path = path

    def __eq__(self, other):
        return (
            set(self.glx) == set(other.glx)
            and set(self.cuda) == set(other.cuda)
            and set(self.generic) == set(other.generic)
            and set(self.egl) == set(other.egl)
            and self.path == other.path
        )

    def __repr__(self):
        return f"LibraryPath<{self.path}>"

    def __hash__(self):
        return hash(
            (
                tuple(self.glx),
                tuple(self.cuda),
                tuple(self.generic),
                tuple(self.egl),
                self.path,
            )
        )

    def to_dict(self) -> dict:
        return {
            "glx": [v.to_dict() for v in self.glx],
            "cuda": [v.to_dict() for v in self.cuda],
            "generic": [v.to_dict() for v in self.generic],
            "egl": [v.to_dict() for v in self.egl],
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, d: dict):
        return LibraryPath(
            glx=[ResolvedLib.from_dict(v) for v in d["glx"]],
            cuda=[ResolvedLib.from_dict(v) for v in d["cuda"]],
            generic=[ResolvedLib.from_dict(v) for v in d["generic"]],
            egl=[ResolvedLib.from_dict(v) for v in d["egl"]],
            path=d["path"],
        )


class CacheDirContent:
    """This datatype encapsulates all the dynamically shared objects
    living in the nix-gl-host cache. We mostly use it to serialize
    what's in the cache on the disk and compare this content to what
    we scanned in the host system."""

    def __init__(self, paths: list[LibraryPath], version: int = CACHE_VERSION):
        self.paths: list[LibraryPath] = paths
        self.version: int = version

    def to_json(self):
        d = {"paths": [p.to_dict() for p in self.paths], "version": self.version}
        return json.dumps(d, sort_keys=True)

    def __eq__(self, o):
        return self.version == o.version and set(self.paths) == set(o.paths)

    @classmethod
    def from_json(cls, j: str):
        d: dict = json.loads(j)
        return CacheDirContent(
            version=d["version"], paths=[LibraryPath.from_dict(p) for p in d["paths"]]
        )


def get_ld_paths() -> list[str]:
    """
    Vendored from https://github.com/albertz/system-tools/blob/master/bin/find-lib-in-path.py

    Find all the directories pointed by LD_LIBRARY_PATH and the ld cache."""

    def parse_ld_conf_file(fn: str) -> list[str]:
        paths = []
        for l in open(fn).read().splitlines():
            l = l.strip()
            if not l:
                continue
            if l.startswith("#"):
                continue
            if l.startswith("include "):
                dirglob = l[len("include ") :]
                if dirglob[0] != "/":
                    dirglob = os.path.dirname(os.path.normpath(fn)) + "/" + dirglob
                for sub_fn in glob(dirglob):
                    paths.extend(parse_ld_conf_file(sub_fn))
                continue
            paths.append(l)
        return paths

    LDPATH = os.getenv("LD_LIBRARY_PATH")
    PREFIX = os.getenv("PREFIX")  # Termux & etc.
    paths = []
    if LDPATH:
        paths.extend(LDPATH.split(":"))
    if os.path.exists("/etc/ld.so.conf"):
        paths.extend(parse_ld_conf_file("/etc/ld.so.conf"))
    else:
        print('WARNING: file "/etc/ld.so.conf" not found.', file=sys.stderr)
    if PREFIX:
        if os.path.exists(PREFIX + "/etc/ld.so.conf"):
            paths.extend(parse_ld_conf_file(PREFIX + "/etc/ld.so.conf"))
        else:
            print(
                'WARNING: file "' + PREFIX + '/etc/ld.so.conf" not found.',
                file=sys.stderr,
            )
        paths.extend(
            [
                PREFIX + "/lib",
                PREFIX + "/usr/lib",
                PREFIX + "/lib64",
                PREFIX + "/usr/lib64",
            ]
        )
    paths.extend(["/lib", "/usr/lib", "/lib64", "/usr/lib64"])
    return [path for path in paths if os.path.isdir(path)]


def resolve_libraries(path: str, files_patterns: list[str]) -> list[ResolvedLib]:
    """Scans the PATH directory looking for the files complying with
    the FILES_PATTERNS regexes list.

    Returns the list of the resolved DSOs."""
    libraries: list[ResolvedLib] = []

    def is_dso_matching_pattern(filename):
        for pattern in files_patterns:
            if re.search(pattern, filename):
                return True
        return False

    for fname in os.listdir(path):
        abs_file_path = os.path.abspath(os.path.join(path, fname))
        if os.path.isfile(abs_file_path) and is_dso_matching_pattern(abs_file_path):
            libraries.append(
                ResolvedLib(name=fname, dirpath=path, fullpath=abs_file_path)
            )
    return libraries


def copy_and_patch_libs(
    dsos: list[ResolvedLib], dest_dir: str, rpath: str | None = None
) -> None:
    """Copies the graphic vendor DSOs to the cache directory before
    patchelf-ing them.

    The DSOs can dlopen each other. Sadly, we don't want any host
    libraries to the LD_LIBRARY_PATH to prevent polluting the nix
    binary env. The only option left is to patch their ELFs runpath to
    point to RPATH.

    We also don't want to directly modify the host DSOs. In the end,
    we first copy them to the user's personal cache directory, we then
    alter their runpath to point to the cache directory."""
    rpath = rpath if (rpath is not None) else dest_dir
    new_paths: list[str] = []
    for dso in dsos:
        basename = os.path.basename(dso.fullpath)
        newpath = os.path.join(dest_dir, basename)
        log_info(f"Copying and patching {dso} to {newpath}")
        shutil.copyfile(dso.fullpath, newpath)
        # Provide write permissions to ensure we can patch this binary.
        os.chmod(newpath, os.stat(dso.fullpath).st_mode | stat.S_IWUSR)
        new_paths.append(newpath)
    patch_dsos(new_paths, rpath)


def patch_dsos(dsoPaths: list[str], rpath: str) -> None:
    """Call patchelf to change the DSOS runpath with RPATH."""
    log_info(f"Patching {dsoPaths}")
    log_info(f"Exec: {PATCHELF_PATH} --set-rpath {rpath} {dsoPaths}")
    res = subprocess.run([PATCHELF_PATH, "--set-rpath", rpath] + dsoPaths)
    if res.returncode != 0:
        raise BaseException(
            f"Cannot patch {dsoPaths}. Patchelf exited with {res.returncode}"
        )


def is_dso_cache_up_to_date(dsos: CacheDirContent, cache_file_path: str) -> bool:
    """Check whether or not we need to update the cache.

    We keep what's in the cache through a JSON file stored at the root
    of the cache_dir. We consider a dynamically shared object to be up
    to date if its name, its full path, its size and last modification
    timestamp are equivalent."""
    log_info("Checking if the cache is up to date")
    if os.path.isfile(cache_file_path):
        with open(cache_file_path, "r", encoding="utf8") as f:
            try:
                cached_dsos: CacheDirContent = CacheDirContent.from_json(f.read())
            except:
                return False
            return dsos == cached_dsos
    return False


def cache_library_path(
    library_path: LibraryPath, temp_cache_dir_root: str, final_cache_dir_root: str
) -> str:
    """Generate a cache directory for the LIBRARY_PATH host directory.

    This cache directory is mirroring the host directory containing
    the graphics card drivers. Its full name is hashed: it's an
    attempt to keep the final LD_LIBRARY_PATH reasonably sized.

    Returns the name of the cache directory created by this
    function to CACHE_DIR_ROOT."""
    # Hash Computation
    h = hashlib.sha256()
    h.update(library_path.path.encode("utf8"))
    path_hash: str = h.hexdigest()
    # Paths
    cache_path_root: str = os.path.join(temp_cache_dir_root, path_hash)
    lib_dir = os.path.join(cache_path_root, "lib")
    rpath_lib_dir = os.path.join(final_cache_dir_root, path_hash, "lib")
    cuda_dir = os.path.join(cache_path_root, "cuda")
    egl_dir = os.path.join(cache_path_root, "egl")
    glx_dir = os.path.join(cache_path_root, "glx")
    # Copy and patch DSOs
    for dsos, d in [
        (library_path.generic, lib_dir),
        (library_path.cuda, cuda_dir),
        (library_path.egl, egl_dir),
        (library_path.glx, glx_dir),
    ]:
        os.makedirs(d, exist_ok=True)
        if len(dsos) > 0:
            copy_and_patch_libs(dsos=dsos, dest_dir=d, rpath=rpath_lib_dir)
        else:
            log_info(f"Did not find any DSO to put in {d}, skipping copy and patching.")
    return path_hash


def generate_cache_ld_library_path(cache_paths: list[str]) -> str:
    """Generates the LD_LIBRARY_PATH colon-separated string pointing
    to the cached DSOs living inside the CACHE_PATHS.

    CACHE_PATH being a list pointing to the root of all the cached
    library paths.
    """
    ld_library_paths: list[str] = []
    for path in cache_paths:
        ld_library_paths = ld_library_paths + [
            f"{path}/glx",
            f"{path}/cuda",
            f"{path}/egl",
        ]
    return ":".join(ld_library_paths)


def generate_cache_metadata(
    cache_dir: str, cache_content: CacheDirContent, cache_paths: list[str]
) -> str:
    """Generates the various cache metadata for a given CACHE_CONTENT
    and CACHE_PATHS in CACHE_DIR. Return the associated LD_LIBRARY_PATH.

    The metadata being:

    - CACHE_DIR/cache.json: json file containing all the paths info.
    - CACHE_DIR/ld_library_path: file containing the LD_LIBRARY_PATH
      to inject for the CACHE_PATHS.
    - CACHE_DIR/egl-confs: directory containing the various EGL
      confs."""
    cache_file_path = os.path.join(cache_dir, "cache.json")
    cached_ld_library_path = os.path.join(cache_dir, "ld_library_path")
    egl_conf_dir = os.path.join(cache_dir, "egl-confs")
    with open(cache_file_path, "w", encoding="utf8") as f:
        f.write(cache_content.to_json())
    nix_gl_ld_library_path = generate_cache_ld_library_path(cache_paths)
    log_info(f"Caching LD_LIBRARY_PATH: {nix_gl_ld_library_path}")
    with open(cached_ld_library_path, "w", encoding="utf8") as f:
        f.write(nix_gl_ld_library_path)
    generate_nvidia_egl_config_files(egl_conf_dir)
    return nix_gl_ld_library_path


def exec_binary(bin_path: str, args: list[str]) -> None:
    """Replace the current python program with the program pointed by
    BIN_PATH.

    Sets the relevant libGLvnd env variables."""
    log_info(f"Execv-ing {bin_path}")
    log_info(f"Goodbye now.")
    os.execvp(bin_path, [bin_path] + args)
