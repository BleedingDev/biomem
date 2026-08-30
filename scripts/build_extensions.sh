#!/usr/bin/env bash
# Package the biomem browser extensions.
#
#   dist/chrome-biomem.zip          tested CWS upload / inspection input
#   dist/chrome-biomem.crx          optional self-signed CRX3 (development/managed only)
#   dist/firefox-biomem-unsigned.xpi
#                                    tested AMO signing input; never a public download
#   dist/safari-biomem-macos.zip    Apple signing input; not public until notarized
#   dist/safari-biomem-macos-development-adhoc.zip
#                                    explicit local-development Safari build
#   dist/keys/chrome-biomem.pem     CRX signing key (generate once, keep stable ID)
#   dist/browser-artifacts.json     explicit public/CI-only classification
#
# Usage:
#   scripts/build_extensions.sh [--validate-only] [--no-crx] [--no-safari]
#                               [--safari-mode=release|development] [--prefix=name]
#
# Requirements: python3 (stdlib only), zip/unzip, openssl; node for JS syntax check
# (skipped with a warning if absent); Xcode + safari-web-extension-converter for Safari.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT="$ROOT/extensions"
DIST="$ROOT/dist"
STAGE="$DIST/.stage"
PREFIX="biomem"

VALIDATE_ONLY=0
WITH_CRX=1
WITH_SAFARI=1
SAFARI_MODE="release"
SAFARI_ARCHS="${BIOMEM_SAFARI_ARCHS:-}"

for arg in "$@"; do
  case "$arg" in
    --validate-only) VALIDATE_ONLY=1 ;;
    --no-crx)        WITH_CRX=0 ;;
    --no-safari)     WITH_SAFARI=0 ;;
    --safari-mode=*) SAFARI_MODE="${arg#--safari-mode=}" ;;
    --prefix=*)      PREFIX="${arg#--prefix=}" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

case "$SAFARI_MODE" in
  release|development) ;;
  *) echo "unknown Safari mode: $SAFARI_MODE (expected release or development)" >&2; exit 2 ;;
esac

CHROME_SRC="$EXT/chrome-src"
FIREFOX_SRC="$EXT/firefox-src"
SAFARI_SRC="$EXT/safari-src"

# ---------------------------------------------------------------- helpers
die() { echo "ERROR: $*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

reject_symlinks() {  # reject_symlinks <root> <label>
  local root="$1" label="$2" link
  [[ ! -L "$root" ]] || die "$label must not be a symlink: $root"
  if ! link=$(find -P "$root" -type l -print -quit); then
    die "could not inspect $label for symlinks: $root"
  fi
  [[ -z "$link" ]] || die "$label contains a forbidden symlink: $link"
}

pyjson() {  # pyjson <file> <expr>
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(eval(sys.argv[2], {"d": d}))
' "$1" "$2"
}

stage_copy() {  # stage_copy <src> <dst>
  local src="$1" dst="$2"
  reject_symlinks "$src" "extension source"
  [[ ! -e "$dst" && ! -L "$dst" ]] || die "staging destination already exists: $dst"
  mkdir -p "$(dirname "$dst")"
  mkdir "$dst"
  if have rsync && [[ "${BIOMEM_FORCE_CP_FALLBACK:-0}" != 1 ]]; then
    rsync -a --exclude='_metadata/' --exclude='META-INF/' \
          --exclude='.DS_Store' --exclude='*.pem' --exclude='*.crx' --exclude='*.xpi' \
          "$src/" "$dst/"
  else
    cp -R "$src/." "$dst/"
    rm -rf "$dst/_metadata" "$dst/META-INF"
    find "$dst" -type f \( \
      -name '.DS_Store' -o -name '*.pem' -o -name '*.crx' -o -name '*.xpi' \
    \) -delete
  fi
  reject_symlinks "$src" "extension source after copy"
  reject_symlinks "$dst" "extension staging tree"
}

verify_safari_resource_tree() {  # verify_safari_resource_tree <resources-dir> <label>
  local resources="$1" label="$2"
  python3 - "$SAFARI_SRC" "$resources" "$label" <<'PY'
from pathlib import Path
import sys

canonical = Path(sys.argv[1])
packaged = Path(sys.argv[2])
label = sys.argv[3]

def files(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts)
    }

expected = files(canonical)
actual = files(packaged)
missing = sorted(expected.keys() - actual.keys())
extra = sorted(actual.keys() - expected.keys())
changed = sorted(name for name in expected.keys() & actual.keys() if expected[name] != actual[name])
if missing or extra or changed:
    details = []
    if missing:
        details.append("missing=" + ",".join(missing))
    if extra:
        details.append("extra=" + ",".join(extra))
    if changed:
        details.append("changed=" + ",".join(changed))
    raise SystemExit(f"{label} differs from canonical safari-src: {'; '.join(details)}")
print(f"  {label} matches canonical safari-src ({len(expected)} files)")
PY
}

verify_safari_app_resources() {  # verify_safari_app_resources <app> <label>
  local app="$1" label="$2" resources resource_count
  resources=$(find "$app" -path '*/PlugIns/*.appex/Contents/Resources' -type d -print)
  resource_count=$(wc -l <<< "$resources" | tr -d ' ')
  [[ $resource_count -eq 1 && -n "$resources" ]] || \
    die "$label must contain exactly one extension resource tree"
  verify_safari_resource_tree "$resources" "$label"
}

# ---------------------------------------------------------------- config
[[ -d "$CHROME_SRC" ]] || die "chrome source not found: $CHROME_SRC"
[[ -d "$FIREFOX_SRC" ]] || die "firefox source not found: $FIREFOX_SRC"
[[ -d "$SAFARI_SRC" ]] || die "safari source not found: $SAFARI_SRC"
reject_symlinks "$CHROME_SRC" "Chrome source"
reject_symlinks "$FIREFOX_SRC" "Firefox source"
reject_symlinks "$SAFARI_SRC" "Safari source"
reject_symlinks "$EXT/safari-xcode" "Safari Xcode source"

VERSION="$(pyjson "$CHROME_SRC/manifest.json" 'd["version"]')"
NAME="$(pyjson "$CHROME_SRC/manifest.json" 'd["name"]')"
FF_VERSION="$(pyjson "$FIREFOX_SRC/manifest.json" 'd["version"]')"
SF_VERSION="$(pyjson "$SAFARI_SRC/manifest.json" 'd["version"]')"

[[ "$VERSION" == "$FF_VERSION" && "$VERSION" == "$SF_VERSION" ]] || \
  die "manifest versions differ: chrome=$VERSION firefox=$FF_VERSION safari=$SF_VERSION"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "unexpected version format: $VERSION"

echo "==> packaging \"$NAME\" v$VERSION (prefix: $PREFIX)"

# ---------------------------------------------------------------- validation
echo "==> validating sources"
JS_FILES=$(find "$EXT" \( -path "$EXT/*/assets" -prune \) -o -name '*.js' -print | sort)
if have node; then
  bad=0
  while IFS= read -r f; do
    if ! node --check "$f" >/dev/null 2>&1; then
      echo "  JS syntax error: $f" >&2
      bad=1
    fi
  done <<< "$JS_FILES"
  [[ $bad -eq 0 ]] || die "JS syntax check failed"
  echo "  JS syntax ok ($(wc -l <<< "$JS_FILES" | tr -d ' ') files)"
else
  echo "  WARNING: node not found, skipping JS syntax check"
fi

python3 - "$CHROME_SRC" "$FIREFOX_SRC" "$SAFARI_SRC" <<'PY'
import json, sys, os, re
for d in sys.argv[1:]:
    mpath = os.path.join(d, "manifest.json")
    m = json.load(open(mpath, encoding="utf-8"))
    assert m.get("manifest_version") == 3, f"{mpath}: not MV3"
    base = os.path.dirname(mpath)
    missing = [p for p in m.get("icons", {}).values() if not os.path.exists(os.path.join(base, p))]
    assert not missing, f"{mpath}: missing icons {missing}"
    for war in m.get("web_accessible_resources", []):
        for r in war.get("resources", []):
            assert os.path.exists(os.path.join(base, r)), f"{mpath}: missing WAR {r}"
    for key in ("_metadata", "META-INF"):
        assert not os.path.exists(os.path.join(base, key)), f"{mpath}: {key} must not ship in package"
# firefox specifics
ff = json.load(open(os.path.join(sys.argv[2], "manifest.json"), encoding="utf-8"))
gid = ff.get("browser_specific_settings", {}).get("gecko", {}).get("id", "")
assert gid, "firefox manifest: gecko id missing"
print(f"  manifests ok (firefox gecko id: {gid})")
PY

SAFARI_PBXPROJ=$(find "$EXT/safari-xcode" -name project.pbxproj -type f | head -1 || true)
[[ -n "$SAFARI_PBXPROJ" ]] || die "Safari Xcode project file not found"
python3 - "$SAFARI_SRC" "$SAFARI_PBXPROJ" <<'PY'
from pathlib import Path
import re, sys

canonical = Path(sys.argv[1])
project = Path(sys.argv[2])
text = project.read_text(encoding="utf-8")
expected = {
    path.name for path in canonical.iterdir()
    if not path.name.startswith(".")
}
referenced = set(re.findall(r'path = "\.\./\.\./\.\./safari-src/([^"/]+)";', text))
missing = sorted(expected - referenced)
stale = sorted(referenced - expected)
if missing or stale:
    raise SystemExit(
        "Safari Xcode resource references drifted from safari-src: "
        f"missing={missing}; stale={stale}"
    )
not_packaged = sorted(name for name in expected if text.count(f"/* {name} in Resources */") < 4)
if not_packaged:
    raise SystemExit(f"Safari Xcode resources are not assigned to both extension targets: {not_packaged}")
shadow = project.parent.parent / "Shared (Extension)"
unexpected_shadow = sorted(
    path.name for path in shadow.iterdir()
    if path.name != "SafariWebExtensionHandler.swift" and not path.name.startswith(".")
)
if unexpected_shadow:
    raise SystemExit(f"Safari Xcode contains shadow web-extension resources: {unexpected_shadow}")
print(f"  Safari Xcode references canonical safari-src ({len(expected)} top-level entries)")
PY

SAFARI_EXISTING_DIST="$EXT/safari-xcode/Dist"
if [[ $VALIDATE_ONLY -eq 1 && -d "$SAFARI_EXISTING_DIST" ]]; then
  SAFARI_DIST_APP=$(find "$SAFARI_EXISTING_DIST" -maxdepth 1 -name '*.app' -type d | head -1 || true)
  [[ -n "$SAFARI_DIST_APP" ]] || die "existing Safari Dist does not contain an app"
  verify_safari_app_resources "$SAFARI_DIST_APP" "existing Safari Dist resources"
fi

[[ $VALIDATE_ONLY -eq 1 ]] && { echo "==> validation passed"; exit 0; }

# Invalidate a same-name artifact from an earlier run before release preflight.
# Otherwise a failed signing attempt could leave a stale ZIP looking current.
if [[ $WITH_SAFARI -eq 1 && "$SAFARI_MODE" == "release" ]]; then
  rm -f "$DIST/safari-$PREFIX-macos.zip"
fi
if [[ $WITH_SAFARI -eq 1 ]]; then
  rm -rf "$EXT/safari-xcode/Dist"
fi

# Fail before creating any package when the default Safari release cannot meet
# the signing policy. The app and extension have distinct bundle identifiers;
# Xcode must resolve their configured manual provisioning profiles separately.
SAFARI_XCODE_PROJ=""
SAFARI_SCHEME=""
SAFARI_TEAM_ID=""
SAFARI_SIGNING_IDENTITY=""
if [[ $WITH_SAFARI -eq 1 ]]; then
  have xcodebuild || die "Xcode is required for Safari packaging (or pass --no-safari)"
  have xcrun || die "xcrun is required for Safari packaging (or pass --no-safari)"
  have codesign || die "codesign is required for Safari packaging (or pass --no-safari)"
  have ditto || die "ditto is required for Safari packaging (or pass --no-safari)"

  SAFARI_XCODE_PROJ=$(find "$EXT/safari-xcode" -maxdepth 2 -name '*.xcodeproj' | head -1 || true)
  [[ -n "$SAFARI_XCODE_PROJ" ]] || die "no Xcode project under extensions/safari-xcode"
  SAFARI_SCHEME=$(xcodebuild -list -project "$SAFARI_XCODE_PROJ" 2>/dev/null \
                  | grep '(macOS)' | sed 's/^[[:space:]]*//' | head -1)
  [[ -n "$SAFARI_SCHEME" ]] || die "no macOS scheme in $SAFARI_XCODE_PROJ"

  if [[ "$SAFARI_MODE" == "release" ]]; then
    have security || die "security is required to validate Safari release signing"
    SAFARI_TEAM_ID="${BIOMEM_APPLE_TEAM_ID:-}"
    SAFARI_SIGNING_IDENTITY="${BIOMEM_DEVELOPER_ID_APPLICATION:-}"
    [[ "$SAFARI_TEAM_ID" =~ ^[A-Z0-9]{10}$ ]] || \
      die "Safari release requires BIOMEM_APPLE_TEAM_ID (10 uppercase letters/digits)"
    [[ "$SAFARI_SIGNING_IDENTITY" =~ ^Developer\ ID\ Application:\ .+\ \([A-Z0-9]{10}\)$ ]] || \
      die "Safari release requires the full BIOMEM_DEVELOPER_ID_APPLICATION identity"
    [[ "$SAFARI_SIGNING_IDENTITY" == *"($SAFARI_TEAM_ID)" ]] || \
      die "Safari signing identity and team ID do not match"
    security find-identity -v -p codesigning 2>/dev/null \
      | grep -F -- "\"$SAFARI_SIGNING_IDENTITY\"" >/dev/null || \
      die "the requested Developer ID Application identity is not installed and valid"
    echo "==> Safari release signing inputs accepted (values redacted)"
  else
    echo "==> Safari development mode: output is ad-hoc signed and non-distributable"
  fi
fi

SAFARI_ARCH_ARGS=()
if [[ -n "$SAFARI_ARCHS" ]]; then
  SAFARI_ARCH_ARGS+=("ARCHS=$SAFARI_ARCHS" "ONLY_ACTIVE_ARCH=NO")
  echo "==> Safari architectures: $SAFARI_ARCHS"
fi

rm -rf "$STAGE"
mkdir -p "$DIST"

# ---------------------------------------------------------------- Chrome
echo "==> building Chromium package"
stage_copy "$CHROME_SRC" "$STAGE/chrome"
# strip CWS-only update_url; harmless for unpacked, wrong for a self-hosted crx
python3 - "$STAGE/chrome/manifest.json" <<'PY'
import json, sys
p = sys.argv[1]
m = json.load(open(p, encoding="utf-8"))
m.pop("update_url", None)
json.dump(m, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
PY
(cd "$STAGE/chrome" && zip -q -9 -X -r "$DIST/chrome-$PREFIX.zip" .)
echo "  -> dist/chrome-$PREFIX.zip ($(du -h "$DIST/chrome-$PREFIX.zip" | cut -f1))"

if [[ $WITH_CRX -eq 1 ]]; then
  KEY="$DIST/keys/chrome-$PREFIX.pem"
  if [[ ! -f "$KEY" ]]; then
    echo "  generating signing key: $KEY"
    mkdir -p "$(dirname "$KEY")"
    openssl genrsa -out "$KEY" 2048 2>/dev/null
    chmod 600 "$KEY"
  fi
  python3 "$ROOT/scripts/crx3_sign.py" pack \
    "$DIST/chrome-$PREFIX.zip" "$KEY" "$DIST/chrome-$PREFIX.crx"
  echo "  -> dist/chrome-$PREFIX.crx"
fi

# ---------------------------------------------------------------- Firefox
echo "==> building Firefox package"
stage_copy "$FIREFOX_SRC" "$STAGE/firefox"
# web-ext/AMO canonicalizes manifest.json with two-space JSON indentation and
# no trailing newline before signing. Build the tested AMO input in that exact
# form so the provider only adds signature metadata and cannot silently rewrite
# any application payload byte.
python3 - "$FIREFOX_SRC/manifest.json" "$STAGE/firefox/manifest.json" <<'PY'
import json
from pathlib import Path
import sys

source = Path(sys.argv[1])
staged = Path(sys.argv[2])
manifest = json.loads(source.read_text(encoding="utf-8"))
canonical = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
expected_source = canonical + b"\n"
if source.read_bytes() != expected_source:
    raise SystemExit(
        "Firefox source manifest must use canonical two-space JSON with one trailing newline"
    )
staged.write_bytes(canonical)
PY
# NOTE: META-INF/ from the previous build is excluded — its signatures
# cover the old files and would make Firefox refuse the modified extension.
rm -f "$DIST/firefox-$PREFIX.xpi"
(cd "$STAGE/firefox" && zip -q -9 -X -r "$DIST/firefox-$PREFIX-unsigned.xpi" .)
echo "  -> dist/firefox-$PREFIX-unsigned.xpi ($(du -h "$DIST/firefox-$PREFIX-unsigned.xpi" | cut -f1)); AMO SIGNING INPUT ONLY"

# ---------------------------------------------------------------- Safari
if [[ $WITH_SAFARI -eq 1 ]]; then
  echo "==> building Safari macOS app ($SAFARI_MODE)"
  if [[ "$SAFARI_MODE" == "release" ]]; then
    SAFARI_ZIP="$DIST/safari-$PREFIX-macos.zip"
    rm -f "$SAFARI_ZIP"
    xcodebuild -project "$SAFARI_XCODE_PROJ" -scheme "$SAFARI_SCHEME" -configuration Release \
               -derivedDataPath "$STAGE/safari-dd" \
               DEVELOPMENT_TEAM="$SAFARI_TEAM_ID" \
               CODE_SIGN_IDENTITY="$SAFARI_SIGNING_IDENTITY" \
               CODE_SIGN_STYLE=Manual "${SAFARI_ARCH_ARGS[@]}" build -quiet 2>&1 \
      | tail -20 || die "Safari release build failed; verify both target provisioning profiles"
    APP=$(find "$STAGE/safari-dd/Build/Products/Release" -maxdepth 1 -name '*.app' | head -1)
    [[ -n "$APP" ]] || die "built Safari release app not found"
    verify_safari_app_resources "$APP" "built Safari release resources"
    codesign --verify --deep --strict "$APP" >/dev/null 2>&1 || \
      die "Safari release signature verification failed"
    SIGNING_DETAILS=$(codesign -dv "$APP" 2>&1)
    grep -F "Authority=$SAFARI_SIGNING_IDENTITY" <<< "$SIGNING_DETAILS" >/dev/null || \
      die "Safari release was not signed by the requested Developer ID identity"
    grep -F "TeamIdentifier=$SAFARI_TEAM_ID" <<< "$SIGNING_DETAILS" >/dev/null || \
      die "Safari release was not signed by the requested team"

    # Refresh the in-tree signed copy and package it. Notarization/stapling is
    # intentionally a separate credentialed release step.
    rm -rf "$EXT/safari-xcode/Dist"
    mkdir -p "$EXT/safari-xcode/Dist"
    ditto "$APP" "$EXT/safari-xcode/Dist/$(basename "$APP")"
    ditto -c -k --sequesterRsrc --keepParent "$APP" "$SAFARI_ZIP"
    echo "  -> dist/safari-$PREFIX-macos.zip ($(du -h "$SAFARI_ZIP" | cut -f1)); notarization required"
  else
    SAFARI_ZIP="$DIST/safari-$PREFIX-macos-development-adhoc.zip"
    DEV_PACKAGE="$STAGE/safari-development-package"
    rm -f "$SAFARI_ZIP"
    xcodebuild -project "$SAFARI_XCODE_PROJ" -scheme "$SAFARI_SCHEME" -configuration Debug \
               -derivedDataPath "$STAGE/safari-dd" \
               CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual \
               "${SAFARI_ARCH_ARGS[@]}" build -quiet 2>&1 \
      | tail -20 || die "Safari development build failed"
    APP=$(find "$STAGE/safari-dd/Build/Products/Debug" -maxdepth 1 -name '*.app' | head -1)
    [[ -n "$APP" ]] || die "built Safari development app not found"
    verify_safari_app_resources "$APP" "built Safari development resources"
    SIGNING_DETAILS=$(codesign -dv "$APP" 2>&1)
    grep -F 'Signature=adhoc' <<< "$SIGNING_DETAILS" >/dev/null || \
      die "Safari development app is not ad-hoc signed"

    mkdir -p "$DEV_PACKAGE"
    ditto "$APP" "$DEV_PACKAGE/$(basename "$APP")"
    python3 - "$DEV_PACKAGE/DEVELOPMENT-ONLY.json" "$VERSION" <<'PY'
import json, sys
metadata = {
    "artifact_kind": "safari-development-adhoc",
    "distributable": False,
    "gatekeeper_ready": False,
    "notarized": False,
    "version": sys.argv[2],
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(metadata, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
    ditto -c -k --sequesterRsrc "$DEV_PACKAGE" "$SAFARI_ZIP"
    echo "  -> dist/safari-$PREFIX-macos-development-adhoc.zip ($(du -h "$SAFARI_ZIP" | cut -f1)); DEVELOPMENT ONLY"
  fi
fi

# A machine-readable boundary prevents development/signing inputs from being
# mistaken for normal-user downloads by release assembly or a human operator.
python3 - "$DIST" "$PREFIX" "$VERSION" "$WITH_CRX" "$WITH_SAFARI" "$SAFARI_MODE" <<'PY'
from hashlib import sha256
import json
from pathlib import Path
import sys

dist, prefix, version = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
with_crx, with_safari = sys.argv[4] == "1", sys.argv[5] == "1"
safari_mode = sys.argv[6]

def record(name, artifact_class, visibility, distributable, normal_install):
    path = dist / name
    if not path.is_file():
        raise SystemExit(f"expected browser artifact is missing: {name}")
    return {
        "artifact_class": artifact_class,
        "distributable": distributable,
        "installable_by_normal_users": normal_install,
        "name": name,
        "release_visibility": visibility,
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }

artifacts = [
    record(
        f"chrome-{prefix}.zip",
        "public_store_upload_input",
        "public_inspection",
        True,
        False,
    ),
    record(
        f"firefox-{prefix}-unsigned.xpi",
        "unsigned_store_signing_input",
        "ci_only",
        False,
        False,
    ),
]
if with_crx:
    artifacts.append(record(
        f"chrome-{prefix}.crx",
        "development_or_managed_crx",
        "ci_only",
        False,
        False,
    ))
if with_safari:
    if safari_mode == "development":
        artifacts.append(record(
            f"safari-{prefix}-macos-development-adhoc.zip",
            "safari_development_adhoc",
            "ci_only",
            False,
            False,
        ))
    else:
        artifacts.append(record(
            f"safari-{prefix}-macos.zip",
            "apple_signing_notarization_input",
            "ci_only",
            False,
            False,
        ))

value = {
    "artifacts": artifacts,
    "policy": {
        "ci_only_classes": [
            "apple_signing_notarization_input",
            "development_or_managed_crx",
            "safari_development_adhoc",
            "unsigned_store_signing_input",
        ],
        "normal_user_channels": ["chrome_web_store", "firefox_amo", "safari_public"],
        "public_release_rule": "only public_inspection or verified store/signed outputs",
    },
    "schema_version": 1,
    "version": version,
}
(dist / "browser-artifacts.json").write_text(
    json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
echo "  -> dist/browser-artifacts.json (release classification)"

echo "==> done"
echo "  install: Chrome/Edge chrome://extensions (Developer mode > Load unpacked > dist/.stage/chrome)"
[[ $WITH_CRX -eq 1 ]] && echo "  or drag dist/chrome-$PREFIX.crx onto chrome://extensions"
echo "  Firefox development: about:debugging#/runtime/this-firefox > Load Temporary Add-on > dist/firefox-$PREFIX-unsigned.xpi"
if [[ $WITH_SAFARI -eq 1 && "$SAFARI_MODE" == "release" ]]; then
  echo "  Safari: unpack the ZIP, notarize and staple the .app, then repackage it for distribution"
elif [[ $WITH_SAFARI -eq 1 ]]; then
  echo "  Safari development: unzip dist/safari-$PREFIX-macos-development-adhoc.zip (not distributable)"
fi
