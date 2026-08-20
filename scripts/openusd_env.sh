#!/usr/bin/env sh
# Source this file to activate an OpenUSD runtime in Bash or Zsh.

if [ -n "${BASH_VERSION:-}" ] && [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "openusd_env.sh must be sourced: source scripts/openusd_env.sh <OpenUSD-prefix>" >&2
    exit 2
fi
if [ -n "${ZSH_VERSION:-}" ]; then
    case ${ZSH_EVAL_CONTEXT:-} in
        *:file) ;;
        *)
            echo "openusd_env.sh must be sourced: source scripts/openusd_env.sh <OpenUSD-prefix>" >&2
            exit 2
            ;;
    esac
fi

_openusd_activate() {
    if [ "$#" -lt 1 ]; then
        echo "usage: source scripts/openusd_env.sh <OpenUSD-prefix> [options]" >&2
        return 2
    fi

    _openusd_root=$1
    shift
    if [ -n "${OPENUSDCONNECT_ENV_PYTHON:-}" ]; then
        _openusd_python=$OPENUSDCONNECT_ENV_PYTHON
    elif command -v python3 >/dev/null 2>&1; then
        _openusd_python=$(command -v python3)
    elif command -v python >/dev/null 2>&1; then
        _openusd_python=$(command -v python)
    else
        echo "Python was not found. Activate the matching venv or set OPENUSDCONNECT_ENV_PYTHON." >&2
        return 2
    fi

    if [ -n "${BASH_VERSION:-}" ]; then
        _openusd_script=${BASH_SOURCE[0]}
    else
        _openusd_script=$0
    fi
    _openusd_script_dir=$(CDPATH= cd -- "$(dirname -- "$_openusd_script")" && pwd)
    _openusd_output=$(
        "$_openusd_python" "$_openusd_script_dir/openusd_runtime.py" \
            --usd-root "$_openusd_root" \
            --python-executable "$_openusd_python" \
            --format posix \
            "$@"
    ) || return $?
    eval "$_openusd_output"
}

_openusd_activate "$@"
_openusd_status=$?
_openusd_finish() {
    unset _openusd_output _openusd_python _openusd_root _openusd_script _openusd_script_dir
    unset _openusd_status
    unset -f _openusd_activate _openusd_finish 2>/dev/null || \
        unfunction _openusd_activate _openusd_finish 2>/dev/null
    return "$1"
}
_openusd_finish "$_openusd_status"
return $? 2>/dev/null || exit $?
