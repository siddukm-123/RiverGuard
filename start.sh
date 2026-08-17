#!/bin/sh
# Having this as its own script (rather than inline in the Dockerfile's CMD)
# guarantees the ${PORT:-8000} variable expansion actually happens: a file
# starting with a shebang is always run through that interpreter by the OS
# itself, regardless of how the outer platform (Railway) invokes the
# container's entrypoint. Putting the same string directly in a Dockerfile
# CMD relies on that outer layer choosing to wrap it in "sh -c", which isn't
# guaranteed everywhere.
exec gunicorn --chdir backend app:app --bind "0.0.0.0:${PORT:-8000}" --workers 1 --threads 8 --timeout 300 --preload
