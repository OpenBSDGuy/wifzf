"""Small fzf-based UI helpers."""
import shlex
import shutil
import sys
from typing import List, Optional

from pyfzf.pyfzf import FzfPrompt


def require_fzf() -> None:
    if not shutil.which("fzf"):
        sys.exit("fzf not found. Install it with: pkg install fzf")


def pick(choices: List[str], header: str = "", prompt: str = "> ", extra: str = "") -> Optional[str]:
    """Show an fzf list and return the selected line, or None on ESC/empty."""
    if not choices:
        return None
    opts = (
        f"--height=50% --reverse --border --no-sort "
        f"--header={shlex.quote(header)} --prompt={shlex.quote(prompt)} {extra}"
    )
    try:
        result = FzfPrompt().prompt(choices, opts)
    except Exception:
        return None
    return result[0] if result else None


def confirm(question: str) -> bool:
    return pick(["Yes", "No"], header=question, prompt="confirm> ") == "Yes"


def info(msg: str) -> None:
    print(f"\033[36m::\033[0m {msg}")


def error(msg: str) -> None:
    print(f"\033[31m!!\033[0m {msg}")


def pause() -> None:
    input("\nPress Enter to continue...")
