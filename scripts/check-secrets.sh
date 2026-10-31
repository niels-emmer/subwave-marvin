#!/bin/sh
# check-secrets.sh - fail if a live station credential sits in a git-tracked file.
#
# Scope: tracked files only. This is a commit gate, not a worktree sweep, so a
# secret in an untracked or ignored file is deliberately invisible here.
#
# Add "# nosecret" to a line to allow a documented false positive.
# "password": "__REDACTED__" is legal repo content (PLAN 5.5) and is not a hit.

set -u

hits_file=$(mktemp) || exit 2
list_file=$(mktemp) || exit 2
trap 'rm -f "$hits_file" "$list_file"' EXIT INT TERM

git ls-files > "$list_file" 2>/dev/null || {
    echo "check-secrets: not a git repository" >&2
    exit 2
}

if [ ! -s "$list_file" ]; then
    echo "check-secrets: clean (no tracked files)"
    exit 0
fi

# Mask matched values before printing anything.
mask() {
    sed -E \
        -e 's/(sk-ant-|sk-|gh[po]_|xox[baprs]-)[A-Za-z0-9_-]{4,}/\1***/g' \
        -e 's/("[^"]*"[[:space:]]*:[[:space:]]*")[^"]*/\1***/g' \
        | cut -c1-120
}

already() { # path, line: one report per line, first rule wins
    [ -s "$hits_file" ] && grep -qF "$1:$2: " "$hits_file"
}

record_hit() { # path, line, rule label, raw line
    already "$1" "$2" && return 0
    printf '%s:%s: [%s] %s\n' "$1" "$2" "$3" "$(printf '%s\n' "$4" | mask)" >> "$hits_file"
}

record_live() { # path, line, variable name
    already "$1" "$2" && return 0
    printf '%s:%s: [live-value:%s] REDACTED\n' "$1" "$2" "$3" >> "$hits_file"
}

scan_env() { # variable name; its value never reaches argv or stdout
    eval "val=\${$1-}"
    [ -n "${val:-}" ] || return 0
    while IFS= read -r f; do
        [ -f "$f" ] || continue
        printf '%s\n' "$val" \
            | grep -Fn -f - "$f" 2>/dev/null \
            | grep -v '# nosecret' \
            | while IFS= read -r hit; do
                record_live "$f" "${hit%%:*}" "$1"
            done
    done < "$list_file"
}

scan_regex() { # rule label, ERE
    while IFS= read -r f; do
        [ -f "$f" ] || continue
        grep -nIE "$2" "$f" 2>/dev/null \
            | grep -v '# nosecret' \
            | grep -vE ':[[:space:]]*"__REDACTED__"' \
            | while IFS= read -r hit; do
                record_hit "$f" "${hit%%:*}" "$1" "${hit#*:}"
            done
    done < "$list_file"
}

# Live values first: if a line carries one, that is the label worth showing.
scan_env SUBWAVE_ADMIN_PASS
scan_env SUBWAVE_STATION_PASSWORD
scan_env ANTHROPIC_API_KEY

scan_regex anthropic-key 'sk-ant-[A-Za-z0-9_-]{8,}'
scan_regex openai-key 'sk-[A-Za-z0-9_-]{20,}'
scan_regex github-token 'gh[po]_[A-Za-z0-9]{20,}'
scan_regex slack-token 'xox[baprs]-[A-Za-z0-9-]{10,}'
scan_regex private-key '-----BEGIN [A-Z ]*PRIVATE KEY-----'
scan_regex json-secret '"[A-Za-z_]*(pass|password|secret|token|apikey|api_key|sessionkey)[A-Za-z_]*"[[:space:]]*:[[:space:]]*"[^"]+"'

if [ -s "$hits_file" ]; then
    cat "$hits_file"
    printf 'check-secrets: %s hit(s) - commit blocked\n' "$(wc -l < "$hits_file" | tr -d ' ')"
    exit 1
fi

printf 'check-secrets: clean (%s tracked files)\n' "$(wc -l < "$list_file" | tr -d ' ')"
exit 0
