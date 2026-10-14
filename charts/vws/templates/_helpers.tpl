{{/*
Labels and the pod body for every control-plane service.
The services map is resolved in control-plane.yaml. These defines do not
branch on a service name and do not read the raw values map.
*/}}

{{/*
Resolve a namespace key to a name. Names live only in values namespaces; "release" is the release namespace.
Call with (dict "root" $ "key" "vms").
*/}}
{{- define "vws.ns" -}}
{{- if eq .key "release" }}{{ .root.Release.Namespace }}
{{- else }}{{ required (printf "namespaces.%s is not defined" .key) (get .root.Values.namespaces .key) }}
{{- end }}
{{- end -}}

{{/*
Container args of one service: flags, plus pendingFlags when global.pendingFlags is true.
A flag value is a scalar or {namespace: <key>}. Call with (dict "root" $ "spec" $spec).
*/}}
{{- define "vws.args" -}}
{{- $flags := deepCopy (.spec.flags | default dict) }}
{{- if .root.Values.global.pendingFlags }}
  {{- $flags = mergeOverwrite $flags (deepCopy (.spec.pendingFlags | default dict)) }}
{{- end }}
{{- $rgw := (.spec.s3 | default dict).rgw | default dict }}
{{- if $rgw.enabled }}
  {{- $_ := set $flags "object-backend" "s3" }}
  {{- $_ = set $flags "s3-profile" "rgw" }}
  {{- $_ = set $flags "s3-endpoint" (required "services.*.s3.rgw.enabled needs s3.rgw.endpoint" $rgw.endpoint) }}
  {{- $_ = required "services.*.s3.rgw.enabled needs s3.rgw.existingSecret" $rgw.existingSecret }}
  {{- if $rgw.bucketPrefix }}
    {{- $_ = set $flags "s3-bucket-prefix" $rgw.bucketPrefix }}
  {{- end }}
{{- end }}
{{- /* apiCredentials: the credential flags are $(VAR) references kubelet expands from the
secretKeyRef env of vws.podTemplate, so no value is in values, args or the pod spec. */}}
{{- if .spec.apiCredentials }}
  {{- $_ := required "services.*.apiCredentials needs existingSecret" .spec.apiCredentials.existingSecret }}
  {{- $_ = set $flags "console-token" "$(VWS_CONSOLE_TOKEN)" }}
  {{- $_ = set $flags "access-key" "$(VWS_ACCESS_KEY)" }}
  {{- $_ = set $flags "secret" "$(VWS_SECRET_KEY)" }}
{{- end }}
{{- $args := list }}
{{- range $key := keys $flags | sortAlpha }}
  {{- $value := index $flags $key }}
  {{- if kindIs "map" $value }}
    {{- $value = include "vws.ns" (dict "root" $.root "key" $value.namespace) }}
  {{- else if kindIs "float64" $value }}
    {{- $value = int64 $value }}
  {{- end }}
  {{- $args = append $args (printf "-%s=%v" $key $value) }}
{{- end }}
{{- toJson $args }}
{{- end -}}

{{- define "vws.selector" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end -}}

{{- define "vws.labels" -}}
{{ include "vws.selector" . }}
app.kubernetes.io/part-of: vws
app.kubernetes.io/component: control-plane
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version | replace "+" "_" }}
{{- end -}}

{{/*
Probes of every service. global.probe picks the check: tcp (default) opens the container port,
grpc calls grpc.health.v1 on it and needs the service to implement that. A service with
grpcReadinessService gets grpc probes on its own (always, #110): liveness and
startup ask the empty service name, readiness asks grpcReadinessService. No HTTP probes: the
services speak gRPC and have no /healthz. The port is the service port from values.
*/}}
{{- define "vws.probe" -}}
{{- if or (eq .root.Values.global.probe "grpc") .grpcReady }}
grpc:
  port: {{ .port }}
  {{- if and .readiness .grpcReady }}
  service: {{ .grpcReady }}
  {{- end }}
{{- else }}
tcpSocket:
  port: http
{{- end }}
{{- end -}}

{{- define "vws.probes" -}}
startupProbe:
  {{- include "vws.probe" . | nindent 2 }}
  periodSeconds: 5
  timeoutSeconds: 2
  failureThreshold: {{ .threshold }}
livenessProbe:
  {{- include "vws.probe" . | nindent 2 }}
  periodSeconds: 10
  timeoutSeconds: 2
  failureThreshold: 3
readinessProbe:
  {{- include "vws.probe" (merge (dict "readiness" true) .) | nindent 2 }}
  periodSeconds: 5
  timeoutSeconds: 2
  failureThreshold: 2
{{- end -}}

{{- define "vws.podTemplate" -}}
metadata:
  labels:
    {{- include "vws.labels" . | nindent 4 }}
spec:
  restartPolicy: Always
  serviceAccountName: {{ .name }}
  automountServiceAccountToken: {{ .rbac }}
  enableServiceLinks: false
  terminationGracePeriodSeconds: 30
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    runAsGroup: 65532
    fsGroup: 65532
    fsGroupChangePolicy: OnRootMismatch
    seccompProfile:
      type: RuntimeDefault
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: kubernetes.io/hostname
      whenUnsatisfiable: {{ ternary "DoNotSchedule" "ScheduleAnyway" (eq (.root.Values.global.topologySpread | default "soft") "hard") }}
      labelSelector:
        matchLabels:
          {{- include "vws.selector" . | nindent 10 }}
  containers:
    - name: {{ .name }}
      image: {{ .image }}
      imagePullPolicy: IfNotPresent
      {{- if .args }}
      args:
        {{- toYaml .args | nindent 8 }}
      {{- end }}
      ports:
        - name: http
          containerPort: {{ .port }}
      env:
        - name: VWS_SERVICE_NAME
          value: {{ .name | quote }}
        {{- if .stateful }}
        - name: VWS_STATE_DIR
          value: /var/lib/vws
        {{- end }}
        {{- with .rgwSecret }}
        - name: VWS_S3_ACCESS_KEY
          valueFrom:
            secretKeyRef:
              name: {{ . }}
              key: access-key
        - name: VWS_S3_SECRET_KEY
          valueFrom:
            secretKeyRef:
              name: {{ . }}
              key: secret-key
        {{- end }}
        {{- with .apiSecret }}
        - name: VWS_CONSOLE_TOKEN
          valueFrom:
            secretKeyRef:
              name: {{ . }}
              key: console-token
        - name: VWS_ACCESS_KEY
          valueFrom:
            secretKeyRef:
              name: {{ . }}
              key: access-key
        - name: VWS_SECRET_KEY
          valueFrom:
            secretKeyRef:
              name: {{ . }}
              key: secret-key
        {{- end }}
        {{- with .apiClient }}
        - name: VWS_CONSOLE_API_URL
          value: {{ .url | quote }}
        - name: VWS_CONSOLE_API_TOKEN
          valueFrom:
            secretKeyRef:
              name: {{ .secret }}
              key: console-token
        {{- end }}
      {{- include "vws.probes" . | nindent 6 }}
      resources:
        requests:
          cpu: {{ .cpu }}
          memory: {{ .memory }}
        limits:
          memory: {{ .memory }}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: tmp
          mountPath: /tmp
        {{- if .stateful }}
        - name: state
          mountPath: /var/lib/vws
        {{- end }}
        {{- if .kube }}
        - name: kubeconfig
          mountPath: {{ .kube.dir }}
          readOnly: true
        {{- end }}
  volumes:
    - name: tmp
      emptyDir: {}
    {{- if .kube }}
    - name: kubeconfig
      secret:
        secretName: {{ .kube.secret }}
        defaultMode: 0440
        items:
          - key: {{ .kube.key }}
            path: {{ .kube.key }}
    {{- end }}
{{- end -}}
