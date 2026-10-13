# 빠른 시작

**클러스터 준비는 범위 밖이다.** 쿠버네티스 1개 이상의 노드, `kubectl`로 cluster-admin 접근이 이미 있어야 한다(vws-deploy#43 10절).

**노드 주소는 고정이어야 한다(#118).** DHCP로 노드 IP가 바뀌면 노드 등록, CNI 터널(Kube-OVN geneve), 인증서 SAN이 모두 옛 주소를 가리켜 노드가 돌아오지 않는다(vws-test에서 node1 재부팅 뒤 실제로 일어났다). 고정 IP나 DHCP 예약을 쓰고, k3s는 `--node-ip`를 그 주소로 지정한다. `vws preflight`의 `node-ip` 검사가 k3s 노드의 `--node-ip`(주석 `k3s.io/node-args`)와 InternalIP를 비교해 다르거나 없으면 WARN을 낸다. 다른 배포판은 검사하지 않는다.

1. `scripts/bootstrap.sh`로 `build/bootstrap.yaml`을 만든다(VWSCluster CRD, 네임스페이스 `vws`, 설치용 ServiceAccount와 임시 권한, 설치 Deployment와 ClusterIP Service, 리퍼 Job). 설치 이미지는 아직 발행되지 않았다: `VWS_INSTALLER_IMAGE`의 digest가 0이다. 발행 전에는 이 절차가 동작하지 않는다(확인 필요).
2. `kubectl apply -f build/bootstrap.yaml`
3. 대화형: `kubectl -n vws exec -it deploy/vws-installer -- vws init`(installer API TUI, #142). 웹 마법사용 접속 정보는 `kubectl -n vws exec deploy/vws-installer -- vws init --print-url`. 무인: `kubectl -n vws exec -i deploy/vws-installer -- vws init --from /dev/stdin < my-vwscluster.yaml`(예제는 `examples/vwscluster/`).
4. `kubectl -n vws exec deploy/vws-installer -- vws apply --unattended`. 두 번째 실행은 `unchanged`. 3과 4를 한 번에: `kubectl -n vws exec -i deploy/vws-installer -- sh -c 'cat > /tmp/cr.yaml && vws apply --unattended -f /tmp/cr.yaml' < my-vwscluster.yaml`(#144).
5. `kubectl -n vws exec deploy/vws-installer -- vws installer finish`. 임시 cluster-admin이 사라졌는지 확인까지 한다. 실패하면 종료 코드 1이고, 그때는 `kubectl delete clusterrolebinding vws-installer-temp`를 직접 실행한다.

finish를 잊어도 `vws-installer-reaper` Job이 바인딩 생성 시각 + `INSTALLER_TTL_SECONDS`(4시간)에 지운다. 자세한 것은 `docs/installer.md`.
