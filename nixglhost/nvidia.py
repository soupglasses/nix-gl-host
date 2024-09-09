import os
import shutil
import fcntl
import tempfile
import json

from nixglhost.util import log_info
from nixglhost import (
    CacheDirContent,
    LibraryPath,
    resolve_libraries,
    generate_cache_metadata,
    cache_library_path,
    is_dso_cache_up_to_date,
    get_ld_paths,
    generate_cache_ld_library_path,
)

# The following regexes list has been figured out by looking at the
# output of nix-build -A linuxPackages.nvidia_x11 before running
# ls ./result/lib | grep -E ".so$".
#
# TODO: find a more systematic way to figure out these names *not
# requiring to build/fetch the nvidia driver at runtime*.
# TODO: compile the regexes
NVIDIA_DSO_PATTERNS = [
    r"libGLESv1_CM_nvidia\.so.*$",
    r"libGLESv2_nvidia\.so.*$",
    r"libglxserver_nvidia\.so.*$",
    r"libnvcuvid\.so.*$",
    r"libnvidia-allocator\.so.*$",
    r"libnvidia-cfg\.so.*$",
    r"libnvidia-compiler\.so.*$",
    r"libnvidia-eglcore\.so.*$",
    r"libnvidia-encode\.so.*$",
    r"libnvidia-fbc\.so.*$",
    r"libnvidia-glcore\.so.*$",
    r"libnvidia-glsi\.so.*$",
    r"libnvidia-glvkspirv\.so.*$",
    r"libnvidia-gpucomp\.so.*$",
    r"libnvidia-ml\.so.*$",
    r"libnvidia-ngx\.so.*$",
    r"libnvidia-nvvm\.so.*$",
    r"libnvidia-opencl\.so.*$",
    r"libnvidia-opticalflow\.so.*$",
    r"libnvidia-ptxjitcompiler\.so.*$",
    r"libnvidia-rtcore\.so.*$",
    r"libnvidia-tls\.so.*$",
    r"libnvidia-vulkan-producer\.so.*$",
    r"libnvidia-wayland-client\.so.*$",
    r"libnvoptix\.so.*$",
    # Cannot find that one :(
    r"libnvtegrahv\.so.*$",
    # Host dependencies required by the nvidia DSOs to properly
    # operate
    # libdrm
    r"libdrm\.so.*$",
    # libffi
    r"libffi\.so.*$",
    # libgbm
    r"libgbm\.so.*$",
    # libexpat
    r"libexpat\.so.*$",
    # libxcb
    r"libxcb-glx\.so.*$",
    # Coming from libx11
    r"libX11-xcb\.so.*$",
    r"libX11\.so.*$",
    r"libXext\.so.*$",
    # libwayland
    r"libwayland-server\.so.*$",
    r"libwayland-client\.so.*$",
]

NVIDIA_CUDA_DSO_PATTERNS = [r"libcudadebugger\.so.*$", r"libcuda\.so.*$"]

NVIDIA_GLX_DSO_PATTERNS = [r"libGLX_nvidia\.so.*$"]

NVIDIA_EGL_DSO_PATTERNS = [
    r"libEGL_nvidia\.so.*$",
    r"libnvidia-egl-wayland\.so.*$",
    r"libnvidia-egl-gbm\.so.*$",
]


def generate_nvidia_egl_config_files(egl_conf_dir: str) -> None:
    """Generates a set of JSON files describing the EGL exec
    environment to libglvnd.

    These configuration files will point to the EGL, wayland and GBM
    Nvidia DSOs. We're only specifying the DSOs names here to give the
    linker enough legroom to load the most appropriate DSO from the
    LD_LIBRARY_PATH."""

    def generate_egl_conf_json(dso):
        return json.dumps(
            {"file_format_version": "1.0.0", "ICD": {"library_path": dso}}
        )

    dso_paths = [
        ("10_nvidia.json", f"libEGL_nvidia.so.0"),
        ("10_nvidia_wayland.json", f"libnvidia-egl-wayland.so.1"),
        ("15_nvidia_gbm.json", f"libnvidia-egl-gbm.so.1"),
    ]

    for conf_file_name, dso_name in dso_paths:
        os.makedirs(egl_conf_dir, exist_ok=True)
        with open(
            os.path.join(egl_conf_dir, conf_file_name), "w", encoding="utf-8"
        ) as f:
            log_info(f"Writing {dso_name} conf to {egl_conf_dir}")
            f.write(generate_egl_conf_json(dso_name))


def scan_dsos_from_dir(path: str) -> LibraryPath | None:
    """Look for the different kind of DSOs we're searching in a
    particular library path.
    This will match and hash the content of each object we're
    interested in."""
    generic = resolve_libraries(path, NVIDIA_DSO_PATTERNS)
    if len(generic) > 0:
        cuda = resolve_libraries(path, NVIDIA_CUDA_DSO_PATTERNS)
        glx = resolve_libraries(path, NVIDIA_GLX_DSO_PATTERNS)
        egl = resolve_libraries(path, NVIDIA_EGL_DSO_PATTERNS)
        return LibraryPath(glx=glx, cuda=cuda, generic=generic, egl=egl, path=path)
    else:
        return None


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


def nvidia_main(
    cache_dir: str, dso_vendor_paths: list[str], print_ld_library_path: bool = False
) -> dict:
    """Prepares the environment necessary to run a opengl/cuda program
    on a Nvidia graphics card. It is by definition really stateful.

    Roughly, we're going to:

    1. Setup the nvidia cache directory.
    2. Find the nvidia DSOs in the DSO_VENDOR_PATHS directories.
    3. Copy these DSOs to their appropriate cache directories.
    4. Generate the EGL configuration files.
    5. Patchelf the runpath of what needs to be patched.
    6. Generate the env variables the main process is supposed to set.

    Keep in mind we want to keep the host system out of the
    LD_LIBRARY_PATH to make sure we won't inject any host DSOs (other
    than the GL/Cuda ones OFC) to the nix-built program.

    We're isolating the main DSOs for GLX/EGL/Cuda in their own dirs,
    add add these directory to the LD_LIBRARY_PATH. We patch their
    runpaths to point to the generic cache dir, containing all the
    libraries we don't want to expose to the program we're wrapping.

    This function returns a dictionary containing the env variables
    supposed to be added to the current process down the line."""
    log_info("Nvidia routine begins")
    # Find Host DSOS
    log_info("Searching for the host DSOs")
    cache_content: CacheDirContent = CacheDirContent(paths=[])
    cache_file_path = os.path.join(cache_dir, "cache.json")
    lock_path = os.path.join(os.path.split(cache_dir)[0], "nix-gl-host.lock")
    cached_ld_library_path = os.path.join(cache_dir, "ld_library_path")
    paths = get_ld_paths()
    egl_conf_dir = os.path.join(cache_dir, "egl-confs")
    nix_gl_ld_library_path: str | None = None
    # Cache/Patch DSOs
    #
    # We need to be super careful about race conditions here. We're
    # using a file lock to make sure only one nix-gl-host instance can
    # access the cache at a time.
    #
    # If the cache is locked, we'll wait until the said lock is
    # released. The lock will always be released when the lock FD get
    # closed, IE. when we'll get out of this block.
    with open(lock_path, "w") as lock:
        log_info("Acquiring the cache lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        log_info("Cache lock acquired")
        for path in paths:
            res = scan_dsos_from_dir(path)
            if res:
                cache_content.paths.append(res)
        if not is_dso_cache_up_to_date(
            cache_content, cache_file_path
        ) or not os.path.isfile(cached_ld_library_path):
            log_info("The cache is not up to date, regenerating it")
            # We're building first the cache in a temporary directory
            # to make sure we won't end up with a partially
            # populated/corrupted nix-gl-host cache.
            with tempfile.TemporaryDirectory() as tmp_cache:
                tmp_cache_dir = os.path.join(tmp_cache, "nix-gl-host")
                os.makedirs(tmp_cache_dir)
                cache_paths: list[str] = []
                for p in cache_content.paths:
                    log_info(f"Caching {p}")
                    cache_paths.append(cache_library_path(p, tmp_cache_dir, cache_dir))
                # Pointing the LD_LIBRARY_PATH to the final destination
                # instead of the tmp dir.
                cache_absolute_paths = [os.path.join(cache_dir, p) for p in cache_paths]
                nix_gl_ld_library_path = generate_cache_metadata(
                    tmp_cache_dir, cache_content, cache_absolute_paths
                )
                # The temporary cache has been successfully populated,
                # let's mv it to the actual nix-gl-host cache.
                # Note: The move operation is atomic on linux.
                log_info(f"Mv {tmp_cache_dir} to {cache_dir}")
                if os.path.exists(cache_dir):
                    shutil.rmtree(cache_dir)
                shutil.move(tmp_cache_dir, os.path.split(cache_dir)[0])
        else:
            log_info("The cache is up to date, re-using it.")
            with open(cached_ld_library_path, "r", encoding="utf8") as f:
                nix_gl_ld_library_path = f.read()
    log_info("Cache lock released")

    assert nix_gl_ld_library_path, "The nix-host-gl LD_LIBRARY_PATH is not set"
    log_info(f"Injecting LD_LIBRARY_PATH: {nix_gl_ld_library_path}")
    new_env = {}
    log_info(f"__GLX_VENDOR_LIBRARY_NAME = nvidia")
    new_env["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
    log_info(f"__EGL_VENDOR_LIBRARY_DIRS = {egl_conf_dir}")
    new_env["__EGL_VENDOR_LIBRARY_DIRS"] = egl_conf_dir
    ld_library_path = os.environ.get("LD_LIBRARY_PATH", None)
    if print_ld_library_path:
        print(nix_gl_ld_library_path)
    ld_library_path = (
        nix_gl_ld_library_path
        if ld_library_path is None
        else f"{nix_gl_ld_library_path}:{ld_library_path}"
    )
    log_info(f"LD_LIBRARY_PATH = {ld_library_path}")
    new_env["LD_LIBRARY_PATH"] = ld_library_path
    return new_env
