#!/usr/bin/env sh
# Source this file to activate an OpenUSD runtime in Bash or Zsh.

if [ -n "${BASH_VERSION:-}" ] && [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "openusd_env.sh must be sourced: source scripts/openusd_env.sh [OpenUSD-prefix]" >&2
    exit 2
fi
if [ -n "${ZSH_VERSION:-}" ]; then
    case ${ZSH_EVAL_CONTEXT:-} in
        *:file) ;;
        *)
            echo "openusd_env.sh must be sourced: source scripts/openusd_env.sh [OpenUSD-prefix]" >&2
            exit 2
            ;;
    esac
fi

_openusd_activate() {
    if [ -n "${BASH_VERSION:-}" ]; then
        _openusd_script=${BASH_SOURCE[0]}
    else
        _openusd_script=$0
    fi
    _openusd_script_dir=$(CDPATH= cd -- "$(dirname -- "$_openusd_script")" && pwd)
    _openusd_repo_root=$(CDPATH= cd -- "$_openusd_script_dir/.." && pwd)

    _openusd_root=
    if [ "$#" -gt 0 ]; then
        case $1 in
            -*) ;;
            *)
                _openusd_root=$1
                shift
                ;;
        esac
    fi
    if [ -n "$_openusd_root" ]; then
        if [ ! -d "$_openusd_root" ]; then
            echo "OpenUSD install prefix does not exist: $_openusd_root" >&2
            return 2
        fi
        if [ ! -d "$_openusd_root/bin" ]; then
            echo "OpenUSD install prefix has no bin directory: $_openusd_root" >&2
            echo "For the project-managed build, omit the prefix: source scripts/openusd_env.sh" >&2
            return 2
        fi
    fi

    if [ -n "${OPENUSDCONNECT_ENV_PYTHON:-}" ]; then
        _openusd_python=$OPENUSDCONNECT_ENV_PYTHON
    elif [ -x "$_openusd_repo_root/.venv/bin/python" ]; then
        _openusd_python=$_openusd_repo_root/.venv/bin/python
    elif command -v python3 >/dev/null 2>&1; then
        _openusd_python=$(command -v python3)
    elif command -v python >/dev/null 2>&1; then
        _openusd_python=$(command -v python)
    else
        echo "Python was not found. Activate the matching venv or set OPENUSDCONNECT_ENV_PYTHON." >&2
        return 2
    fi

    if [ -n "$_openusd_root" ]; then
        _openusd_output=$(
            "$_openusd_python" "$_openusd_script_dir/openusd_runtime.py" \
                --usd-root "$_openusd_root" \
                --python-executable "$_openusd_python" \
                --format posix \
                "$@"
        ) || return $?
    else
        _openusd_output=$(
            "$_openusd_python" "$_openusd_script_dir/openusd_runtime.py" \
                --managed \
                --python-executable "$_openusd_python" \
                --format posix \
                "$@"
        ) || return $?
    fi
    eval "$_openusd_output"
}

_openusd_activate "$@"
_openusd_status=$?
_openusd_finish() {
    unset _openusd_output _openusd_python _openusd_repo_root _openusd_root
    unset _openusd_script _openusd_script_dir
    unset _openusd_status
    unset -f _openusd_activate _openusd_finish 2>/dev/null || \
        unfunction _openusd_activate _openusd_finish 2>/dev/null
    return "$1"
}
_openusd_finish "$_openusd_status"
return $? 2>/dev/null || exit $?
