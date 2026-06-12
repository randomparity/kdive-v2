# Easier Install — PR2: Turnkey Demo Path — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken Bitnami-subchart demo with first-party, ephemeral, in-chart Postgres + MinIO + mock-OIDC so `helm install -f values-demo.yaml` followed by `helm test kdive` reaches an authenticated `tools/list` with no external backends, no IdP, and no hand-built image.

**Architecture:** Drop both subchart dependencies; render Postgres/MinIO/mock-OIDC as first-party templates under `templates/demo/`, gated by the existing `bundledBackends` + `demoAcknowledged` flags. Compute the DB/S3/OIDC URLs from the in-chart service names. A render-time gate forces `service.type=ClusterIP` on the demo path (the bundled issuer mints valid `aud=kdive` tokens, so an exposed MCP is an open control plane). A `helm test` pod proves the stack via the session-negotiating MCP client.

**Tech Stack:** Helm templates/`_helpers.tpl`, `mock-oauth2-server` `JSON_CONFIG`, MinIO + `mc`, the in-image `fastmcp` client; pytest render tests shelling out to `helm template`/`helm lint`.

This is Workstream B of `docs/superpowers/specs/2026-06-12-easier-install-design.md`. **It depends on PR1** — the demo pulls the published `:edge` image. Service names below use `{{ include "kdive.fullname" . }}` = `kdive-kdive` (release `kdive`).

---

### Task 1: ADR record

Spec B preamble: ADR-0088 is `Status: Proposed`, so amend decision 7 in place (the supersede-don't-edit convention is for *accepted* decisions). Only write a superseding ADR-0097 if 0088 has since been accepted.

**Files:**
- Modify (conditional): `docs/adr/0088-*.md`, OR Create: `docs/adr/0097-in-chart-demo-backends.md`

- [ ] **Step 1: Check 0088's status.**

Run: `grep -m1 '^- \*\*Status:\*\*' docs/adr/0088-*.md`
Expected: shows `Proposed` or `Accepted`.

- [ ] **Step 2a (if `Proposed`): amend decision 7 in place.** Edit `docs/adr/0088-*.md` decision 7 to read (plain, factual language):

```markdown
7. **Bundled backends are first-party, in-chart, and ephemeral.** `bundledBackends=true`
   (co-set with `demoAcknowledged=true`) renders first-party Postgres, MinIO, and a
   mock-OIDC issuer as in-chart Deployments on `emptyDir` — a pod restart drops all state by
   design. It is demo-only: the issuer mints valid `aud=kdive` tokens for any caller, so a
   render-time gate forces `service.type=ClusterIP` (reach MCP via `kubectl port-forward`).
   This replaces the earlier Bitnami-subchart approach, whose Docker Hub images were retired
   in 2025 and which shipped no OIDC issuer.
```

- [ ] **Step 2b (only if `Accepted`): instead** create `docs/adr/0097-in-chart-demo-backends.md` (Status: Proposed) stating the same decision and "Supersedes ADR-0088 decision 7", and add a "Superseded in part by ADR-0097" note on 0088 decision 7.

- [ ] **Step 3: Commit.**

```bash
git add docs/adr/
git commit -m "docs(adr): in-chart demo backends (amend ADR-0088 decision 7)"
```

---

### Task 2: Drop the subcharts; bump the chart version

Spec B1.

**Files:**
- Modify: `deploy/helm/kdive/Chart.yaml:5` (version), `:8-22` (remove `dependencies`)
- Delete: `deploy/helm/kdive/Chart.lock`
- Modify: `.github/workflows/ci.yml:92-95` (remove "Helm chart deps")

- [ ] **Step 1: Edit `Chart.yaml`** — bump `version` and delete the `dependencies:` block and the Bitnami comment above it. The file becomes:

```yaml
apiVersion: v2
name: kdive
description: kdive control plane (server/worker/reconciler)
type: application
version: 0.2.0
appVersion: "0.3.0"
```

- [ ] **Step 2: Delete the lockfile.**

Run: `git rm deploy/helm/kdive/Chart.lock`
Expected: removes the file.

- [ ] **Step 3: Remove the `Helm chart deps` step from `.github/workflows/ci.yml`** (lines 92-95, the `helm dependency build` step) — it no longer applies with no dependencies.

- [ ] **Step 4: Confirm the chart still renders the external path without subcharts.**

Run: `helm template kdive deploy/helm/kdive --set config.KDIVE_DATABASE_URL=postgresql://x/y >/dev/null && echo OK`
Expected: `OK` (no "missing dependencies" error).

- [ ] **Step 5: Commit.**

```bash
git add deploy/helm/kdive/Chart.yaml .github/workflows/ci.yml
git commit -m "build(helm): drop Bitnami subcharts, bump chart version to 0.2.0"
```

---

### Task 3: Demo values (drop dead blocks, add demo image pins)

Spec B1 + B2.

**Files:**
- Modify: `deploy/helm/kdive/values.yaml:72-88` (remove dead `postgresql:`/`minio:` subchart-override blocks; add `demo:` image pins)

- [ ] **Step 1: Delete the dead subchart-override blocks** `postgresql:` (lines 72-79) and `minio:` (lines 80-88) — they only configured the now-removed subcharts. Keep `demoCredentials`.

- [ ] **Step 2: Add a `demo:` block** (image pins for the first-party demo backends) where the `postgresql:`/`minio:` blocks were:

```yaml
# Image pins for the first-party demo backends (bundledBackends path only).
demo:
  postgres:
    image: postgres:17
  minio:
    image: minio/minio:RELEASE.2025-04-22T22-12-26Z
  mc:
    image: minio/mc:RELEASE.2025-04-16T18-13-26Z
  oidc:
    image: ghcr.io/navikt/mock-oauth2-server:3.0.3
```

- [ ] **Step 3: Commit.**

```bash
git add deploy/helm/kdive/values.yaml
git commit -m "build(helm): replace dead subchart overrides with demo image pins"
```

---

### Task 4: Helpers — service-derived URLs + the ClusterIP gate

Spec B3 (helpers) + the demo access-boundary gate. The render tests in Task 10 verify this.

**Files:**
- Modify: `deploy/helm/kdive/templates/_helpers.tpl:25-42` (databaseUrl/s3Endpoint), add `oidcIssuer`/`oidcJwks`, `:119-123` (validateValues gate)

- [ ] **Step 1: Replace the `kdive.databaseUrl` and `kdive.s3Endpoint` helpers** (lines 25-42) so the bundled path points at the in-chart services:

```gotemplate
{{- define "kdive.databaseUrl" -}}
{{- if .Values.bundledBackends -}}
{{- $c := .Values.demoCredentials.postgresql -}}
{{- printf "postgresql://%s:%s@%s-postgres:5432/%s" $c.username $c.password (include "kdive.fullname" .) $c.database -}}
{{- else -}}
{{- .Values.config.KDIVE_DATABASE_URL -}}
{{- end -}}
{{- end -}}

{{- define "kdive.s3Endpoint" -}}
{{- if .Values.bundledBackends -}}
{{- printf "http://%s-minio:9000" (include "kdive.fullname" .) -}}
{{- else -}}
{{- .Values.config.KDIVE_S3_ENDPOINT_URL -}}
{{- end -}}
{{- end -}}

{{- define "kdive.oidcIssuer" -}}
{{- if .Values.bundledBackends -}}
{{- printf "http://%s-oidc:8080/default" (include "kdive.fullname" .) -}}
{{- else -}}
{{- .Values.config.KDIVE_OIDC_ISSUER -}}
{{- end -}}
{{- end -}}

{{- define "kdive.oidcJwks" -}}
{{- if .Values.bundledBackends -}}
{{- printf "http://%s-oidc:8080/default/jwks" (include "kdive.fullname" .) -}}
{{- else -}}
{{- .Values.config.KDIVE_OIDC_JWKS_URI -}}
{{- end -}}
{{- end -}}
```

- [ ] **Step 2: Add the ClusterIP gate to `kdive.validateValues`** (currently lines 119-123). Replace it with:

```gotemplate
{{- define "kdive.validateValues" -}}
{{- if and .Values.bundledBackends (not .Values.demoAcknowledged) -}}
{{- fail "bundledBackends is ephemeral/demo-only: set demoAcknowledged=true to use it (data is NOT durable)" -}}
{{- end -}}
{{- if and .Values.bundledBackends (ne (.Values.service.type | toString) "ClusterIP") -}}
{{- fail "bundledBackends is demo-only and its issuer mints valid kdive tokens for any caller: service.type must stay ClusterIP (reach MCP via `kubectl port-forward`). Expose MCP only on the external-backend path, behind a real IdP." -}}
{{- end -}}
{{- end -}}
```

- [ ] **Step 3: Verify the gate fires.**

Run: `helm template kdive deploy/helm/kdive --set bundledBackends=true --set demoAcknowledged=true --set service.type=NodePort 2>&1 | grep -o 'service.type must stay ClusterIP' | head -1`
Expected: prints `service.type must stay ClusterIP`.

- [ ] **Step 4: Commit.**

```bash
git add deploy/helm/kdive/templates/_helpers.tpl
git commit -m "feat(helm): derive demo DB/S3/OIDC URLs from in-chart services; ClusterIP gate"
```

---

### Task 5: ConfigMap — compute the OIDC issuer/JWKS

Spec B3. `KDIVE_OIDC_ISSUER`/`KDIVE_OIDC_JWKS_URI` default empty in `config`, so on the bundled path they must be computed (like the DB/S3 URLs), not passed through empty.

**Files:**
- Modify: `deploy/helm/kdive/templates/configmap.yaml:22-30`

- [ ] **Step 1: Exclude the OIDC keys from the passthrough `range` and emit them computed.** Replace the `data:` body's `range` + computed block (lines 21-30):

```gotemplate
data:
  {{- range $k, $v := .Values.config }}
  {{- if not (or (eq $k "KDIVE_DATABASE_URL") (eq $k "KDIVE_S3_ENDPOINT_URL") (eq $k "KDIVE_OIDC_ISSUER") (eq $k "KDIVE_OIDC_JWKS_URI")) }}
  {{ $k }}: {{ $v | quote }}
  {{- end }}
  {{- end }}
  # Computed: bundled path derives these from the in-release services; external path
  # passes .Values.config.* through (see the kdive.* helpers).
  KDIVE_DATABASE_URL: {{ include "kdive.databaseUrl" . | quote }}
  KDIVE_S3_ENDPOINT_URL: {{ include "kdive.s3Endpoint" . | quote }}
  KDIVE_OIDC_ISSUER: {{ include "kdive.oidcIssuer" . | quote }}
  KDIVE_OIDC_JWKS_URI: {{ include "kdive.oidcJwks" . | quote }}
```

- [ ] **Step 2: Verify external-path OIDC still passes through.**

Run: `helm template kdive deploy/helm/kdive --set config.KDIVE_DATABASE_URL=postgresql://x/y --set config.KDIVE_OIDC_ISSUER=https://idp.example | grep 'KDIVE_OIDC_ISSUER:'`
Expected: `KDIVE_OIDC_ISSUER: "https://idp.example"`.

- [ ] **Step 3: Verify bundled-path OIDC is computed.**

Run: `helm template kdive deploy/helm/kdive --set bundledBackends=true --set demoAcknowledged=true | grep 'KDIVE_OIDC_ISSUER:'`
Expected: `KDIVE_OIDC_ISSUER: "http://kdive-kdive-oidc:8080/default"`.

- [ ] **Step 4: Commit.**

```bash
git add deploy/helm/kdive/templates/configmap.yaml
git commit -m "feat(helm): compute demo OIDC issuer/JWKS into the config ConfigMap"
```

---

### Task 6: Demo backend templates (postgres, minio, oidc, networkpolicy)

Spec B2 + the access-boundary NetworkPolicy.

**Files:**
- Create: `deploy/helm/kdive/templates/demo/postgres.yaml`, `minio.yaml`, `oidc.yaml`, `networkpolicy.yaml`

- [ ] **Step 1: Create `templates/demo/postgres.yaml`:**

```gotemplate
{{- if .Values.bundledBackends }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "kdive.fullname" . }}-postgres
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ include "kdive.fullname" . }}-postgres
  template:
    metadata:
      labels:
        app: {{ include "kdive.fullname" . }}-postgres
        {{- include "kdive.labels" . | nindent 8 }}
    spec:
      containers:
        - name: postgres
          image: {{ .Values.demo.postgres.image }}
          env:
            - name: POSTGRES_USER
              value: {{ .Values.demoCredentials.postgresql.username | quote }}
            - name: POSTGRES_PASSWORD
              value: {{ .Values.demoCredentials.postgresql.password | quote }}
            - name: POSTGRES_DB
              value: {{ .Values.demoCredentials.postgresql.database | quote }}
          ports:
            - containerPort: 5432
          readinessProbe:
            exec:
              command: ["pg_isready", "-U", {{ .Values.demoCredentials.postgresql.username | quote }}, "-d", {{ .Values.demoCredentials.postgresql.database | quote }}]
            periodSeconds: 5
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
      volumes:
        - name: data
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "kdive.fullname" . }}-postgres
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    app: {{ include "kdive.fullname" . }}-postgres
  ports:
    - port: 5432
      targetPort: 5432
{{- end }}
```

- [ ] **Step 2: Create `templates/demo/minio.yaml`** (Deployment + Service + bucket-create Job whose `mc alias set` retry-loop waits for MinIO readiness — spec B2 / review finding 4):

```gotemplate
{{- if .Values.bundledBackends }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "kdive.fullname" . }}-minio
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ include "kdive.fullname" . }}-minio
  template:
    metadata:
      labels:
        app: {{ include "kdive.fullname" . }}-minio
        {{- include "kdive.labels" . | nindent 8 }}
    spec:
      containers:
        - name: minio
          image: {{ .Values.demo.minio.image }}
          args: ["server", "/data", "--console-address", ":9001"]
          env:
            - name: MINIO_ROOT_USER
              value: {{ .Values.demoCredentials.minio.rootUser | quote }}
            - name: MINIO_ROOT_PASSWORD
              value: {{ .Values.demoCredentials.minio.rootPassword | quote }}
          ports:
            - containerPort: 9000
          readinessProbe:
            httpGet:
              path: /minio/health/ready
              port: 9000
            periodSeconds: 5
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "kdive.fullname" . }}-minio
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    app: {{ include "kdive.fullname" . }}-minio
  ports:
    - port: 9000
      targetPort: 9000
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "kdive.fullname" . }}-minio-init
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
spec:
  backoffLimit: 10
  template:
    metadata:
      labels:
        {{- include "kdive.labels" . | nindent 8 }}
    spec:
      restartPolicy: OnFailure
      containers:
        - name: mc
          image: {{ .Values.demo.mc.image }}
          command:
            - /bin/sh
            - -c
            - |
              set -e
              until mc alias set local http://{{ include "kdive.fullname" . }}-minio:9000 "$MC_USER" "$MC_PASS"; do
                echo "waiting for minio..."; sleep 3
              done
              mc mb --ignore-existing local/{{ .Values.config.KDIVE_S3_BUCKET }}
          env:
            - name: MC_USER
              value: {{ .Values.demoCredentials.minio.rootUser | quote }}
            - name: MC_PASS
              value: {{ .Values.demoCredentials.minio.rootPassword | quote }}
{{- end }}
```

- [ ] **Step 3: Create `templates/demo/oidc.yaml`** (the `JSON_CONFIG` pins `aud=kdive` — spec B2):

```gotemplate
{{- if .Values.bundledBackends }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "kdive.fullname" . }}-oidc
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ include "kdive.fullname" . }}-oidc
  template:
    metadata:
      labels:
        app: {{ include "kdive.fullname" . }}-oidc
        {{- include "kdive.labels" . | nindent 8 }}
    spec:
      containers:
        - name: oidc
          image: {{ .Values.demo.oidc.image }}
          env:
            - name: SERVER_PORT
              value: "8080"
            # Mint aud=kdive on every token from the `default` issuer (spec B2).
            - name: JSON_CONFIG
              value: '{"interactiveLogin":false,"tokenCallbacks":[{"issuerId":"default","requestMappings":[{"requestParam":"grant_type","match":"*","claims":{"sub":"kdive-demo","aud":["kdive"]}}]}]}'
          ports:
            - containerPort: 8080
          readinessProbe:
            httpGet:
              path: /default/.well-known/openid-configuration
              port: 8080
            periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "kdive.fullname" . }}-oidc
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    app: {{ include "kdive.fullname" . }}-oidc
  ports:
    - port: 8080
      targetPort: 8080
{{- end }}
```

- [ ] **Step 4: Create `templates/demo/networkpolicy.yaml`** (defense-in-depth; the primary guard is the Task 4 ClusterIP gate — spec access-boundary):

```gotemplate
{{- if .Values.bundledBackends }}
# Defense-in-depth: confine demo pods' ingress to the release namespace. The primary
# exposure guard is the ClusterIP render-gate (a NodePort SNATs to a node IP that this
# policy can't distinguish). No-op under a CNI that does not enforce NetworkPolicy.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "kdive.fullname" . }}-demo
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/instance: {{ .Release.Name }}
  policyTypes: ["Ingress"]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ .Release.Namespace }}
{{- end }}
```

- [ ] **Step 5: Render the bundled path and confirm all four demo kinds appear.**

Run:
```bash
helm template kdive deploy/helm/kdive --set bundledBackends=true --set demoAcknowledged=true \
  | grep -E 'name: kdive-kdive-(postgres|minio|oidc|demo)|kind: NetworkPolicy' | sort -u
```
Expected: lines for `-postgres`, `-minio`, `-minio-init`, `-oidc`, `-demo`, and `kind: NetworkPolicy`.

- [ ] **Step 6: Confirm the external path renders none of them.**

Run: `helm template kdive deploy/helm/kdive --set config.KDIVE_DATABASE_URL=postgresql://x/y | grep -cE 'demo|NetworkPolicy|mock-oauth2' || echo 0`
Expected: `0`.

- [ ] **Step 7: Commit.**

```bash
git add deploy/helm/kdive/templates/demo/
git commit -m "feat(helm): first-party demo Postgres/MinIO/mock-OIDC + NetworkPolicy"
```

---

### Task 7: Point the migrate Job's wait-for-db at the in-chart Postgres

Spec B3. The bundled migrate Job waits on `{{ .Release.Name }}-postgresql` (the old subchart name); the in-chart service is `{{ include "kdive.fullname" . }}-postgres`.

**Files:**
- Modify: `deploy/helm/kdive/templates/job-migrate.yaml:42`

- [ ] **Step 1: Replace the host line** (line 42 inside the `wait-for-db` script):

```gotemplate
              host = "{{ include "kdive.fullname" . }}-postgres"
```

- [ ] **Step 2: Verify the bundled render references the new host.**

Run: `helm template kdive deploy/helm/kdive --set bundledBackends=true --set demoAcknowledged=true | grep 'host = "kdive-kdive-postgres"'`
Expected: prints the matching line.

- [ ] **Step 3: Commit.**

```bash
git add deploy/helm/kdive/templates/job-migrate.yaml
git commit -m "fix(helm): wait-for-db targets the in-chart demo Postgres service"
```

---

### Task 8: `helm test` smoke pod

Spec B4. Best-effort CPU preflight → poll MCP Service → mint `aud=kdive` token → list tools via the session-negotiating client.

**Files:**
- Create: `deploy/helm/kdive/templates/tests/smoke.yaml`

- [ ] **Step 1: Create `templates/tests/smoke.yaml`:**

```gotemplate
{{- if .Values.bundledBackends }}
apiVersion: v1
kind: Pod
metadata:
  name: {{ include "kdive.fullname" . }}-smoke
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
  annotations:
    "helm.sh/hook": test
    "helm.sh/hook-delete-policy": before-hook-creation
spec:
  restartPolicy: Never
  containers:
    - name: smoke
      image: {{ include "kdive.image" . }}
      imagePullPolicy: {{ .Values.image.pullPolicy }}
      env:
        - name: OIDC
          value: "http://{{ include "kdive.fullname" . }}-oidc:8080/default"
        - name: MCP
          value: "http://{{ include "kdive.fullname" . }}-server:8000/mcp"
      command: ["python", "-c"]
      args:
        - |
          import asyncio, json, os, sys, time, urllib.error, urllib.request
          mcp, oidc = os.environ["MCP"], os.environ["OIDC"]
          # (1) best-effort node-CPU preflight (this pod's node only; the readiness
          # poll below is the authoritative x86-64-v2 signal).
          if "sse4_2" not in open("/proc/cpuinfo").read():
              print("WARN: node lacks sse4_2 (x86-64-v2); demo backends may crash", file=sys.stderr)
          # (2) poll the MCP Service until it answers. ANY HTTP status means the server
          # process is up (don't guess FastMCP's exact unauth/GET code — 401/400/406/307/405
          # are all "up"); only connection-level errors mean "not yet". tools/list is static,
          # so this proceeds correctly even while /readyz is still red (post-install migrate).
          for _ in range(60):
              try:
                  urllib.request.urlopen(mcp, timeout=3)
                  break
              except urllib.error.HTTPError:
                  break  # server responded with an HTTP status -> it is up
              except Exception:
                  time.sleep(2)  # connection refused / no route / timeout -> wait
          else:
              sys.exit("MCP never became reachable")
          # (3) mint an aud=kdive token from the bundled issuer.
          data = b"grant_type=client_credentials&client_id=kdive-demo&client_secret=x"
          req = urllib.request.Request(
              oidc + "/token", data=data,
              headers={"Content-Type": "application/x-www-form-urlencoded"})
          token = json.load(urllib.request.urlopen(req, timeout=10))["access_token"]
          # (4) list tools over MCP via the session-negotiating client (handshake required).
          from fastmcp import Client
          from fastmcp.client.transports import StreamableHttpTransport
          async def main():
              transport = StreamableHttpTransport(mcp, headers={"Authorization": "Bearer " + token})
              async with Client(transport) as client:
                  tools = await client.list_tools()
                  assert tools, "tools/list returned empty"
                  print("OK: %d tools" % len(tools))
          asyncio.run(main())
{{- end }}
```

- [ ] **Step 2: Verify the exact `fastmcp` client import against the installed version** (the transport import path is version-sensitive):

Run: `uv run python -c "from fastmcp import Client; from fastmcp.client.transports import StreamableHttpTransport; print('ok')"`
Expected: `ok`. If it errors, adjust the import in the template to the installed path (e.g. `from fastmcp.client import Client`) and re-run.

- [ ] **Step 3: Confirm the smoke pod renders only on the bundled path.**

Run:
```bash
helm template kdive deploy/helm/kdive --set bundledBackends=true --set demoAcknowledged=true | grep -c 'helm.sh/hook: test'
helm template kdive deploy/helm/kdive --set config.KDIVE_DATABASE_URL=postgresql://x/y | grep -c 'helm.sh/hook: test' || true
```
Expected: `1` then `0`.

- [ ] **Step 4: Commit.**

```bash
git add deploy/helm/kdive/templates/tests/smoke.yaml
git commit -m "test(helm): helm-test smoke pod mints a token and lists tools over MCP"
```

---

### Task 9: `values-demo.yaml` + `NOTES.txt`

Spec B4/B5 + A4 (the demo pins `:edge`).

**Files:**
- Create: `deploy/helm/kdive/values-demo.yaml`
- Modify: `deploy/helm/kdive/templates/NOTES.txt`

- [ ] **Step 1: Create `deploy/helm/kdive/values-demo.yaml`:**

```yaml
# Turnkey demo. Pulls the published rolling image and stands up in-chart
# Postgres/MinIO/mock-OIDC. Ephemeral (emptyDir) — a pod restart drops all state.
# Reach MCP with `kubectl port-forward`; never expose it (the bundled issuer mints
# valid kdive tokens for any caller). See deploy/helm/kdive/README.md.
image:
  tag: edge
bundledBackends: true
demoAcknowledged: true
```

- [ ] **Step 2: Add a bundled-path block to `NOTES.txt`.** Append (the existing NOTES content stays for the external path):

```gotemplate
{{- if .Values.bundledBackends }}

DEMO MODE (ephemeral — a pod restart drops all state).

Verify the whole stack (helm test waits for readiness itself; add --wait to the
install above if you prefer the release to block until pods are Ready):
  helm test {{ .Release.Name }}

Reach MCP (keep it cluster-internal — the demo issuer mints valid kdive tokens for
any caller; never expose it via NodePort/LoadBalancer/Ingress):
  kubectl port-forward svc/{{ include "kdive.fullname" . }}-server 8000:8000

Mint a demo token for your own client (expires per the issuer's tokenExpiry):
  TOKEN=$(kubectl run mint --rm -i --restart=Never --image=curlimages/curl -q -- \
    -s -d 'grant_type=client_credentials&client_id=kdive-demo&client_secret=x' \
    http://{{ include "kdive.fullname" . }}-oidc:8080/default/token | sed -e 's/.*"access_token":"//' -e 's/".*//')
Then drive http://127.0.0.1:8000/mcp with an MCP client and `Authorization: Bearer $TOKEN`
(the streamable-HTTP transport needs the MCP initialize handshake — a raw tools/list POST 307s).
{{- end }}
```

- [ ] **Step 3: Render NOTES for both paths.** `helm template` does NOT render NOTES.txt
  (and `--show-only templates/NOTES.txt` errors); only `install`/`upgrade` do, so use
  `--dry-run`:

Run:
```bash
helm install kdive deploy/helm/kdive -f deploy/helm/kdive/values-demo.yaml --dry-run 2>/dev/null | grep -c "DEMO MODE"
helm install kdive deploy/helm/kdive --set config.KDIVE_DATABASE_URL=postgresql://x/y --dry-run 2>/dev/null | grep -c "DEMO MODE" || true
```
Expected: `1` for the demo install, `0` for the external install. (`--dry-run` renders NOTES without contacting the cluster.)

- [ ] **Step 4: Commit.**

```bash
git add deploy/helm/kdive/values-demo.yaml deploy/helm/kdive/templates/NOTES.txt
git commit -m "feat(helm): values-demo.yaml (pins :edge) and demo NOTES guidance"
```

---

### Task 10: Render tests (update broken ones, add new coverage)

Spec Testing. The existing bundled-path tests hard-code the old subchart service names and MUST be updated; new tests cover OIDC wiring, the demo deployments, the NetworkPolicy, and the ClusterIP gate.

**Files:**
- Modify: `tests/helm/test_helm_render.py:59-66` (fix old service names)
- Modify: `tests/helm/test_helm_render.py` (add new tests at end, before `test_lint_is_clean`)

- [ ] **Step 1: Run the suite first to see the bundled test fail against the new names.**

Run: `uv run python -m pytest tests/helm/test_helm_render.py::test_bundled_path_wires_backends_into_config -q`
Expected: FAIL (asserts old `kdive-postgresql`/`kdive-minio`).

- [ ] **Step 2: Fix `test_bundled_path_wires_backends_into_config`** (lines 59-66) to the new in-chart service names and add the OIDC assertion:

```python
def test_bundled_path_wires_backends_into_config() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    # The demo apps must reach the in-chart services, not render empty config.
    dsn = "postgresql://kdive:kdive-demo@kdive-kdive-postgres:5432/kdive"  # pragma: allowlist secret
    assert f'KDIVE_DATABASE_URL: "{dsn}"' in res.stdout
    assert 'KDIVE_S3_ENDPOINT_URL: "http://kdive-kdive-minio:9000"' in res.stdout
    assert 'KDIVE_OIDC_ISSUER: "http://kdive-kdive-oidc:8080/default"' in res.stdout
    assert 'KDIVE_OIDC_JWKS_URI: "http://kdive-kdive-oidc:8080/default/jwks"' in res.stdout
    assert "wait-for-db" in res.stdout
```

- [ ] **Step 3: Run that test green.**

Run: `uv run python -m pytest tests/helm/test_helm_render.py::test_bundled_path_wires_backends_into_config -q`
Expected: PASS.

- [ ] **Step 4: Add new tests** at the end of the file (before `test_lint_is_clean`):

```python
def test_bundled_renders_demo_backends() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    for name in ("kdive-kdive-postgres", "kdive-kdive-minio", "kdive-kdive-oidc"):
        assert f"name: {name}\n" in res.stdout, name
    assert "mock-oauth2-server" in res.stdout
    assert "kind: NetworkPolicy" in res.stdout
    # six Deployments on the demo path: 3 app + 3 demo backends.
    assert res.stdout.count("kind: Deployment") == 6


def test_external_path_has_no_demo_backends() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    assert "mock-oauth2-server" not in res.stdout
    assert "kind: NetworkPolicy" not in res.stdout
    assert res.stdout.count("kind: Deployment") == 3


def test_bundled_oidc_pins_audience_kdive() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    assert '"aud":["kdive"]' in res.stdout


def test_bundled_demo_services_are_clusterip() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true")
    assert res.returncode == 0, res.stderr
    for doc in yaml.safe_load_all(res.stdout):
        if isinstance(doc, dict) and doc.get("kind") == "Service":
            assert doc["spec"].get("type", "ClusterIP") == "ClusterIP", doc["metadata"]["name"]


def test_bundled_with_nodeport_is_rejected() -> None:
    res = _template("bundledBackends=true", "demoAcknowledged=true", "service.type=NodePort")
    assert res.returncode != 0
    assert "service.type must stay ClusterIP" in res.stderr


def test_bundled_has_a_helm_test_hook() -> None:
    hooks = _hooks_by_kind("bundledBackends=true", "demoAcknowledged=true")
    assert hooks.get("Pod", {}).get("phase") == "test"
```

- [ ] **Step 5: Run the full helm render suite.**

Run: `uv run python -m pytest tests/helm/test_helm_render.py -q`
Expected: all PASS (including the unchanged external-path and aux-port tests, and `test_lint_is_clean`).

- [ ] **Step 6: Commit.**

```bash
git add tests/helm/test_helm_render.py
git commit -m "test(helm): cover in-chart demo backends, OIDC aud, ClusterIP gate, helm-test hook"
```

---

### Task 11: Chart README + kubernetes-deploy runbook

Spec B1 (remove subchart-distribution) + B5 (demo install). PR1 already added the `:edge` install note.

**Files:**
- Modify: `deploy/helm/kdive/README.md` ("Bundled backends" + "Subchart distribution" sections)
- Modify: `docs/runbooks/kubernetes-deploy.md` (bundled-backends mentions)

- [ ] **Step 1: Replace the chart README "Bundled backends (demo only)" section** with the new one-command flow:

```markdown
## Bundled backends (demo only)

`bundledBackends=true` (co-set with `demoAcknowledged=true`) stands up first-party Postgres,
MinIO, and a mock-OIDC issuer as in-chart Deployments on `emptyDir`: **a pod restart drops all
state by design.** The issuer mints valid `aud=kdive` tokens for any caller, so the chart
forces `service.type=ClusterIP` on this path — reach MCP with `kubectl port-forward`, never
expose it.

```sh
helm install kdive deploy/helm/kdive -f deploy/helm/kdive/values-demo.yaml
helm test kdive    # mints a token, asserts tools/list returns tools
```

`values-demo.yaml` pins `image.tag=edge` (the rolling published image); without a published
image the demo cannot pull. The demo migrate Job runs `post-install` behind a DB-readiness
init container.
```

- [ ] **Step 2: Delete the "Subchart distribution" section** from the chart README (the subcharts are gone).

- [ ] **Step 3: Update `docs/runbooks/kubernetes-deploy.md`** — replace the bundled-backends warning (the "emptyDir-only and its Bitnami subchart images were retired" prerequisite bullet) with a pointer to the first-party demo path + `helm test`, and drop the `helm dependency build` from the bundled instructions.

- [ ] **Step 4: Verify docs guards pass.**

Run: `just docs-check && just config-docs-check`
Expected: PASS (hand-written docs only; no generated-reference drift).

- [ ] **Step 5: Commit.**

```bash
git add deploy/helm/kdive/README.md docs/runbooks/kubernetes-deploy.md
git commit -m "docs: first-party demo path; drop subchart-distribution section"
```

---

### Task 12: Full gate + chart lint

- [ ] **Step 1: Run the helm render suite and chart lint.**

Run: `uv run python -m pytest tests/helm/test_helm_render.py -q`
Expected: all PASS, including `test_lint_is_clean` (`helm lint` → `0 chart(s) failed`).

- [ ] **Step 2: Run the workflow + shell lint the changed files.**

Run: `just lint-workflows && just lint-shell`
Expected: PASS (Task 2 touched ci.yml).

- [ ] **Step 3: Run the PR1 appVersion guard (chart version unchanged appVersion).**

Run: `just chart-version-check`
Expected: `appVersion == pyproject == 0.3.0` (Task 2 bumped chart `version`, not `appVersion`).

- [ ] **Step 4: No commit (verification only).** If anything failed, fix in the owning task and re-run.

---

## Self-review (PR2)

- **Spec coverage:** B-preamble ADR → Task 1; B1 (drop subcharts, chart version, dead values) → Tasks 2–3; B2 (demo templates, aud pinning, bucket wait) → Task 6; B3 (helpers, configmap, migrate host) → Tasks 4, 5, 7; B4 (helm test, NOTES) → Tasks 8–9; B5 (install) → Task 9; access-boundary gate + NetworkPolicy → Tasks 4, 6; Testing → Tasks 10, 12.
- **Type/name consistency:** demo service names are `kdive-kdive-{postgres,minio,oidc}` everywhere (helpers Task 4, configmap Task 5, templates Task 6, migrate Task 7, smoke Task 8, NOTES Task 9, tests Task 10). The MCP Service is `kdive-kdive-server` (existing `service.yaml`).
- **Broken-test fix:** Task 10 step 2 fixes the pre-existing `test_bundled_path_wires_backends_into_config` whose hard-coded subchart names (`kdive-postgresql`/`kdive-minio`) no longer render.
- **Dependency on PR1:** the demo (`image.tag=edge`) and `helm test` require PR1's published `:edge` image; the runbook/README state this prerequisite.
- **Open detail flagged:** Task 8 step 2 verifies the exact `fastmcp` client import against the installed version before relying on it.
