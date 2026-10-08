{{- define "brain-ops.fullname" -}}
{{- $name := default (printf "%s-%s" .Release.Name (default .Chart.Name .Values.nameOverride)) .Values.fullnameOverride -}}
{{- if gt (len $name) 41 -}}
{{- printf "%s-%s" ($name | trunc 32 | trimSuffix "-") ($name | sha256sum | trunc 8) -}}
{{- else -}}
{{- $name | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
