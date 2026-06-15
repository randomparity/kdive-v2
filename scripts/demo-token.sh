#!/usr/bin/env bash
# Mint a bearer token from the bundled-demo mock-OIDC issuer and print it to stdout:
#
#   export KDIVE_TOKEN=$(scripts/demo-token.sh)
#
# The token is minted INSIDE the cluster (kubectl exec into the server pod) so its `iss`
# matches the in-cluster issuer the server validates against — a port-forwarded mint stamps
# the wrong issuer and the server 401s. Tokens are short-lived (~1h); re-run when one expires
# and reconnect the MCP client.
#
# DEMO ONLY: the bundled issuer mints a valid `aud=kdive` token for ANY caller. Never run this
# against a real deployment; production supplies its own OIDC token via $KDIVE_TOKEN.
#
# Env overrides:
#   KDIVE_DEMO_NAMESPACE  release namespace      (default: kdive-demo)
#   KDIVE_DEMO_FULLNAME   chart fullname         (default: kdive-kdive, i.e. <release>-kdive)
#   KDIVE_DEMO_CONTEXT    kube context           (default: current context)
set -euo pipefail

namespace="${KDIVE_DEMO_NAMESPACE:-kdive-demo}"
fullname="${KDIVE_DEMO_FULLNAME:-kdive-kdive}"

kube=(kubectl)
if [[ -n "${KDIVE_DEMO_CONTEXT:-}" ]]; then
  kube+=(--context "${KDIVE_DEMO_CONTEXT}")
fi
kube+=(-n "${namespace}")

# The OIDC URL is passed as argv[1] so the Python body needs no shell interpolation.
mint='import json, sys, urllib.request as u
data = b"grant_type=client_credentials&client_id=kdive-demo&client_secret=x"
req = u.Request(sys.argv[1], data=data)
print(json.load(u.urlopen(req, timeout=10))["access_token"])'

exec "${kube[@]}" exec "deploy/${fullname}-server" -- \
  python -c "${mint}" "http://${fullname}-oidc:8080/default/token"
