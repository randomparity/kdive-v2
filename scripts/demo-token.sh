#!/usr/bin/env bash
# Mint a bearer token from the bundled-demo mock-OIDC issuer and print it to stdout:
#
#   export KDIVE_TOKEN=$(scripts/demo-token.sh)                 # full admin grant (default)
#   export KDIVE_TOKEN=$(scripts/demo-token.sh --role viewer)   # narrowed, for testing a denial
#
# --role {admin|operator|viewer} selects how much authority the token carries, so you can
# demonstrate an RBAC *denial* without redeploying the chart. The bundled issuer maps a
# per-role client_id to a narrower claim set (deploy/helm/kdive/templates/demo/oidc.yaml):
#   admin     (default) admin on project `demo` + all three platform roles — reaches every tool
#   operator  operator on project `demo`, NO platform roles — denied admin + platform-ops tools
#   viewer    viewer on project `demo`, NO platform roles — read-only; mutations are denied
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

usage() {
  echo "usage: demo-token.sh [--role admin|operator|viewer]" >&2
}

role="admin"
while [[ $# -gt 0 ]]; do
  case "$1" in
  --role)
    [[ $# -ge 2 ]] || {
      usage
      exit 2
    }
    role="$2"
    shift 2
    ;;
  --role=*)
    role="${1#--role=}"
    shift
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "unknown argument: $1" >&2
    usage
    exit 2
    ;;
  esac
done

# The catch-all client_id (kdive-demo) gets the full admin grant; per-role client_ids map to
# the narrowed variants the chart registers. Keep these in lockstep with oidc.yaml.
case "$role" in
admin) client_id="kdive-demo" ;;
operator) client_id="kdive-demo-operator" ;;
viewer) client_id="kdive-demo-viewer" ;;
*)
  echo "invalid --role '${role}' (want: admin|operator|viewer)" >&2
  exit 2
  ;;
esac

namespace="${KDIVE_DEMO_NAMESPACE:-kdive-demo}"
fullname="${KDIVE_DEMO_FULLNAME:-kdive-kdive}"

kube=(kubectl)
if [[ -n "${KDIVE_DEMO_CONTEXT:-}" ]]; then
  kube+=(--context "${KDIVE_DEMO_CONTEXT}")
fi
kube+=(-n "${namespace}")

# The OIDC URL and client_id are passed as argv so the Python body needs no shell interpolation.
mint='import json, sys, urllib.request as u
data = ("grant_type=client_credentials&client_id=%s&client_secret=x" % sys.argv[2]).encode()
req = u.Request(sys.argv[1], data=data)
print(json.load(u.urlopen(req, timeout=10))["access_token"])'

exec "${kube[@]}" exec "deploy/${fullname}-server" -- \
  python -c "${mint}" "http://${fullname}-oidc:8080/default/token" "${client_id}"
