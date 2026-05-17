"""agentibrain — standalone brain + KB kernel for the agenti ecosystem."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("agentibrain")
except PackageNotFoundError:
    # Editable install before metadata is written, or running from source
    # without pip install -e. Tests assert this is a non-empty string.
    __version__ = "0.0.0+dev"
