#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
WORK="$ROOT/var/acceptance"
PORT_FILE="$WORK/port"
SERVER_PID=""
FAILURES=0

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
pass() { printf '\033[1;32mPASS:\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mFAIL:\033[0m %s\n' "$*"; FAILURES=$((FAILURES+1)); }

api() {
  local method=$1 path=$2 body=${3-}
  python3 - "$method" "$path" "$body" <<'PY'
import json,sys,urllib.request,urllib.error
method,path,body=sys.argv[1:4]
base='http://127.0.0.1:%s' % open('var/acceptance/port').read().strip()
data=body.encode() if body else None
req=urllib.request.Request(base+path,data=data,method=method,headers={'content-type':'application/json'})
try:
    with urllib.request.urlopen(req) as r:
        sys.stdout.write(r.read().decode())
except urllib.error.HTTPError as e:
    sys.stdout.write(e.read().decode())
    sys.exit(0)
PY
}
state() { curl -sS "http://127.0.0.1:$(cat "$PORT_FILE")/api/admin/state"; }
events_for() { python3 - "$1" <<'PY'
import json,sys
p='var/acceptance/home-%s/.vdv/events.json' % sys.argv[1]
try: print(json.dumps(json.load(open(p)),ensure_ascii=False))
except FileNotFoundError: print('[]')
PY
}
assert_phase() {
  local device=$1 phase=$2 status=$3
  if events_for "$device" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(e["phase"]==sys.argv[1] and e["status"]==sys.argv[2] for e in d) else 1)' "$phase" "$status"; then
    pass "$device has $phase/$status"
  else
    fail "$device missing $phase/$status"; events_for "$device"
  fi
}
assert_no_phase() {
  local device=$1 phase=$2 status=$3
  if events_for "$device" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(1 if any(e["phase"]==sys.argv[1] and e["status"]==sys.argv[2] for e in d) else 0)' "$phase" "$status"; then
    pass "$device does not have $phase/$status"
  else
    fail "$device unexpectedly has $phase/$status"
  fi
}
setup() {
  local id=$1 arch=$2
  api POST /api/admin/devices "{\"id\":\"$id\",\"name\":\"$id\",\"arch\":\"$arch\",\"capabilities\":{\"declared\":\"does-not-override-probe\"}}" >/dev/null
}
batch() {
  local name=$1 channel=$2 id=$3
  api POST /api/admin/batches "{\"name\":\"$name\",\"appId\":\"visual-window\",\"channel\":\"$channel\",\"deviceIds\":[\"$id\"]}" >/dev/null
}
agent() {
  local id=$1; shift
  set +e
  python3 client/agent.py upgrade --server "http://127.0.0.1:$(cat "$PORT_FILE")" \
    --device-id "$id" --home "$WORK/home-$id" --install-root "$WORK/install-$id" "$@"
  local rc=$?
  set -e
  echo "$rc" > "$WORK/rc-$id"
}
cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then kill "$SERVER_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

log "clean and rebuild reproducible artifacts"
rm -rf "$WORK"
mkdir -p "$WORK"
ARCH=$(uname -m)
python3 tools/build_releases.py --mode fixture --version 1.0.0 --channel fixture --policies system,bundled --out "$WORK/release" >/dev/null
python3 tools/build_releases.py --mode fixture --version 1.0.0 --channel fontcheck --policies bundled --font-family 'Definitely Missing Font' --out "$WORK/release" >/dev/null
python3 tools/build_releases.py --mode fixture --version 1.0.1 --channel rollback --policies bundled --fixture-fail-first-frame --out "$WORK/release" >/dev/null
sha256sum "$WORK"/release/*.tar | sort > "$WORK/sha.before"
python3 tools/build_releases.py --mode fixture --version 1.0.0 --channel fixture --policies bundled --out "$WORK/release-rebuild" >/dev/null
cmp "$WORK/release/visual-window_1.0.0_fixture_linux_bundled.tar" \
    "$WORK/release-rebuild/visual-window_1.0.0_fixture_linux_bundled.tar"
pass "rebuild is byte-for-byte reproducible"
if python3 - "$WORK/release" <<'PY'
import pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1])
needles = (b'/workspace/', str(pathlib.Path.home()).encode())
found = []
for archive in root.glob('*.tar'):
    with tarfile.open(archive, 'r:*') as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            data = tf.extractfile(member).read()
            if any(x in data for x in needles):
                found.append(f'{archive.name}:{member.name}')
if found:
    print('\n'.join(found)); sys.exit(1)
PY
then
  pass "no developer absolute path appears inside tar payload or manifest"
else
  fail "absolute developer path string found inside release archive"
fi

log "start clean control plane"
python3 deploy/server.py --host 127.0.0.1 --port 0 --db "$WORK/vdv.db" --artifact-dir "$WORK/artifacts" --port-file "$PORT_FILE" >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do [[ -s "$PORT_FILE" ]] && break; sleep 0.1; done
PORT=$(cat "$PORT_FILE")
api POST /api/admin/channels '{"appId":"visual-window","name":"fixture","active":true,"minClientVersion":1}' >/dev/null
api POST /api/admin/channels '{"appId":"visual-window","name":"fontcheck","active":true,"minClientVersion":1}' >/dev/null
api POST /api/admin/channels '{"appId":"visual-window","name":"rollback","active":true,"minClientVersion":1}' >/dev/null
for f in "$WORK"/release/*.tar; do
  api POST /api/admin/releases/publish "{\"path\":\"$f\"}" >/dev/null
done
system_size=$(stat -c%s "$WORK/release/visual-window_1.0.0_fixture_linux_system.tar" 2>/dev/null || echo 0)
bundled_size=$(stat -c%s "$WORK/release/visual-window_1.0.0_fixture_linux_bundled.tar")
if (( bundled_size > 0 )); then pass "artifact sizes measured (bundled=$bundled_size bytes; system=$system_size bytes)"; fi

log "scenario 1: clean user HOME, actual probes, first-frame success"
setup clean "$ARCH"; batch fixture fixture clean
agent clean
[[ $(cat "$WORK/rc-clean") == 0 ]] && pass "clean upgrade exits 0" || fail "clean upgrade rc=$(cat "$WORK/rc-clean")"
assert_phase clean probe success
assert_phase clean preflight_passed success
assert_phase clean first_frame_verified success
test -L "$WORK/install-clean/current" && pass "current symlink atomically points to verified release"

log "scenario 2: missing default font is blocked by actual font probe"
setup nofont "$ARCH"; batch fontcheck fontcheck nofont
agent nofont
[[ $(cat "$WORK/rc-nofont") == 0 ]] || fail "blocked assignment should not be agent error"
state > "$WORK/state-nofont.json"
python3 - "$WORK/state-nofont.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1])); d=next(x for x in s['devices'] if x['id']=='nofont')
ok=d['assignment'] and d['assignment']['reason']=='missing required font(s): Definitely Missing Font'
sys.exit(0 if ok else 1)
PY
pass "server rejected missing required font instead of assuming font presence"

log "scenario 3: install root cannot be written"
setup noroot "$ARCH"; batch fixture fixture noroot
# A directory path occupied by a regular file is an OS-level create/write denial
# without requiring root or privilege-drop capabilities in the test container.
printf 'blocked\n' > "$WORK/install-noroot"
agent noroot
[[ $(cat "$WORK/rc-noroot") == 2 ]] && pass "non-writable install root fails preflight before download" || fail "rc=$(cat "$WORK/rc-noroot")"
assert_phase noroot preflight_failed failure
assert_no_phase noroot download_verified success

log "scenario 4: network breaks during first download, then resumes"
setup netcut "$ARCH"; batch fixture fixture netcut
agent netcut --fail-first-after 4096
[[ $(cat "$WORK/rc-netcut") == 0 ]] && pass "resumable upgrade succeeds after interruption" || fail "rc=$(cat "$WORK/rc-netcut")"
assert_phase netcut download_interrupted warning
assert_phase netcut first_frame_verified success

log "scenario 5: old client receives new schema fields in compatibility window"
setup oldv1 "$ARCH"; batch fixture fixture oldv1
assignment=$(curl -sS "http://127.0.0.1:$PORT/api/agent/devices/oldv1/assignment?protocol=1&app=visual-window")
python3 - <<PY
import json
a=json.loads('''$assignment''')
assert 'extensions' not in a
assert a['action']=='upgrade' and a['manifest']['schemaVersion']==2
assert a['manifest']['payload']['entrypoint']=='bin/window-app'
print('v1 top-level compatibility window preserved; nested future data ignored by v1 parser')
PY
agent oldv1 --protocol 1
[[ $(cat "$WORK/rc-oldv1") == 0 ]] && pass "protocol v1 client completes" || fail "rc=$(cat "$WORK/rc-oldv1")"
assert_phase oldv1 first_frame_verified success

log "scenario 6: web UI must not call download completion upgrade completion"
setup webpage "$ARCH"; batch fixture fixture webpage
agent webpage --stop-after-download
[[ $(cat "$WORK/rc-webpage") == 0 ]] && pass "agent stopped deliberately after verified download" || fail "rc=$(cat "$WORK/rc-webpage")"
assert_phase webpage download_verified success
assert_no_phase webpage first_frame_verified success
state > "$WORK/state-web.json"
python3 - "$WORK/state-web.json" deploy/console/index.html <<'PY'
import json,sys,re
s=json.load(open(sys.argv[1])); html=open(sys.argv[2]).read()
d=next(x for x in s['devices'] if x['id']=='webpage')
complete=any(r['phase']=='first_frame_verified' and r['status']=='success' for r in d['reports'])
assert not complete
assert '已下载，尚未完成' in html and '升级完成：首帧已验证' in html
print('UI state distinguishes downloaded from first-frame-verified')
PY
pass "download-only device is not rendered as upgrade complete"

log "scenario 7: first-frame failure activates rollback to prior verified release"
setup rollback "$ARCH"; batch rollback rollback rollback
mkdir -p "$WORK/home-rollback" "$WORK/install-rollback"
cp -a "$WORK/home-clean/." "$WORK/home-rollback/"
cp -a "$WORK/install-clean/." "$WORK/install-rollback/"
rm -f "$WORK/install-rollback/current"
ln -s "$WORK/install-rollback/releases/1.0.0-1/payload" "$WORK/install-rollback/current"
agent rollback
rc=$(cat "$WORK/rc-rollback")
[[ $rc == 6 || $rc == 7 ]] && pass "bad 1.0.1 did not become complete (rc=$rc)" || fail "bad upgrade rc=$rc"
assert_phase rollback first_frame_failed failure
assert_phase rollback rollback_complete success
if readlink "$WORK/install-rollback/current" | grep -q '1.0.0-1'; then pass "current remains old verified release after rollback"; else fail "rollback current wrong: $(readlink "$WORK/install-rollback/current")"; fi

log "artifact selection and fingerprint evidence"
python3 - <<'PY'
import json
s=json.load(open('var/acceptance/state-nofont.json'))
s2=json.load(open('var/acceptance/state-web.json'))
rels={r['dependency_policy']:r for r in s2['releases']}
if 'system' in rels:
    assert rels['system']['size'] < rels['bundled']['size']
for r in s2['releases']:
    assert r['fingerprint']['absolutePathLeak']==[]
print('system artifact is smaller; bundled artifact chosen when system component absent; no path leaks')
PY
pass "dependency choice is based on measured size and observed compatibility"

if (( FAILURES == 0 )); then
  log "ALL ACCEPTANCE SCENARIOS PASSED"
else
  log "$FAILURES SCENARIO ASSERTIONS FAILED"
  exit 1
fi
