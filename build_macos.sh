#!/usr/bin/env bash
set -euo pipefail

fail() {
    echo "error: $*" >&2
    exit 1
}

[[ "$(uname -s)" == "Darwin" ]] || fail "build_macos.sh must run on macOS."
for tool in realpath file xcrun plutil ditto swift; do
    command -v "$tool" >/dev/null 2>&1 || fail "Required tool was not found: $tool"
done

SCRIPT_PATH="$(realpath "$0")"
PROJECT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd -P)"
BUILD_ROOT="$PROJECT_DIR/.build"
DIST_DIR="$PROJECT_DIR/dist"
VENV="$BUILD_ROOT/backend-venv"
BROWSER_CACHE="$BUILD_ROOT/playwright-browsers"
BACKEND_BUILD="$BUILD_ROOT/pyinstaller"
BACKEND_DIST="$BUILD_ROOT/backend-dist"
PIP_CACHE_DIR="$BUILD_ROOT/pip-cache"
TEMP_ROOT="$BUILD_ROOT/tmp"
PYINSTALLER_CONFIG_DIR="$BUILD_ROOT/pyinstaller-config"
LICENSE_STAGE="$BUILD_ROOT/backend-licenses"
APP_BUNDLE="$DIST_DIR/CSI OpenBase.app"
ARCHIVE="$DIST_DIR/CSI-OpenBase-macOS.zip"

assert_safe_output_path() {
    local target="$1" relative current old_ifs part canonical
    local -a parts
    case "$target" in
        "$PROJECT_DIR"/*) ;;
        *) fail "Output is outside the physical mac project: $target" ;;
    esac
    [[ "$target" != "$PROJECT_DIR" ]] || fail "Project root cannot be an output path."
    relative="${target#"$PROJECT_DIR"/}"
    old_ifs="$IFS"
    IFS='/'
    read -r -a parts <<< "$relative"
    IFS="$old_ifs"
    current="$PROJECT_DIR"
    for part in "${parts[@]}"; do
        [[ -n "$part" && "$part" != "." && "$part" != ".." ]] \
            || fail "Unsafe output path component: $target"
        current="$current/$part"
        [[ ! -L "$current" ]] \
            || fail "Output path has a symlink ancestor: $current"
    done
    if [[ -e "$target" ]]; then
        canonical="$(realpath "$target")"
        case "$canonical" in
            "$PROJECT_DIR"/*) ;;
            *) fail "Resolved output escapes the mac project: $target -> $canonical" ;;
        esac
    fi
}

paths_overlap() {
    case "$1" in "$2"|"$2"/*) return 0 ;; esac
    case "$2" in "$1"|"$1"/*) return 0 ;; esac
    return 1
}

assert_input_outside_outputs() {
    if paths_overlap "$1" "$BUILD_ROOT" || paths_overlap "$1" "$DIST_DIR"; then
        fail "$2 overlaps mac build output and could be deleted: $1"
    fi
}

validate_attribution_text() {
    local source="$1" label="$2" encoding
    [[ -f "$source" ]] || fail "Missing $label: $source"
    encoding="$(file -b --mime-encoding "$source")"
    case "$encoding" in us-ascii|utf-8) ;; *) fail "$label is not UTF-8 text: $source" ;; esac
    if LC_ALL=C grep -Eq '(^|[[:space:]])(/Users/|/private/|/Volumes/|/tmp/|[A-Za-z]:[\\/])' "$source"; then
        fail "$label contains a local absolute path: $source"
    fi
}

validate_attribution_tree() {
    local root="$1" label="$2" file count
    [[ -d "$root" ]] || fail "Missing $label directory: $root"
    [[ -z "$(find "$root" -type l -print -quit)" ]] \
        || fail "$label must not contain symlinks: $root"
    count=0
    while IFS= read -r -d '' file; do
        validate_attribution_text "$file" "$label file"
        count=$((count + 1))
    done < <(find "$root" -type f -print0)
    [[ "$count" -gt 0 ]] || fail "$label directory is empty: $root"
}

HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in arm64|x86_64) ;; *) fail "Unsupported host architecture: $HOST_ARCH" ;; esac
LIPO_TOOL="$(xcrun --find lipo)"
validate_macho_architecture() {
    local executable="$1" label="$2" description architectures
    [[ -f "$executable" && -x "$executable" ]] \
        || fail "$label is not executable: $executable"
    description="$(file -b "$executable")"
    case "$description" in *Mach-O*) ;; *) fail "$label is not Mach-O: $executable" ;; esac
    architectures="$("$LIPO_TOOL" -archs "$executable")" \
        || fail "Could not inspect $label architecture."
    case " $architectures " in
        *" $HOST_ARCH "*) ;;
        *) fail "$label lacks $HOST_ARCH architecture (found: $architectures)" ;;
    esac
}

for path in "$BUILD_ROOT" "$DIST_DIR" "$VENV" "$BROWSER_CACHE" \
    "$BACKEND_BUILD" "$BACKEND_DIST" "$PIP_CACHE_DIR" "$TEMP_ROOT" \
    "$PYINSTALLER_CONFIG_DIR" "$LICENSE_STAGE" "$APP_BUNDLE" "$ARCHIVE"; do
    assert_safe_output_path "$path"
done

for path in LICENSE NOTICE Packaging/Info.plist Packaging/backend_entry.py \
    Packaging/backend.entitlements Packaging/chromium.entitlements \
    scripts/write_dependency_licenses.py; do
    [[ -f "$PROJECT_DIR/$path" ]] || fail "Missing mac project file: $path"
done

INPUT_MODE=""
PYTHON_PROJECT_SOURCE="$PROJECT_DIR/python-web"
PYTHON_WHEEL_SOURCE=""
BACKEND_SOURCE=""
BACKEND_NOTICES_SOURCE=""
SIGNING_ID="${SIGNING_IDENTITY:-}"
PYTHON_COMMAND="${PYTHON_BIN:-python3}"

usage() {
    echo "Usage: ./build_macos.sh [INPUT] [--sign IDENTITY]"
    echo "  --python-project PATH  Freeze a Python project (default: ./python-web)"
    echo "  --python-wheel PATH    Freeze an installed csi-openbase wheel"
    echo "  --backend PATH         Advanced prebuilt macOS backend override"
    echo "  --backend-notices DIR  Attribution directory required by --backend"
    echo "  --sign IDENTITY        Signing identity; '-' means ad-hoc"
}

select_mode() {
    [[ -z "$INPUT_MODE" ]] \
        || fail "--python-project, --python-wheel and --backend are mutually exclusive."
    INPUT_MODE="$1"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --python-project)
            [[ $# -ge 2 ]] || fail "--python-project requires a path"
            select_mode python-project; PYTHON_PROJECT_SOURCE="$2"; shift 2 ;;
        --python-wheel)
            [[ $# -ge 2 ]] || fail "--python-wheel requires a path"
            select_mode python-wheel; PYTHON_WHEEL_SOURCE="$2"; shift 2 ;;
        --backend)
            [[ $# -ge 2 ]] || fail "--backend requires a path"
            select_mode backend; BACKEND_SOURCE="$2"; shift 2 ;;
        --backend-notices)
            [[ $# -ge 2 ]] || fail "--backend-notices requires a path"
            BACKEND_NOTICES_SOURCE="$2"; shift 2 ;;
        --sign)
            [[ $# -ge 2 ]] || fail "--sign requires an identity"
            SIGNING_ID="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$INPUT_MODE" ]]; then
    INPUT_MODE=python-project
fi
[[ "$INPUT_MODE" == backend || -z "$BACKEND_NOTICES_SOURCE" ]] \
    || fail "--backend-notices may only be used with --backend."

PYTHON_INSTALL_SOURCE=""
PYTHON_EXECUTABLE=""
BACKEND_EXECUTABLE_SOURCE=""
PROJECT_NOTICE_SOURCE=""
PREBUILT_BACKEND=0

if [[ "$INPUT_MODE" == python-project ]]; then
    [[ -d "$PYTHON_PROJECT_SOURCE" ]] || fail "Python project not found: $PYTHON_PROJECT_SOURCE"
    PYTHON_INSTALL_SOURCE="$(realpath "$PYTHON_PROJECT_SOURCE")"
    [[ -f "$PYTHON_INSTALL_SOURCE/pyproject.toml" ]] \
        || fail "Python project has no pyproject.toml: $PYTHON_INSTALL_SOURCE"
    assert_input_outside_outputs "$PYTHON_INSTALL_SOURCE" "Python project"
elif [[ "$INPUT_MODE" == python-wheel ]]; then
    [[ -f "$PYTHON_WHEEL_SOURCE" ]] || fail "Python wheel not found: $PYTHON_WHEEL_SOURCE"
    PYTHON_INSTALL_SOURCE="$(realpath "$PYTHON_WHEEL_SOURCE")"
    case "$PYTHON_INSTALL_SOURCE" in *.whl) ;; *) fail "--python-wheel requires a .whl file" ;; esac
    assert_input_outside_outputs "$PYTHON_INSTALL_SOURCE" "Python wheel"
else
    PREBUILT_BACKEND=1
    [[ -e "$BACKEND_SOURCE" ]] || fail "External backend not found: $BACKEND_SOURCE"
    BACKEND_SOURCE="$(realpath "$BACKEND_SOURCE")"
    assert_input_outside_outputs "$BACKEND_SOURCE" "External backend"
    if [[ -d "$BACKEND_SOURCE" ]]; then
        BACKEND_EXECUTABLE_SOURCE="$BACKEND_SOURCE/CSI.OpenBase.Backend"
    else
        BACKEND_EXECUTABLE_SOURCE="$BACKEND_SOURCE"
    fi
    [[ ! -L "$BACKEND_EXECUTABLE_SOURCE" ]] \
        || fail "External backend main executable must not be a symlink."
    BACKEND_EXECUTABLE_SOURCE="$(realpath "$BACKEND_EXECUTABLE_SOURCE")"
    assert_input_outside_outputs "$BACKEND_EXECUTABLE_SOURCE" "External backend executable"
    validate_macho_architecture "$BACKEND_EXECUTABLE_SOURCE" "External backend"

    if [[ -z "$BACKEND_NOTICES_SOURCE" ]]; then
        if [[ -d "$BACKEND_SOURCE" ]]; then
            BACKEND_NOTICES_SOURCE="$BACKEND_SOURCE"
        else
            BACKEND_NOTICES_SOURCE="$(dirname "$BACKEND_SOURCE")"
        fi
    fi
    [[ -d "$BACKEND_NOTICES_SOURCE" ]] \
        || fail "External backend attribution directory not found: $BACKEND_NOTICES_SOURCE"
    BACKEND_NOTICES_SOURCE="$(realpath "$BACKEND_NOTICES_SOURCE")"
    assert_input_outside_outputs "$BACKEND_NOTICES_SOURCE" "External backend notices"
    if [[ -f "$BACKEND_NOTICES_SOURCE/backend/LICENSE" ]]; then
        PROJECT_NOTICE_SOURCE="$BACKEND_NOTICES_SOURCE/backend"
    else
        PROJECT_NOTICE_SOURCE="$BACKEND_NOTICES_SOURCE"
    fi
    PROJECT_NOTICE_SOURCE="$(realpath "$PROJECT_NOTICE_SOURCE")"
    assert_input_outside_outputs "$PROJECT_NOTICE_SOURCE" "External project notices"
    for name in LICENSE NOTICE THIRD-PARTY-NOTICES.md; do
        validate_attribution_text "$PROJECT_NOTICE_SOURCE/$name" "external backend $name"
        assert_input_outside_outputs \
            "$(realpath "$PROJECT_NOTICE_SOURCE/$name")" \
            "External backend $name"
    done
    validate_attribution_tree \
        "$PROJECT_NOTICE_SOURCE/licenses" \
        "external backend third-party licenses"
    while IFS= read -r -d '' license_file; do
        assert_input_outside_outputs \
            "$(realpath "$license_file")" \
            "External backend third-party license"
    done < <(find "$PROJECT_NOTICE_SOURCE/licenses" -type f -print0)
    validate_attribution_text "$BACKEND_NOTICES_SOURCE/CPython-LICENSE.txt" \
        "external backend CPython license"
    assert_input_outside_outputs \
        "$(realpath "$BACKEND_NOTICES_SOURCE/CPython-LICENSE.txt")" \
        "External backend CPython license"
    validate_attribution_text "$BACKEND_NOTICES_SOURCE/python-packages.txt" \
        "external backend Python dependency report"
    assert_input_outside_outputs \
        "$(realpath "$BACKEND_NOTICES_SOURCE/python-packages.txt")" \
        "External backend Python dependency report"
fi

if [[ "$PREBUILT_BACKEND" -eq 0 ]]; then
    PYTHON_EXECUTABLE="$(command -v "$PYTHON_COMMAND")" \
        || fail "Python executable not found: $PYTHON_COMMAND"
    PYTHON_EXECUTABLE="$(realpath "$PYTHON_EXECUTABLE")"
    [[ -x "$PYTHON_EXECUTABLE" ]] || fail "Python is not executable: $PYTHON_EXECUTABLE"
    assert_input_outside_outputs "$PYTHON_EXECUTABLE" "Bootstrap Python"
fi

# All inputs are canonical and disjoint from outputs before the first deletion.
rm -rf "$VENV" "$BROWSER_CACHE" "$BACKEND_BUILD" "$BACKEND_DIST" \
    "$TEMP_ROOT" "$PYINSTALLER_CONFIG_DIR" "$LICENSE_STAGE" "$APP_BUNDLE"
rm -f "$ARCHIVE"
mkdir -p "$BUILD_ROOT" "$DIST_DIR" "$LICENSE_STAGE/backend"

if [[ "$PREBUILT_BACKEND" -eq 0 ]]; then
    mkdir -p "$BROWSER_CACHE" "$BACKEND_BUILD/spec" "$BACKEND_BUILD/work" \
        "$BACKEND_DIST" "$PIP_CACHE_DIR" "$TEMP_ROOT"
    echo "Creating isolated Python backend environment..."
    "$PYTHON_EXECUTABLE" -m venv "$VENV"
    VENV_PYTHON="$VENV/bin/python"
    export PIP_CACHE_DIR
    export TMPDIR="$TEMP_ROOT"
    export PYTHONPYCACHEPREFIX="$BUILD_ROOT/pycache"
    export PYINSTALLER_CONFIG_DIR
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install "${PYTHON_INSTALL_SOURCE}[desktop]"

    INSTALLED_ENTRY="$("$VENV_PYTHON" -I -c \
        'import pathlib, scripts.run_openbase as e; print(pathlib.Path(e.__file__).resolve())')"
    case "$INSTALLED_ENTRY" in
        "$VENV"/*) ;;
        *) fail "Backend entry was not loaded from the isolated environment: $INSTALLED_ENTRY" ;;
    esac

    echo "Collecting installed Python attribution texts..."
    "$VENV_PYTHON" "$PROJECT_DIR/scripts/write_dependency_licenses.py" \
        --output "$LICENSE_STAGE/python-packages.txt" \
        --project-notices-dir "$LICENSE_STAGE/backend" \
        --cpython-license "$LICENSE_STAGE/CPython-LICENSE.txt" \
        --include-package PyInstaller
    for name in LICENSE NOTICE THIRD-PARTY-NOTICES.md; do
        validate_attribution_text "$LICENSE_STAGE/backend/$name" "installed backend $name"
    done
    validate_attribution_tree \
        "$LICENSE_STAGE/backend/licenses" \
        "installed backend third-party licenses"
    validate_attribution_text "$LICENSE_STAGE/CPython-LICENSE.txt" "CPython license"
    validate_attribution_text "$LICENSE_STAGE/python-packages.txt" "Python dependency report"

    echo "Installing bundled Playwright Chromium..."
    PLAYWRIGHT_BROWSERS_PATH="$BROWSER_CACHE" \
        "$VENV_PYTHON" -m playwright install chromium --no-shell

    PYINSTALLER_ARGS=(
        --noconfirm --name CSI.OpenBase.Backend --onedir --console
        --distpath "$BACKEND_DIST"
        --workpath "$BACKEND_BUILD/work"
        --specpath "$BACKEND_BUILD/spec"
        --collect-all playwright
        --collect-submodules uvicorn
        --collect-data admin_app
        --hidden-import anyio._backends._asyncio
        --exclude-module numpy
        --exclude-module pytest
        --exclude-module tkinter
        --exclude-module trio
    )
    BROWSER_COUNT=0
    for browser_dir in "$BROWSER_CACHE"/*; do
        if [[ -d "$browser_dir" ]]; then
            browser_name="$(basename "$browser_dir")"
            PYINSTALLER_ARGS+=(--add-data "$browser_dir:ms-playwright/$browser_name")
            BROWSER_COUNT=$((BROWSER_COUNT + 1))
        fi
    done
    [[ "$BROWSER_COUNT" -gt 0 ]] || fail "Playwright installed no browser runtime."

    echo "Freezing installed csi-openbase backend..."
    PLAYWRIGHT_BROWSERS_PATH="$BROWSER_CACHE" \
        "$VENV_PYTHON" -m PyInstaller "${PYINSTALLER_ARGS[@]}" \
        "$PROJECT_DIR/Packaging/backend_entry.py"
    BACKEND_SOURCE="$BACKEND_DIST/CSI.OpenBase.Backend"
    BACKEND_EXECUTABLE_SOURCE="$BACKEND_SOURCE/CSI.OpenBase.Backend"
    validate_macho_architecture "$BACKEND_EXECUTABLE_SOURCE" "Generated backend"
else
    cp "$PROJECT_NOTICE_SOURCE/LICENSE" "$LICENSE_STAGE/backend/LICENSE"
    cp "$PROJECT_NOTICE_SOURCE/NOTICE" "$LICENSE_STAGE/backend/NOTICE"
    cp "$PROJECT_NOTICE_SOURCE/THIRD-PARTY-NOTICES.md" \
        "$LICENSE_STAGE/backend/THIRD-PARTY-NOTICES.md"
    cp -R "$PROJECT_NOTICE_SOURCE/licenses" "$LICENSE_STAGE/backend/licenses"
    cp "$BACKEND_NOTICES_SOURCE/CPython-LICENSE.txt" \
        "$LICENSE_STAGE/CPython-LICENSE.txt"
    cp "$BACKEND_NOTICES_SOURCE/python-packages.txt" \
        "$LICENSE_STAGE/python-packages.txt"
fi

echo "Building native host and backend supervisor..."
swift build --package-path "$PROJECT_DIR" -c release --product CSIOpenBaseMac
swift build --package-path "$PROJECT_DIR" -c release --product CSIBackendLauncher
BIN_DIR="$(swift build --package-path "$PROJECT_DIR" -c release --show-bin-path)"
HOST_BINARY="$BIN_DIR/CSIOpenBaseMac"
LAUNCHER_BINARY="$BIN_DIR/CSIBackendLauncher"
validate_macho_architecture "$HOST_BINARY" "Swift host"
validate_macho_architecture "$LAUNCHER_BINARY" "Backend supervisor"

CONTENTS="$APP_BUNDLE/Contents"
RESOURCES="$CONTENTS/Resources"
BACKEND_DESTINATION="$RESOURCES/backend"
mkdir -p "$CONTENTS/MacOS" "$BACKEND_DESTINATION"
cp "$HOST_BINARY" "$CONTENTS/MacOS/CSIOpenBaseMac"
cp "$LAUNCHER_BINARY" "$RESOURCES/CSIBackendLauncher"
cp "$PROJECT_DIR/Packaging/Info.plist" "$CONTENTS/Info.plist"
chmod +x "$CONTENTS/MacOS/CSIOpenBaseMac" "$RESOURCES/CSIBackendLauncher"

if [[ -d "$BACKEND_SOURCE" ]]; then
    cp -R "$BACKEND_SOURCE" "$BACKEND_DESTINATION/CSI.OpenBase.Backend"
    BUNDLED_BACKEND="$BACKEND_DESTINATION/CSI.OpenBase.Backend/CSI.OpenBase.Backend"
else
    cp "$BACKEND_SOURCE" "$BACKEND_DESTINATION/CSI.OpenBase.Backend"
    BUNDLED_BACKEND="$BACKEND_DESTINATION/CSI.OpenBase.Backend"
fi
chmod +x "$BUNDLED_BACKEND"
validate_macho_architecture "$BUNDLED_BACKEND" "Bundled backend"

cp "$PROJECT_DIR/LICENSE" "$RESOURCES/LICENSE"
cp "$PROJECT_DIR/NOTICE" "$RESOURCES/NOTICE"
mkdir -p "$RESOURCES/backend-licenses"
cp -R "$LICENSE_STAGE/." "$RESOURCES/backend-licenses/"

plutil -lint "$CONTENTS/Info.plist" \
    "$PROJECT_DIR/Packaging/backend.entitlements" \
    "$PROJECT_DIR/Packaging/chromium.entitlements"

is_macho() {
    case "$(file -b "$1")" in *Mach-O*) return 0 ;; *) return 1 ;; esac
}

if [[ -n "$SIGNING_ID" ]]; then
    command -v codesign >/dev/null 2>&1 || fail "codesign was not found."
    echo "Signing nested code from the inside out..."
    codesign_one() {
        local target="$1" entitlements="${2:-}"
        local -a args
        args=(--force --options runtime --sign "$SIGNING_ID")
        [[ "$SIGNING_ID" == "-" ]] || args+=(--timestamp)
        [[ -z "$entitlements" ]] || args+=(--entitlements "$entitlements")
        codesign "${args[@]}" "$target"
    }

    while IFS= read -r -d '' code_file; do
        is_macho "$code_file" || continue
        case "$code_file" in
            "$BACKEND_DESTINATION"/*/ms-playwright/*)
                codesign_one "$code_file" "$PROJECT_DIR/Packaging/chromium.entitlements" ;;
            "$BACKEND_DESTINATION"/*)
                codesign_one "$code_file" "$PROJECT_DIR/Packaging/backend.entitlements" ;;
            *) codesign_one "$code_file" ;;
        esac
    done < <(find "$CONTENTS" -type f -print0)

    while IFS= read -r -d '' bundle; do
        case "$bundle" in
            "$BACKEND_DESTINATION"/*/ms-playwright/*)
                codesign_one "$bundle" "$PROJECT_DIR/Packaging/chromium.entitlements" ;;
            *) codesign_one "$bundle" ;;
        esac
    done < <(find "$CONTENTS" -depth -type d \
        \( -name '*.app' -o -name '*.framework' -o -name '*.xpc' -o -name '*.appex' \) \
        -print0)

    codesign_one "$APP_BUNDLE"
    codesign --verify --deep --strict --verbose=2 "$APP_BUNDLE"
    echo "IMPORTANT: run the signed Chromium authorization/start/exit smoke test before release."
fi

ditto -c -k --sequesterRsrc --keepParent "$APP_BUNDLE" "$ARCHIVE"
echo "Application: $APP_BUNDLE"
echo "Archive:     $ARCHIVE"
