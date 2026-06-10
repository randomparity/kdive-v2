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
