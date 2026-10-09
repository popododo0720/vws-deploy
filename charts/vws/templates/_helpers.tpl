{{/*
Labels and the pod body for every control-plane service.
The services map is resolved in control-plane.yaml. These defines do not
branch on a service name and do not read the raw values map.
*/}}

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

{{- define "vws.probes" -}}
startupProbe:
  httpGet:
    path: /healthz
    port: http
  periodSeconds: 5
  timeoutSeconds: 2
  failureThreshold: {{ .threshold }}
livenessProbe:
  httpGet:
    path: /healthz
    port: http
  periodSeconds: 10
  timeoutSeconds: 2
  failureThreshold: 3
readinessProbe:
  httpGet:
    path: /readyz
    port: http
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
  automountServiceAccountToken: false
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
      whenUnsatisfiable: ScheduleAnyway
      labelSelector:
        matchLabels:
          {{- include "vws.selector" . | nindent 10 }}
  containers:
    - name: {{ .name }}
      image: {{ .image }}
      imagePullPolicy: IfNotPresent
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
  volumes:
    - name: tmp
      emptyDir: {}
{{- end -}}
