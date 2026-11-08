# JMeter 실행 이미지 (multi-arch: amd64 / arm64 모두 동작)
#
# [변경 1] 베이스 이미지
#   arm64v8/openjdk:11-jre-slim → Docker 공식 openjdk 이미지는 2022년 7월 deprecated 되어
#   더 이상 보안 패치를 받지 않습니다. 공식 대체재인 eclipse-temurin 으로 교체했습니다.
#   temurin 은 멀티아키 이미지라 arm64v8/ 접두사로 아키텍처를 고정할 필요가 없습니다.
#   JMeter 5.6.3 은 Java 8+ 에서 동작하고 17+ 를 권장합니다.
FROM eclipse-temurin:17-jre-jammy

# [변경 2] JMeter 버전
#   5.4.1 은 log4j-core 2.13.3 을 번들하고 있어 Log4Shell(CVE-2021-44228, CVSS 9.8, 원격 코드 실행)에
#   그대로 노출됩니다. 부하 테스트 도구는 응답 헤더/본문을 로깅하므로 실제로 악용 가능한 경로입니다.
#   5.4.2 / 5.4.3 에서 1차 수정되었고, 현재 최신 안정 버전은 5.6.3 입니다.
ENV JMETER_VERSION=5.6.3
ENV JMETER_HOME=/opt/apache-jmeter-${JMETER_VERSION}
ENV PATH=${JMETER_HOME}/bin:$PATH

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# [변경 3] 다운로드 무결성 검증
#   검증 없는 wget 은 미러 변조/중간자 공격에 무방비입니다. 공식 sha512 와 대조합니다.
RUN set -eux; \
    cd /tmp; \
    curl -fsSL -o jmeter.tgz    "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz"; \
    curl -fsSL -o jmeter.sha512 "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz.sha512"; \
    EXPECTED="$(tr -d ' \n' < jmeter.sha512 | sed 's/.*://' | grep -oE '[0-9a-fA-F]{128}' | head -1 | tr 'A-Z' 'a-z')"; \
    ACTUAL="$(sha512sum jmeter.tgz | cut -d' ' -f1)"; \
    [ "$EXPECTED" = "$ACTUAL" ] || { echo "checksum mismatch: $ACTUAL != $EXPECTED"; exit 1; }; \
    tar -xzf jmeter.tgz -C /opt/; \
    rm -f jmeter.tgz jmeter.sha512

# [변경 4] 비(非)root 실행
#   부하 테스트를 root 로 돌릴 이유가 없습니다. 컨테이너 탈출 시 피해 범위를 줄입니다.
#   uid 1000 은 Ubuntu 기본 사용자와 맞춰 bind mount 쓰기 권한 문제를 피하기 위한 값입니다.
RUN useradd -u 1000 -m -s /bin/bash jmeter && \
    mkdir -p /jmeter && chown -R jmeter:jmeter /jmeter ${JMETER_HOME}

USER jmeter

# [변경 5] 작업 디렉터리를 버전과 무관한 /jmeter 로 고정
#   기존처럼 WORKDIR 을 /opt/apache-jmeter-5.4.1 로 두면 JMeter 버전을 올릴 때마다
#   Jenkinsfile 의 docker cp 경로까지 전부 고쳐야 합니다.
WORKDIR /jmeter

CMD ["jmeter", "-n"]
