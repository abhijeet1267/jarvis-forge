#!/bin/sh
# dev_port_guard.sh — keep test builds off the installed app's backend port.
#
# The incident this exists for: a second copy of Vantage (`dist/Vantage.app`)
# was launched during a build-testing session. It started its own backend on
# 8765 and displaced the backend /Applications/Vantage.app was talking to; the
# app refused to connect and the user's session was broken until someone
# killed the stray process by hand. The app's ownership guard worked. The
# build/test tooling had none.
#
#   scripts/dev_port_guard.sh check <port>   exit 0 if free, 1 if held or
#                                            reserved; names the holder and
#                                            its cwd on stderr
#   scripts/dev_port_guard.sh claim          print the first free port in
#                                            8798-8810 on stdout
#
# Port 8765 belongs to the installed app. `check 8765` refuses even when the
# port is free, and `claim` never returns it.
#
# This is a guard, not a reaper. It never signals or kills a process, and it
# never binds a port. `claim` reports a free port; it does not reserve it, so
# treat the printed number as a hint, not a lock — if the build fails to bind
# it, something else took it first.
#
# Visibility: lsof as an ordinary user may not see the files of a process
# owned by another user, in which case a busy port can look free. Run this as
# the user who owns the build.
#
# Requires: lsof and standard utilities. POSIX sh, no bashisms.

set -u

RESERVED_PORT=8765
CLAIM_FIRST=8798
CLAIM_LAST=8810

usage() {
    cat >&2 <<EOF
usage: $0 check <port>
       $0 claim

  check <port>  exit 0 if the TCP port is free, 1 if something is listening
                (names its pid and working directory) or if the port is the
                installed app's reserved port $RESERVED_PORT.
  claim         print the first free port starting at $CLAIM_FIRST, up to
                $CLAIM_LAST, and nothing else on stdout.
EOF
}

# One pid per line; empty and non-zero when nothing is listening.
listeners() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null
}

describe_holder() {
    pid=$1
    cwd=$(lsof -nP -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)
    if [ -n "$cwd" ]; then
        printf '  pid %s (cwd: %s)\n' "$pid" "$cwd" >&2
    else
        printf '  pid %s (cwd: not readable; the process may belong to another user)\n' "$pid" >&2
    fi
}

valid_port() {
    case $1 in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

cmd_check() {
    [ $# -eq 1 ] || { usage; exit 2; }
    port=$1
    if ! valid_port "$port"; then
        printf 'not a port number: %s\n' "$port" >&2
        exit 2
    fi

    if [ "$port" -eq "$RESERVED_PORT" ]; then
        printf 'REFUSED: port %s is the installed app'"'"'s backend port.\n' "$port" >&2
        printf 'A test build must never take it. Run "%s claim" for a development port (%s-%s).\n' \
            "$0" "$CLAIM_FIRST" "$CLAIM_LAST" >&2
        if pids=$(listeners "$port"); then
            printf 'Port %s is currently held by:\n' "$port" >&2
            for pid in $pids; do
                describe_holder "$pid"
            done
        else
            printf 'It is free right now. That does not make it available: do not bind it.\n' >&2
        fi
        exit 1
    fi

    if pids=$(listeners "$port"); then
        printf 'port %s is IN USE:\n' "$port" >&2
        for pid in $pids; do
            describe_holder "$pid"
        done
        exit 1
    fi
    printf 'port %s is free\n' "$port"
    exit 0
}

cmd_claim() {
    [ $# -eq 0 ] || { usage; exit 2; }
    port=$CLAIM_FIRST
    while [ "$port" -le "$CLAIM_LAST" ]; do
        # The range sits above RESERVED_PORT and must stay that way.
        if [ "$port" -ne "$RESERVED_PORT" ] && ! listeners "$port" >/dev/null; then
            printf '%s\n' "$port"
            exit 0
        fi
        port=$((port + 1))
    done
    printf 'no free development port in %s-%s\n' "$CLAIM_FIRST" "$CLAIM_LAST" >&2
    exit 1
}

if ! command -v lsof >/dev/null 2>&1; then
    printf 'dev_port_guard: lsof not found on PATH\n' >&2
    exit 2
fi

[ $# -ge 1 ] || { usage; exit 2; }
verb=$1
shift

case $verb in
    check) cmd_check "$@" ;;
    claim) cmd_claim "$@" ;;
    *) usage; exit 2 ;;
esac