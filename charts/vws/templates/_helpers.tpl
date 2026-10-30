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
Container args of one service: flags, plus pendingFlags when global.pendingFlags is true, then
one arg per item of each repeatedFlags list.
A flag value is a scalar or {namespace: <key>}.
Call with (dict "root" $ "spec" $spec "iamEnabled" $iamEnabled "iamPort" $iamPort).
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
{{- $iamFlag := "" }}
{{- if and (hasKey $flags "iam") (kindIs "string" (index $flags "iam")) }}
  {{- $iamFlag = index $flags "iam" }}
{{- end }}
{{- if eq $iamFlag "grpc" }}
  {{- if not .iamEnabled }}
    {{- fail "R14: -iam=grpc needs services.iam enabled" }}
  {{- end }}
  {{- if hasKey $flags "iam-endpoint" }}
    {{- fail "R14: -iam-endpoint is derived from services.iam.port" }}
  {{- end }}
  {{- $_ := set $flags "iam-endpoint" (printf "iam:%d" (int .iamPort)) }}
{{- else if hasKey $flags "iam-endpoint" }}
  {{- fail "R14: -iam-endpoint requires -iam=grpc" }}
{{- end }}
{{- /* apiCredentials: no credential flag. console-api reads VWS_CONSOLE_TOKEN, VWS_ACCESS_KEY and
VWS_SECRET_KEY from the secretKeyRef env of vws.podTemplate, so no value is in values, args, the pod
spec or the process arguments (vws-console-api: flag, then <ENV>_FILE, then <ENV>). */}}
{{- if .spec.apiCredentials }}
  {{- $_ := required "services.*.apiCredentials needs existingSecret" .spec.apiCredentials.existingSecret }}
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
{{- /* repeatedFlags: a flag given once per list item, in list order, after flags (vws-deploy#391). */}}
{{- $repeated := .spec.repeatedFlags | default dict }}
{{- range $key := keys $repeated | sortAlpha }}
  {{- if hasKey $flags $key }}
    {{- fail (printf "R14: -%s is in both flags and repeatedFlags" $key) }}
  {{- end }}
  {{- range $item := index $repeated $key }}
    {{- $args = append $args (printf "-%s=%s" $key $item) }}
  {{- end }}
{{- end }}
{{- toJson $args }}
{{- end -}}

{{/*
Call with (dict "flags" $flags "port" $port).
*/}}
{{- define "vws.extraPorts" -}}
{{- $primary := int .port }}
{{- $acc := dict "items" (list) "primaryHits" 0 }}
{{- range $key, $value := (.flags | default dict) }}
  {{- if and (kindIs "string" $value) (regexMatch "-addr$" $key) }}
    {{- if regexMatch "^127[.]0[.]0[.]1:[0-9]+$" $value }}
      {{- fail (printf "R15: -%s=%s must not listen on loopback" $key $value) }}
    {{- end }}
    {{- if regexMatch "^0[.]0[.]0[.]0:[0-9]+$" $value }}
      {{- $n := int (index (splitList ":" $value) 1) }}
      {{- if or (lt $n 1024) (gt $n 65535) }}
        {{- fail (printf "R15: -%s=%s is outside 1024..65535" $key $value) }}
      {{- else if eq $n $primary }}
        {{- $_ := set $acc "primaryHits" (add (index $acc "primaryHits") 1) }}
      {{- else }}
        {{- $pname := trimSuffix "-addr" $key }}
        {{- if not (regexMatch "^[a-z]([a-z0-9-]{0,13}[a-z0-9])?$" $pname) }}
          {{- fail (printf "R15: -%s=%s port name %s is invalid" $key $value $pname) }}
        {{- end }}
        {{- $_ := set $acc "items" (append (index $acc "items") (dict "key" $key "value" $value "name" $pname "port" $n)) }}
      {{- end }}
    {{- end }}
  {{- end }}
{{- end }}
{{- if gt (int (index $acc "primaryHits")) 1 }}
  {{- fail "R15: more than one -addr flag listens on the primary port" }}
{{- end }}
{{- $seenName := dict }}
{{- $seenPort := dict }}
{{- range $item := index $acc "items" }}
  {{- if or (hasKey $seenName $item.name) (eq $item.name "http") }}
    {{- fail (printf "R15: -%s=%s repeats Service port name %s" $item.key $item.value $item.name) }}
  {{- end }}
  {{- if hasKey $seenPort (toString (int $item.port)) }}
    {{- fail (printf "R15: -%s=%s repeats Service port %d" $item.key $item.value (int $item.port)) }}
  {{- end }}
  {{- $_ := set $seenName $item.name $item.key }}
  {{- $_ := set $seenPort (toString (int $item.port)) $item.key }}
{{- end }}
{{- $packed := dict "keys" (list) "port" (dict) }}
{{- range $item := index $acc "items" }}
  {{- $_ := set $packed "keys" (append (index $packed "keys") (printf "%05d|%s" (int $item.port) $item.name)) }}
  {{- $_ := set (index $packed "port") $item.name (int $item.port) }}
{{- end }}
{{- $out := dict "items" (list) }}
{{- range $key := index $packed "keys" | sortAlpha }}
  {{- $bits := splitList "|" $key }}
  {{- $pname := index $bits 1 }}
  {{- $_ := set $out "items" (append (index $out "items") (dict "name" $pname "port" (index (index $packed "port") $pname))) }}
{{- end }}
{{- toJson (index $out "items") }}
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
Probes of every service. A service with healthPort gets httpGet probes on its metrics port:
/healthz for startup and liveness, /readyz for readiness. Otherwise global.probe picks the check:
tcp (default) opens the container port, grpc calls grpc.health.v1 on it and needs the service to
implement that. A service with grpcReadinessService gets grpc probes on its own (always, #110):
liveness and startup ask the empty service name, readiness asks grpcReadinessService. Only a
service with healthPort gets an HTTP probe; the others speak gRPC and have no /healthz. The port
is the service port from values.
*/}}
{{- define "vws.probe" -}}
{{- if .healthPort }}
httpGet:
  path: {{ .path }}
  port: metrics
{{- else if or (eq .root.Values.global.probe "grpc") .grpcReady }}
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
  {{- include "vws.probe" (merge (dict "path" "/healthz") .) | nindent 2 }}
  periodSeconds: 5
  timeoutSeconds: 2
  failureThreshold: {{ .threshold }}
livenessProbe:
  {{- include "vws.probe" (merge (dict "path" "/healthz") .) | nindent 2 }}
  periodSeconds: 10
  timeoutSeconds: 2
  failureThreshold: 3
readinessProbe:
  {{- include "vws.probe" (merge (dict "path" "/readyz" "readiness" true) .) | nindent 2 }}
  periodSeconds: 5
  timeoutSeconds: 2
  failureThreshold: 2
{{- end -}}

{{- define "vws.podTemplate" -}}
metadata:
  labels:
    {{- include "vws.labels" . | nindent 4 }}
  {{- if .healthPort }}
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: {{ .healthPort | quote }}
    prometheus.io/path: /metrics
  {{- end }}
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
        {{- if .healthPort }}
        - name: metrics
          containerPort: {{ .healthPort }}
        {{- end }}
      env:
        - name: VWS_SERVICE_NAME
          value: {{ .name | quote }}
        {{- if .healthPort }}
        - name: VWS_HEALTH_ADDR
          value: {{ printf "0.0.0.0:%d" (int .healthPort) | quote }}
        {{- end }}
        {{- with .portEnv }}
        - name: {{ . }}
          value: {{ $.port | quote }}
        {{- end }}
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
        {{- with .kekSecret }}
        - name: VWS_IAM_KEK
          valueFrom:
            secretKeyRef:
              name: {{ . }}
              key: kek
        - name: VWS_IAM_KEK_ID
          valueFrom:
            secretKeyRef:
              name: {{ . }}
              key: key-id
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
