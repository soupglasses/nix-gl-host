import os


def log_info(string: str) -> None:
    """Prints STR to STDERR if the DEBUG environment variable is
    set."""
    if "DEBUG" in os.environ:
        print(f"[+] {string}")
