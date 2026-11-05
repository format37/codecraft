# Official GraalVM native-image client: the 2020 image (oracle/graalvm-ce:20.2.0-java11 on Docker Hub)
# is gone; this is the last Java 11 GraalVM CE from the GraalVM registry, with the same Maven 3.6.3.
FROM ghcr.io/graalvm/graalvm-ce:ol8-java11-22.3.3
RUN gu install native-image \
    && curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.6.3/binaries/apache-maven-3.6.3-bin.tar.gz | tar xz -C /opt \
    && ln -s /opt/apache-maven-3.6.3 /opt/maven
ENV M2_HOME=/opt/maven MAVEN_HOME=/opt/maven
ENV PATH=/opt/maven/bin:${PATH}
COPY client /project
WORKDIR /project
RUN mvn package --batch-mode
