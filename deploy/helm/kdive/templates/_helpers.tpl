{{- define "kdive.fullname" -}}{{ .Release.Name }}-kdive{{- end -}}

{{- define "kdive.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if .Values.image.digest -}}
{{- .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{- .Values.image.repository }}:{{ $tag }}
{{- end -}}
{{- end -}}

{{- define "kdive.labels" -}}
app.kubernetes.io/name: kdive
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/*
On the bundledBackends demo path the apps must reach the in-chart Postgres/MinIO/OIDC.
These helpers derive KDIVE_DATABASE_URL / KDIVE_S3_ENDPOINT_URL / KDIVE_OIDC_ISSUER /
KDIVE_OIDC_JWKS_URI from the in-chart service names and the fixed demo credentials in
.Values.demoCredentials, falling back to the operator-provided .Values.config.* on the
external-backend path.
*/}}
{{- define "kdive.databaseUrl" -}}
{{- if .Values.bundledBackends -}}
{{- $c := .Values.demoCredentials.postgresql -}}
{{- $userinfo := printf "%s:%s" $c.username $c.password -}}
{{- $host := printf "%s-postgres:5432" (include "kdive.fullname" .) -}}
{{- printf "postgresql://%s@%s/%s" $userinfo $host $c.database -}}
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

{{/*
Aux health/metrics wiring (ADR-0090 §5). Each process exposes /livez /readyz /metrics
on a per-process aux port (server 9464, worker 9465, reconciler 9466). The listener
binds 0.0.0.0:<port> INSIDE the pod via an explicit KDIVE_HEALTH_BIND_ADDR env (env
wins over the shared configMap) so the kubelet/scrape can reach it; no Service fronts
the aux port, so it stays pod-local / non-public. Liveness probes /livez (loop-alive)
and readiness probes /readyz (dependency set) — a failing /readyz withdraws/gates the
pod but must never let liveness kill a live-but-not-ready pod. Call with a port arg:
`{{ include "kdive.auxEnv" 9464 }}`.
*/}}
{{- define "kdive.auxEnv" -}}
- name: KDIVE_HEALTH_BIND_ADDR
  value: "0.0.0.0:{{ . }}"
{{- end -}}

{{- define "kdive.auxProbes" -}}
livenessProbe:
  httpGet:
    path: /livez
    port: {{ . }}
  initialDelaySeconds: 5
  periodSeconds: 10
readinessProbe:
  httpGet:
    path: /readyz
    port: {{ . }}
  initialDelaySeconds: 5
  periodSeconds: 10
{{- end -}}

{{/*
Optional file-secret projection (issue #313). When .Values.secrets.secretName is set, the
chart mounts that pre-existing Secret read-only under .Values.secrets.mountPath and points
KDIVE_SECRETS_ROOT at it, so file-ref secrets (e.g. the remote-libvirt TLS client cert/key/CA)
resolve under the root. Each helper renders nothing when secretName is empty, so a deployment
that does not opt in is unchanged. Call with the root context: `include "kdive.secretsEnv" .`.
*/}}
{{- define "kdive.secretsEnv" -}}
{{- if .Values.secrets.secretName -}}
- name: KDIVE_SECRETS_ROOT
  value: {{ .Values.secrets.mountPath | quote }}
{{- end -}}
{{- end -}}

{{- define "kdive.secretsVolumeMount" -}}
{{- if .Values.secrets.secretName -}}
- name: kdive-secrets
  mountPath: {{ .Values.secrets.mountPath | quote }}
  readOnly: true
{{- end -}}
{{- end -}}

{{- define "kdive.secretsVolume" -}}
{{- if .Values.secrets.secretName -}}
- name: kdive-secrets
  secret:
    secretName: {{ .Values.secrets.secretName | quote }}
    # 0440 (not 0400): k8s owns Secret files as root, so the non-root UID reads them via
    # the pod's fsGroup group bit — owner-only 0400 would be unreadable to UID 10001.
    defaultMode: 0440
{{- end -}}
{{- end -}}

{{- define "kdive.scrapeAnnotations" -}}
prometheus.io/scrape: "true"
prometheus.io/path: /metrics
prometheus.io/port: "{{ . }}"
{{- end -}}

{{/*
Render gate: bundledBackends is ephemeral/demo-only, so it must be co-set with
demoAcknowledged. A free-standing fail in this partial would never execute (Helm
only renders define blocks from _*.tpl when included), so the check lives in a
named template that a rendered manifest (the ConfigMap) includes.
*/}}
{{- define "kdive.validateValues" -}}
{{- if and .Values.bundledBackends (not .Values.demoAcknowledged) -}}
{{- fail "bundledBackends is ephemeral/demo-only: set demoAcknowledged=true to use it (data is NOT durable)" -}}
{{- end -}}
{{- if and .Values.bundledBackends (ne (.Values.service.type | toString) "ClusterIP") -}}
{{- fail "bundledBackends is demo-only and its issuer mints valid kdive tokens for any caller: service.type must stay ClusterIP (reach MCP via `kubectl port-forward`). Expose MCP only on the external-backend path, behind a real IdP." -}}
{{- end -}}
{{- end -}}
