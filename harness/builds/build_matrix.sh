#!/usr/bin/env bash
# Build every bmv2 variant in matrix.tsv from ONE pinned commit, on the machine that will run it.
# No root needed (dependencies come from setup_testbed.sh). Run under setsid; it is long.
#
#   build_matrix.sh [ID ...]      # default: every id in matrix.tsv that is not built yet
#
# Layout under $BMB (default ~/bmbench):
#   src/behavioral-model          pinned clone, never built in place
#   builds/<id>/                  CMake build tree (objects deleted after the build) or autotools prefix
#   builds/<id>/BUILD-INFO        flags, commit, toolchain, binary sha256, ldd, identity checks
#
# Identity is checked, not assumed: every bmv2 library the binary loads must resolve inside its
# own build directory (a variant silently running against another variant's libraries is the
# classic way to benchmark the wrong build), and the logging/elogger knobs are verified from the
# built code itself.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BMB="${BMB:-$HOME/bmbench}"
COMMIT="${BMV2_COMMIT:-8b28efb274}"
JOBS="${JOBS:-6}"                       # 6 x cc1plus at -O3 fits in 15 GB; 12 would not
SRC="$BMB/src/behavioral-model"
mkdir -p "$BMB/src" "$BMB/builds"

if [[ ! -d "$SRC/.git" ]]; then
    git clone -q https://github.com/p4lang/behavioral-model.git "$SRC" || exit 1
fi
git -C "$SRC" fetch -q origin || true
git -C "$SRC" checkout -q "$COMMIT" || { echo "cannot checkout $COMMIT"; exit 1; }
git -C "$SRC" submodule update -q --init --recursive
FULL="$(git -C "$SRC" rev-parse HEAD)"
echo "bmv2 source: $FULL ($(cat "$SRC/VERSION"))"

ids=("$@")
if (( ${#ids[@]} == 0 )); then
    mapfile -t ids < <(grep -v '^#' "$HERE/matrix.tsv" | cut -f1)
fi

info() {  # $1 = id, $2 = dir, $3 = binary
    local id="$1" dir="$2" bin="$3"
    {
        echo "id=$id"
        echo "bmv2_commit=$FULL"
        echo "bmv2_version=$(cat "$SRC/VERSION")"
        grep -P "^$id\t" "$HERE/matrix.tsv" | awk -F'\t' '{print "system="$2; print "cflags="$3; print "options="$4; print "why="$5}'
        echo "gcc=$(gcc --version | head -1)"
        echo "host=$(hostname) $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs)"
        echo "built_at=$(date -u +%FT%TZ)"
        echo "binary=$bin"
        # Flags as the compiler actually received them (not as matrix.tsv asked), for the file that
        # holds the packet pipeline's hot path. CMake: compile_commands.json; autotools: the make log.
        if [[ -f "$dir/compile_commands.json" ]]; then
            echo "actual_compile_flags=$(python3 -c 'import json,sys
for e in json.load(open(sys.argv[1])):
    if e["file"].endswith("src/bm_sim/match_units.cpp"):
        a = e.get("command") or " ".join(e["arguments"])
        print(" ".join(t for t in a.split() if t.startswith(("-O", "-g", "-march", "-D", "-f")) and not t.startswith("-DBM_") ))
        break' "$dir/compile_commands.json")"
            echo "actual_defines=$(python3 -c 'import json,sys
for e in json.load(open(sys.argv[1])):
    if e["file"].endswith("src/bm_sim/match_units.cpp"):
        a = e.get("command") or " ".join(e["arguments"])
        print(" ".join(t for t in a.split() if t.startswith("-D")))
        break' "$dir/compile_commands.json")"
        else
            echo "actual_compile_flags=$(grep -m1 -oE "match_units\.c[^ ]* .*|.* -c .*match_units\.cpp.*" "$dir/build.log" | grep -oE ' (-O[0-9s]?|-g|-march=[^ ]+|-DNDEBUG)' | tr -d '\n')"
        fi
        echo "binary_sha256=$(sha256sum "$bin" | cut -d' ' -f1)"
        echo "version_string=$("$bin" --version 2>&1 | head -1)"
        # identity check 1: every libbm*/libsimpleswitch*/libruntime* must come from this build
        local foreign
        foreign="$(ldd "$bin" | grep -E 'libbm|libsimpleswitch|libruntimestubs|libsswitch' | grep -v "$dir/" || true)"
        echo "foreign_bmv2_libs=${foreign:-none}"
        ldd "$bin" | sed 's/^/ldd: /'
        # identity check 2: the knobs, read from the built code (binary + its own bmv2 libraries)
        local objs
        objs="$bin $(ldd "$bin" | awk -v d="$dir/" 'index($3, d) == 1 {print $3}' | tr '\n' ' ')"
        echo "evidence_trace_strings=$(strings $objs | grep -c -E 'Processing packet received on port|Parser set: setting field' )"
        echo "evidence_eventlogger_dynsyms=$(nm -DC $objs 2>/dev/null | grep -c 'EventLogger::')"
        echo "evidence_debugger_impl=$(strings $objs | grep -c DebuggerNN)"  # DebuggerNN exists only with BM_DEBUG_ON
    } > "$dir/BUILD-INFO"
}

for id in "${ids[@]}"; do
    line="$(grep -P "^$id\t" "$HERE/matrix.tsv")" || { echo "unknown id $id"; continue; }
    IFS=$'\t' read -r _ system cflags options why <<<"$line"
    dir="$BMB/builds/$id"
    if [[ -f "$dir/BUILD-INFO" ]]; then echo "== $id: already built, skipping"; continue; fi
    echo "== $id ($system; cflags=$cflags; $options)  $(date '+%T')"
    rm -rf "$dir"; mkdir -p "$dir"
    t0=$(date +%s)
    if [[ "$system" == cmake ]]; then
        args=(-S "$SRC" -B "$dir" -G Ninja -DWITH_PI=OFF -DWITH_PDFIXED=OFF -DWITH_STRESS_TESTS=OFF
              -DCMAKE_EXPORT_COMPILE_COMMANDS=ON)
        case "$cflags" in
            -)       ;;                                        # upstream default: nothing at all
            release) args+=(-DCMAKE_BUILD_TYPE=Release) ;;
            *)       args+=("-DCMAKE_C_FLAGS=$cflags" "-DCMAKE_CXX_FLAGS=$cflags") ;;
        esac
        [[ "$options" != - ]] && read -ra extra <<<"$options" && args+=("${extra[@]}")
        cmake "${args[@]}" > "$dir/configure.log" 2>&1 || { echo "   configure FAILED (see $dir/configure.log)"; continue; }
        cmake --build "$dir" --target simple_switch -j "$JOBS" > "$dir/build.log" 2>&1 || { echo "   build FAILED (see $dir/build.log)"; continue; }
        find "$dir" -name '*.o' -delete
        bin="$dir/targets/simple_switch/simple_switch"
    else
        work="$dir/src"; prefix="$dir/inst"
        # autotools defaults to --with-packet-trace, which needs protobuf. When protobuf is not
        # installed system-wide it may come from Ubuntu's own debs unpacked (no root) under
        # $BMB/local/root by `apt-get download` + `dpkg -x`; same package version apt would install.
        LOCALR="$BMB/local/root"; extra_env=()
        if [[ -x "$LOCALR/usr/bin/protoc" ]] && ! command -v protoc >/dev/null; then
            L="$LOCALR/usr/lib/x86_64-linux-gnu"
            # bmv2's packet-trace Makefile ignores PROTOBUF_CFLAGS, so the headers go in CPPFLAGS too
            extra_env=(PATH="$LOCALR/usr/bin:$PATH" PKG_CONFIG_PATH="$L/pkgconfig" LD_LIBRARY_PATH="$L" CPPFLAGS="-I$LOCALR/usr/include")
            ldx="-Wl,-rpath,$L"
        else
            ldx=""
        fi
        cp -a "$SRC" "$work"
        ( cd "$work" && ./autogen.sh ) > "$dir/autogen.log" 2>&1 || { echo "   autogen FAILED"; continue; }
        read -ra extra <<<"$options"
        # NOT --without-pi: bmv2 configure.ac treats any --with(out)-pi as "yes" (AC_ARG_WITH action-if-given)
        cfg=(--prefix="$prefix" "${extra[@]}" "LDFLAGS=-Wl,-rpath,$prefix/lib $ldx")
        case "$cflags" in
            -)     ;;
            cxx:*) cfg+=("CXXFLAGS=${cflags#cxx:}") ;;              # C++ only, as p4lang/tutorials' install script
            *)     cfg+=("CXXFLAGS=$cflags" "CFLAGS=$cflags") ;;
        esac
        ( cd "$work" && env "${extra_env[@]}" ./configure "${cfg[@]}" ) > "$dir/configure.log" 2>&1 || { echo "   configure FAILED (see $dir/configure.log)"; continue; }
        ( cd "$work" && env "${extra_env[@]}" make -j "$JOBS" && env "${extra_env[@]}" make install ) > "$dir/build.log" 2>&1 || { echo "   build FAILED (see $dir/build.log)"; continue; }
        grep -E 'Logging macros|Event logger|Debugger|CXXFLAGS' "$work/config.log" | head -5 > "$dir/configure-recap.txt"
        rm -rf "$work"
        bin="$prefix/bin/simple_switch"
    fi
    [[ -x "$bin" ]] || { echo "   no binary at $bin"; continue; }
    info "$id" "$dir" "$bin"
    echo "   ok in $(( $(date +%s) - t0 )) s; $(grep -E '^(binary_sha256|foreign_bmv2_libs|evidence_)' "$dir/BUILD-INFO" | tr '\n' ' ')"
done
echo "matrix done $(date '+%F %T')"
