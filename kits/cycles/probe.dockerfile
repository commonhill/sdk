# The cycles kit's own test image (tests/test_cycles_kit_docker.py), built with
# project/ as its context: the kit's Valgrind, built as a project's dockerfile.grader
# builds it (this stage, step for step, so the two share Docker's cache), and its sandbox, at /kit,
# on the Python image the graders use; the tests' programs come in at run time.
#
# Valgrind is CodSpeed's fork (valgrind-codspeed, release 3.26.0-0codspeed13, commit 346abce5) with
# its Capstone submodule (commit 02257584), both from GitHub's archives pinned by their SHA-256,
# and native/valgrind.patch. Only the two tools the kit runs leave the stage: they are static.
FROM debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3 AS valgrind
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev make autoconf automake perl patch \
 && rm -rf /var/lib/apt/lists/*
ADD --chmod=644 --checksum=sha256:9d1f8f720b7f9e089dbc1a8391d46ee64a6a21c774bfa08b27a0b175d0913f8b https://codeload.github.com/CodSpeedHQ/valgrind-codspeed/tar.gz/346abce500c33cb5e1b43fafb5520929bed8497e /opt/valgrind.tar.gz
ADD --chmod=644 --checksum=sha256:1b0d7efa450e766c120a8dfcf9b539f1e120925fc1478fbe125d6d52bae0c506 https://codeload.github.com/capstone-engine/capstone/tar.gz/022575848782a4801fd150fdbc927effcbca0864 /opt/capstone.tar.gz
WORKDIR /opt
COPY grader/native/valgrind.patch /opt/
RUN mkdir valgrind \
 && tar xzf valgrind.tar.gz -C valgrind --strip-components=1 \
 && tar xzf capstone.tar.gz -C valgrind/third_party/capstone --strip-components=1 \
 && rm valgrind.tar.gz capstone.tar.gz \
 && cd valgrind \
 && patch -p1 --forward < /opt/valgrind.patch \
 && ./autogen.sh \
 && ./configure --enable-only64bit \
 && make include/vgversion.h \
 && for dir in third_party include VEX coregrind cachegrind massif; do make -C $dir -j"$(nproc)" || exit 1; done \
 && mkdir /opt/tools \
 && cp cachegrind/cachegrind-amd64-linux massif/massif-amd64-linux /opt/tools/

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=valgrind /opt/tools/ /kit/bin/valgrind/
COPY grader/sandbox.py /kit/
