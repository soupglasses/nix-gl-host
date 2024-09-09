import argparse
import sys
import time
import os

from nixglhost import get_ld_paths, exec_binary
from nixglhost.util import log_info
from nixglhost.nvidia import nvidia_main


def cli():
    parser = argparse.ArgumentParser(
        prog="nixglhost",
        description="Wrapper used to massage the host GL drivers to work with your nix-built binary.",
    )
    parser.add_argument(
        "-d",
        "--driver-directory",
        type=str,
        help="Use the driver libraries contained in this directory instead of discovering them from the load path.",
        default=None,
    )
    parser.add_argument(
        "-p",
        "--print-ld-library-path",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Print the GL/Cuda LD_LIBRARY_PATH env you should add to your environment.",
    )
    parser.add_argument(
        "NIX_BINARY",
        type=str,
        nargs="?",
        help="Nix-built binary you'd like to wrap.",
        default=None,
    )
    parser.add_argument(
        "ARGS",
        nargs=argparse.REMAINDER,
        help="The args passed to the wrapped binary.",
        default=None,
    )
    args = parser.parse_args()
    if args.print_ld_library_path and args.NIX_BINARY:
        print(
            "ERROR: -p and NIX_BINARY are both set. You have to choose between one of these options.",
            file=sys.stderr,
        )
        print("       run nixglhost --help for more information. ", file=sys.stderr)
        sys.exit(1)
    elif not args.print_ld_library_path and not args.NIX_BINARY:
        print("ERROR: Please set the NIX_BINARY you want to run.", file=sys.stderr)
        print("       run nixglhost --help for more information. ", file=sys.stderr)
        sys.exit(1)

    start_time = time.time()
    home = os.path.expanduser("~")
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME", os.path.join(home, ".cache"))
    cache_dir = os.path.join(xdg_cache_home, "nix-gl-host")
    os.makedirs(cache_dir, exist_ok=True)
    log_info(f'Using "{cache_dir}" as cache dir.')
    if args.driver_directory:
        log_info(
            f"Retrieving DSOs from the specified directory: {args.driver_directory}"
        )
        host_dsos_paths: list[str] = [args.driver_directory]
    else:
        log_info("Retrieving DSOs from the load path.")
        host_dsos_paths: list[str] = get_ld_paths()
    new_env = nvidia_main(cache_dir, host_dsos_paths, args.print_ld_library_path)
    log_info(f"{time.time() - start_time} seconds elapsed since script start.")
    if args.NIX_BINARY:
        os.environ.update(new_env)
        exec_binary(args.NIX_BINARY, args.ARGS)
    return 0


if __name__ == "__main__":
    sys.exit(cli())
