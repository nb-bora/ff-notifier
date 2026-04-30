#!/bin/sh
set -e

# WeasyPrint (PDF generation) needs a writable /tmp for temp files.
chmod 1777 /tmp

exec gosu ff-user "$@"
