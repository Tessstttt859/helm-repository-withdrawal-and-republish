{{- define "ledger-api.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "ledger-api.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "ledger-api.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "ledger-api.labels" -}}
app.kubernetes.io/name: {{ include "ledger-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "ledger-api.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "ledger-api.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

