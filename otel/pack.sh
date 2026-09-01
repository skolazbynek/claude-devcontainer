#!/usr/bin/env bash
# pack.sh -- package this directory (otel/) into a single portable artifact
# so it can be handed to someone with no cld, no repo access, and no hosting
# to fetch from. Two shapes:
#
#   ./pack.sh              a self-extracting bash script (readable header +
#                           base64 tar.gz payload). Recipient runs it, gets
#                           an otel/ directory, follows QUICK-START.md.
#   ./pack.sh --tarball     the plain .tar.gz, for people who won't run a
#                           script they were sent.
#
# otel/ in the cld repo stays the single source of truth: the artifact is
# generated fresh from whatever is on disk every time this runs, and this
# script itself travels inside its own payload so a recipient can re-pack and
# pass it on. No dependency outside tar, gzip, base64, fold, awk, mktemp and
# a sha256 tool (sha256sum / shasum / openssl, whichever is present).
#
#     pack.sh [--out PATH] [--tarball] [--quiet] [-h]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

usage() {
    cat <<'EOF'
usage: pack.sh [--out PATH] [--tarball] [--quiet] [-h]

  --out PATH   write the artifact here (default: ./<generated name> in $PWD)
  --tarball    emit the plain .tar.gz instead of a self-extracting script
  --quiet      suppress the summary; print only the artifact path
EOF
}

OUT=""
TARBALL=0
QUIET=0
while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="${2:?--out requires a path}"; shift 2 ;;
        --tarball) TARBALL=1; shift ;;
        --quiet) QUIET=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "pack.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    else
        echo ""
    fi
}

# --- file discovery --------------------------------------------------------
# Every regular file directly in $HERE (non-recursive -- otel/ is flat),
# minus pack.sh's own output and runtime state. Discovery by listing, not a
# hard-coded manifest, so new files (T2..T5's changes, a future doctor
# helper) ride along with no edit to this script.
discover_files() {
    local f base
    for f in "$HERE"/*; do
        [ -f "$f" ] || continue
        base="$(basename "$f")"
        case "$base" in
            .*) continue ;;
            otel-standalone-*.sh) continue ;;
            *.tar.gz|*.tgz) continue ;;
            aggregate.log|aggregate.pid) continue ;;
            PROVENANCE) continue ;;
        esac
        echo "$base"
    done | LC_ALL=C sort
}

FILES="$(discover_files)"

for required in otelctl.sh aggregate.py otel-collector-config.yaml README.md; do
    if ! printf '%s\n' "$FILES" | grep -qx "$required"; then
        echo "pack.sh: refusing to pack -- $required not found in $HERE (pack.sh must live inside otel/)" >&2
        exit 1
    fi
done

# --- provenance --------------------------------------------------------
# Best-effort, never fatal (VCS probes are guarded with || true). jj is
# queried first and before anything else touches the tree: jj snapshots the
# working copy on every command, so this is what makes @ the revision being
# packed.
REV_ID=""
SOURCE_REV="unknown"
SOURCE_REMOTE=""

if command -v jj >/dev/null 2>&1 && (cd "$HERE" && jj root >/dev/null 2>&1); then
    commit_id="$( (cd "$HERE" && jj log -r @ --no-graph -T 'commit_id.short(12)') 2>/dev/null)" || true
    change_id="$( (cd "$HERE" && jj log -r @ --no-graph -T 'change_id.short(12)') 2>/dev/null)" || true
    if [ -n "${commit_id:-}" ]; then
        REV_ID="$commit_id"
        SOURCE_REV="jj ${commit_id} (change ${change_id:-unknown})"
    fi
fi

if [ -z "$REV_ID" ] && command -v git >/dev/null 2>&1 && git -C "$HERE" rev-parse --short=12 HEAD >/dev/null 2>&1; then
    rev="$(git -C "$HERE" rev-parse --short=12 HEAD 2>/dev/null)" || true
    if [ -n "${rev:-}" ]; then
        dirty=""
        if [ -n "$(git -C "$HERE" status --porcelain -- "$HERE" 2>/dev/null)" ]; then
            dirty="-dirty"
        fi
        REV_ID="${rev}${dirty}"
        SOURCE_REV="git ${rev}${dirty}"
    fi
fi

if [ -z "$REV_ID" ] && [ -f "$HERE/PROVENANCE" ]; then
    prior="$(grep '^Source rev:' "$HERE/PROVENANCE" 2>/dev/null | head -1 | sed 's/^Source rev:[[:space:]]*//')" || true
    if [ -n "${prior:-}" ]; then
        REV_ID="carried"
        SOURCE_REV="${prior} (re-packed from a standalone copy)"
    fi
fi

SOURCE_REMOTE="$( (cd "$HERE" && jj git remote list 2>/dev/null | awk '{print $2; exit}') )" || true
if [ -z "$SOURCE_REMOTE" ]; then
    SOURCE_REMOTE="$(git -C "$HERE" remote get-url origin 2>/dev/null)" || true
fi

# --- build --------------------------------------------------------------
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DATE_SLUG="$(date -u +%Y%m%d)"

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
mkdir "$staging/otel"

while IFS= read -r name; do
    [ -n "$name" ] || continue
    cp -p "$HERE/$name" "$staging/otel/$name"
done <<<"$FILES"

manifest=""
while IFS= read -r name; do
    [ -n "$name" ] || continue
    sum="$(_sha256 "$staging/otel/$name")"
    [ -n "$sum" ] || sum="unavailable"
    size="$(wc -c < "$staging/otel/$name" | tr -d ' ')"
    manifest="${manifest}  ${sum}  ${size}  ${name}
"
done <<<"$FILES"

remote_block=""
if [ -n "$SOURCE_REMOTE" ]; then
    remote_block="Source remote: ${SOURCE_REMOTE}
                 (informational only -- may not be reachable from here)
"
fi

cat > "$staging/otel/PROVENANCE" <<PROVEOF
otel standalone copy -- provenance

Packed:        ${TIMESTAMP}
Source:        cld repo, otel/ (verbatim copy)
Source rev:    ${SOURCE_REV}
${remote_block}Packed by:     otel/pack.sh v1

These files are a byte-for-byte copy of otel/ in the cld repo at the revision
above; that directory is the single source of truth. To update, get a newer
artifact from someone with repo access -- or re-run ./pack.sh here to pass
this exact copy on. Local edits will be overwritten by the next extraction.

Files (sha256  bytes  name):
${manifest}
PROVEOF

tar czf "$staging/payload.tar.gz" -C "$staging" otel

payload_sha="$(_sha256 "$staging/payload.tar.gz")"
[ -n "$payload_sha" ] || payload_sha="unavailable"
payload_bytes="$(wc -c < "$staging/payload.tar.gz" | tr -d ' ')"

namerev="${REV_ID:-norev}"
if [ -z "$OUT" ]; then
    if [ "$TARBALL" = "1" ]; then
        OUT="$PWD/otel-standalone-${DATE_SLUG}-${namerev}.tar.gz"
    else
        OUT="$PWD/otel-standalone-${DATE_SLUG}-${namerev}.sh"
    fi
fi

if [ "$TARBALL" = "1" ]; then
    mv "$staging/payload.tar.gz" "$OUT"
else
    # Two heredocs, deliberately: a short UNQUOTED one carrying only the
    # substituted metadata (rev, checksum, manifest, remote) as comments and
    # shell assignments, then the static installer logic in a QUOTED
    # (<<'EOS') heredoc so its own $0/$@/$(...) are never touched at pack
    # time. Do not merge these into one heredoc.
    # Composed unprefixed, then piped through one sed at the end -- a
    # multi-line value (the remote block, the manifest) embedded directly in
    # a "# ..." heredoc line only gets its first line commented, not its
    # continuation lines. Building the whole block first and prefixing it in
    # one pass avoids that class of bug.
    info_body="$(
        printf 'Source:         cld repo, otel/ (verbatim copy)\n'
        printf 'Source rev:     %s\n' "$SOURCE_REV"
        if [ -n "$SOURCE_REMOTE" ]; then
            printf 'Source remote:  %s\n' "$SOURCE_REMOTE"
            printf '                (informational only -- may not be reachable from here)\n'
        fi
        printf 'Packed:         %s\n' "$TIMESTAMP"
        printf 'Payload sha256: %s\n' "$payload_sha"
        printf 'Payload bytes:  %s\n' "$payload_bytes"
        printf '\n'
        printf 'Manifest (sha256  bytes  name):\n'
        printf '%s' "$manifest"
    )"

    cat > "$OUT" <<HEADER
#!/usr/bin/env bash
# otel-standalone-installer -- self-extracting copy of otel/ from the cld
# repo, produced by otel/pack.sh. otel/ in that repo is the single source of
# truth; this file is a snapshot, not a fork.
#
# --- begin info ---
$(printf '%s' "$info_body" | sed 's/^/# /')
# --- end info ---
#
# usage: bash $(basename "$OUT") [--dir PATH] [--force] [--list] [--check] [--no-verify] [-h]

OTEL_PACK_SOURCE_REV="${SOURCE_REV}"
OTEL_PACK_PAYLOAD_SHA256="${payload_sha}"
OTEL_PACK_PAYLOAD_BYTES="${payload_bytes}"
OTEL_PACK_TIMESTAMP="${TIMESTAMP}"
HEADER

    cat >> "$OUT" <<'EOS'
set -euo pipefail

_b64d() {
    if base64 --decode </dev/null >/dev/null 2>&1; then base64 --decode
    elif base64 -d </dev/null >/dev/null 2>&1; then base64 -d
    elif base64 -D </dev/null >/dev/null 2>&1; then base64 -D
    else openssl base64 -d
    fi
}

_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    else
        echo ""
    fi
}

usage() {
    cat <<'USAGE'
usage: bash otel-standalone-*.sh [--dir PATH] [--force] [--list] [--check] [--no-verify] [-h]

  --dir PATH    extract into PATH instead of ./otel
  --force       proceed even if the target directory exists and is non-empty
  --list        print provenance and manifest from the header; extract nothing
  --check       verify the payload checksum only; exit 0 (ok) or 4 (mismatch)
  --no-verify   skip the checksum check on extract
  -h, --help    show this help
USAGE
}

TARGET_DIR="./otel"
FORCE=0
DO_LIST=0
DO_CHECK=0
NO_VERIFY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) TARGET_DIR="${2:?--dir requires a path}"; shift 2 ;;
        --force) FORCE=1; shift ;;
        --list) DO_LIST=1; shift ;;
        --check) DO_CHECK=1; shift ;;
        --no-verify) NO_VERIFY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "otel-standalone: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ ! -f "$0" ] || [ ! -r "$0" ]; then
    echo "otel-standalone: run this as a file (bash otel-standalone-*.sh) -- it reads its own payload back out of itself, so piping it via stdin can't work." >&2
    exit 2
fi

if [ "$DO_LIST" = "1" ]; then
    awk '/^# --- begin info ---$/{f=1;next} /^# --- end info ---$/{f=0} f' "$0" | sed -E 's/^# ?//'
    exit 0
fi

tmp_payload="$(mktemp)"
tmp_extract="$(mktemp -d)"
trap 'rm -rf "$tmp_payload" "$tmp_extract"' EXIT

awk '/^__OTEL_PAYLOAD_BELOW__$/{f=1;next} f' "$0" | _b64d > "$tmp_payload"

if [ "$NO_VERIFY" != "1" ]; then
    if [ -n "$OTEL_PACK_PAYLOAD_SHA256" ] && [ "$OTEL_PACK_PAYLOAD_SHA256" != "unavailable" ]; then
        actual_sha="$(_sha256 "$tmp_payload")"
        if [ -z "$actual_sha" ]; then
            echo "otel-standalone: no checksum tool available on this machine -- skipping verification" >&2
        elif [ "$actual_sha" != "$OTEL_PACK_PAYLOAD_SHA256" ]; then
            echo "otel-standalone: payload does not match its checksum -- the file was likely mangled in transit; ask for it again." >&2
            exit 4
        fi
    else
        echo "otel-standalone: no checksum recorded at pack time -- skipping verification" >&2
    fi
fi

if [ "$DO_CHECK" = "1" ]; then
    echo "otel-standalone: checksum ok"
    exit 0
fi

if [ -e "$TARGET_DIR" ] && [ "$FORCE" != "1" ]; then
    if [ -d "$TARGET_DIR" ] && [ -z "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]; then
        :
    else
        echo "otel-standalone: $TARGET_DIR already exists and is not empty -- pass --dir to pick another location, or --force to extract into it anyway." >&2
        exit 3
    fi
fi

tar xzf "$tmp_payload" -C "$tmp_extract"
mkdir -p "$TARGET_DIR"
cp -pR "$tmp_extract/otel/." "$TARGET_DIR/"

chmod +x "$TARGET_DIR/otelctl.sh" 2>/dev/null || true
chmod +x "$TARGET_DIR/aggregate.py" 2>/dev/null || true
chmod +x "$TARGET_DIR/pack.sh" 2>/dev/null || true

file_count="$(find "$TARGET_DIR" -maxdepth 1 -type f ! -name '.*' | wc -l | tr -d ' ')"
echo "otel-standalone: extracted $file_count files to $TARGET_DIR (requires docker + python3)"
echo "otel-standalone: next -- cd $TARGET_DIR && ./otelctl.sh start"
echo "otel-standalone: see $TARGET_DIR/QUICK-START.md"

exit 0
__OTEL_PAYLOAD_BELOW__
EOS

    base64 "$staging/payload.tar.gz" | fold -w 76 >> "$OUT"
    chmod +x "$OUT"
fi

# --- summary --------------------------------------------------------------
if [ "$QUIET" = "1" ]; then
    echo "$OUT"
else
    total_bytes="$(wc -c < "$OUT" | tr -d ' ')"
    file_count="$(printf '%s\n' "$FILES" | grep -c . || true)"
    echo "$OUT  (${total_bytes} bytes, ${file_count} files, rev ${SOURCE_REV})"
    if [ "$TARBALL" = "1" ]; then
        echo "give it to someone with:  tar xzf $(basename "$OUT") && cd otel && ./otelctl.sh start"
    else
        echo "give it to someone with:  bash $(basename "$OUT")"
    fi
fi
