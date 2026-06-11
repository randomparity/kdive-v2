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
On the bundledBackends demo path the apps must reach the in-release Postgres/MinIO.
These helpers derive KDIVE_DATABASE_URL / KDIVE_S3_ENDPOINT_URL from the subchart
service names and the fixed demo credentials in .Values.demoCredentials, falling
back to the operator-provided .Values.config.* on the external-backend path.
*/}}
{{- define "kdive.databaseUrl" -}}
{{- if .Values.bundledBackends -}}
{{- $c := .Values.demoCredentials.postgresql -}}
{{- $userinfo := printf "%s:%s" $c.username $c.password -}}
{{- $host := printf "%s-postgresql:5432" .Release.Name -}}
{{- printf "postgresql://%s@%s/%s" $userinfo $host $c.database -}}
{{- else -}}
{{- .Values.config.KDIVE_DATABASE_URL -}}
{{- end -}}
{{- end -}}

{{- define "kdive.s3Endpoint" -}}
{{- if .Values.bundledBackends -}}
{{- printf "http://%s-minio:9000" .Release.Name -}}
{{- else -}}
{{- .Values.config.KDIVE_S3_ENDPOINT_URL -}}
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
{{- end -}}
