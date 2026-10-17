#!/usr/bin/env bash
# Testira state-mašinu auto-pauze Android aplikacije (Recorder.java) BEZ telefona:
# kompajlira pravi Recorder.java sa stubom za android.util.Log i pušta sintetičke signale.
#
#   bash scripts/test_recorder_logic.sh [putanja/do/projekta]
#
# Pokriva klase grešaka koje su stvarno prijavljivane: lažna detekcija šuma (R talas),
# zaglavljivanje u pauzi iako je signal dobar, pauza pri slabom kontaktu.
set -euo pipefail

PROJ="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../android" && pwd)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${TMPDIR:-/tmp}/recorder_logic_test.$$"

mkdir -p "$WORK/src/org/ecgrecorder" "$WORK/src/android/util" "$WORK/out"
cp "$PROJ/app/src/main/java/org/ecgrecorder/Recorder.java" "$WORK/src/org/ecgrecorder/"
cp "$HERE/recorder_test/Log.java" "$WORK/src/android/util/"
cp "$HERE/recorder_test/TestRec.java" "$WORK/src/"

if ! command -v javac >/dev/null; then echo "NEMA javac (JDK) — test se ne može pokrenuti"; exit 2; fi
(cd "$WORK" && javac -d out src/android/util/Log.java src/org/ecgrecorder/Recorder.java src/TestRec.java)
echo "kompajlirano — puštam scenario (~40 s realnog vremena, aplikacija meri zidni sat)"
cd "$WORK" && java -cp out TestRec
rc=$?
echo "radni folder: $WORK"
exit $rc
