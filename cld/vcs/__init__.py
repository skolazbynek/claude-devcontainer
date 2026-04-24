"""VCS abstraction layer -- jujutsu preferred, git fallback."""

from cld.vcs.base import VcsBackend
from cld.vcs.detect import get_backend

__all__ = ["VcsBackend", "get_backend"]
