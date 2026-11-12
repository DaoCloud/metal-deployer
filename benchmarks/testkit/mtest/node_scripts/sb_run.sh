#!/bin/bash
# S6 SuperBench, run inside the sbcli image on the rank 0 host of a group (host network).
# mtest mounts /mtest with: inventory (Ansible, name = mgmt IP for MPI, ansible_host = LOM IP)
# and id_ed25519 (a key added to every host only for this run). Results go to /results/sb.
#
# Env: MTEST_SB_CONFIG      config file name, e.g. config_gb300_multi_node.yaml
#      MTEST_SB_CONFIG_DIR  where configs and rules are (default /workspace in the sbcli image)
#      MTEST_SB_RULE        rule file name for "sb result summary"
#      MTEST_SB_WORKER      worker image already present on every host (sb deploy --no-image-pull)
#      MTEST_SB_INCLUDE / MTEST_SB_EXCLUDE   comma-separated benchmark names (empty: all enabled ones)
#      MTEST_SB_OVERRIDES   space separated extra key=value config overrides
# Exit: 0 when sb produced results (benchmark failures are judged by mtest), 2 otherwise.
set -u
OUT=/results/sb
CFG_DIR=${MTEST_SB_CONFIG_DIR:-/workspace}
CFG="$CFG_DIR/$MTEST_SB_CONFIG"
INV=/mtest/inventory
mkdir -p "$OUT"
[ -f "$CFG" ] || { echo "config not found: $CFG"; exit 2; }
cp "$CFG" "$OUT/"

# Benchmark selection: rewrite superbench.enable from include/exclude.
ENABLE=$(python3 - "$CFG" <<'EOF'
import os, sys, yaml
with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f)
enabled = cfg["superbench"].get("enable") or list(cfg["superbench"].get("benchmarks", {}))
inc = [x for x in os.environ.get("MTEST_SB_INCLUDE", "").split(",") if x]
exc = set(x for x in os.environ.get("MTEST_SB_EXCLUDE", "").split(",") if x)
sel = [b for b in enabled if (not inc or b in inc) and b not in exc]
unknown = [b for b in inc if b not in enabled]
if unknown:
    sys.stderr.write("benchmarks not in {}: {}\n".format(os.path.basename(sys.argv[1]), ", ".join(unknown)))
    sys.exit(1)
print("[" + ",".join('"%s"' % b for b in sel) + "]" if (inc or exc) else "")
EOF
) || exit 2
OVERRIDE=()
VALUES=()
[ -n "$ENABLE" ] && VALUES+=("superbench.enable=$ENABLE")
# MTEST_SB_OVERRIDES: space separated key=value, e.g. the ib-traffic pairing file made by mtest.
for v in ${MTEST_SB_OVERRIDES:-}; do VALUES+=("$v"); done
[ ${#VALUES[@]} -gt 0 ] && OVERRIDE=(--config-override "${VALUES[@]}")
echo "config $MTEST_SB_CONFIG ${VALUES[*]}"
echo "hosts: $(grep -c ansible_host "$INV")"

echo "== sb deploy"
sb deploy -f "$INV" --no-image-pull -i "$MTEST_SB_WORKER" -o "$OUT/deploy" || { echo "sb deploy failed"; exit 2; }
echo "== sb run"
sb run -f "$INV" --config-file "$CFG" --output-dir "$OUT/run" --get-info "${OVERRIDE[@]}"
echo $? > "$OUT/run.rc"
if [ -n "${MTEST_SB_RULE:-}" ] && [ -s "$OUT/run/results-summary.jsonl" ]; then
    sb result summary --data-file "$OUT/run/results-summary.jsonl" --rule-file "$CFG_DIR/$MTEST_SB_RULE" \
        --output-file-format md --output-dir "$OUT/summary" > "$OUT/summary.log" 2>&1 || true
fi
[ -s "$OUT/run/results-summary.jsonl" ] || { echo "sb run produced no results-summary.jsonl"; exit 2; }
echo "done: sb run exit $(cat "$OUT/run.rc")"
exit 0
