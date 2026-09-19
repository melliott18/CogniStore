{{- define "cognistore.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- define "cognistore.fullname" -}}
{{- default (printf "%s-%s" .Release.Name (include "cognistore.name" .)) .Values.fullnameOverride | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- define "cognistore.labels" -}}
app.kubernetes.io/name: {{ include "cognistore.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}
{{- define "cognistore.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}
{{- end -}}
{{- define "cognistore.securityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end -}}
{{- define "cognistore.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end -}}
{{- define "cognistore.env" -}}
- name: COGNISTORE_SECURITY_PROFILE
  value: {{ .Values.securityProfile | quote }}
- name: COGNISTORE_DRIVERS
  value: /etc/cognistore/config/drivers.yaml
- name: COGNISTORE_LOG_FORMAT
  value: json
- name: COGNISTORE_JOB_STREAM
  value: {{ .Values.queue.stream | quote }}
- name: COGNISTORE_JOB_SUBJECT
  value: {{ .Values.queue.subject | quote }}
- name: COGNISTORE_JOB_CONSUMER
  value: {{ .Values.queue.consumer | quote }}
- name: HOME
  value: /tmp
{{- if eq .Values.securityProfile "production" }}
- name: COGNISTORE_ENCRYPTION_CONFIG
  value: /etc/cognistore/config/encryption.json
- name: COGNISTORE_AUTHORIZATION_POLICY
  value: /etc/cognistore/config/authorization.json
- name: COGNISTORE_TENANT_POLICY
  value: /etc/cognistore/config/tenants.json
- name: COGNISTORE_AUTH_ISSUER
  value: {{ .Values.auth.issuer | quote }}
- name: COGNISTORE_AUTH_AUDIENCE
  value: {{ .Values.auth.audience | quote }}
{{- if .Values.auth.jwksUri }}
- name: COGNISTORE_AUTH_JWKS_URI
  value: {{ .Values.auth.jwksUri | quote }}
{{- end }}
- name: COGNISTORE_TLS_CA_FILE
  value: /etc/cognistore/tls/ca.crt
- name: PGSSLROOTCERT
  value: /etc/cognistore/tls/ca.crt
- name: COGNISTORE_API_TLS_CERTFILE
  value: /etc/cognistore/tls/tls.crt
- name: COGNISTORE_API_TLS_KEYFILE
  value: /etc/cognistore/tls/tls.key
- name: COGNISTORE_HEALTH_TLS_CERTFILE
  value: /etc/cognistore/tls/tls.crt
- name: COGNISTORE_HEALTH_TLS_KEYFILE
  value: /etc/cognistore/tls/tls.key
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}
{{- define "cognistore.mounts" -}}
- name: config
  mountPath: /etc/cognistore/config
  readOnly: true
- name: tmp
  mountPath: /tmp
{{- if eq .Values.securityProfile "production" }}
- name: tls
  mountPath: /etc/cognistore/tls
  readOnly: true
{{- end }}
{{- with .Values.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end -}}
{{- define "cognistore.volumes" -}}
- name: config
  secret:
    secretName: {{ .Values.config.existingSecret }}
    defaultMode: 0440
- name: tmp
  emptyDir:
    medium: Memory
    sizeLimit: 256Mi
{{- if eq .Values.securityProfile "production" }}
- name: tls
  secret:
    secretName: {{ .Values.tls.existingSecret }}
    defaultMode: 0440
{{- end }}
{{- with .Values.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end -}}
{{- define "cognistore.scheduleClaim" -}}
{{ default (printf "%s-schedule" (include "cognistore.fullname" .)) .Values.scheduler.storage.existingClaim }}
{{- end -}}
