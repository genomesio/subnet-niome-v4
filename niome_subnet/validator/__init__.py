from .forward import forward

__version__ = "1.0.0"
version_split = __version__.split(".")
__spec_version__ = (
    (1000 * int(version_split[0])) 
    + (100 * int(version_split[1])) 
    + int(version_split[2])
)