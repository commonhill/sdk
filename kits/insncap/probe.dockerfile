# The instruction-count kit's own test image (tests/test_insncap_kit_docker.py), built with
# project/ as its context: the kit's emulator and plugin, built as a project's
# dockerfile.grader builds them (oxipng-bytes's, whose build stage this is, step for step, so the
# two share Docker's cache), and its sandbox, at /kit, on the Python image the AlgoTune graders use;
# the tests' programs and trees come in at run time.
FROM debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3 AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev make ninja-build pkg-config python3 \
      python3-venv libglib2.0-dev libpng-dev zlib1g-dev xz-utils patch \
 && rm -rf /var/lib/apt/lists/*
ADD --chmod=644 --checksum=sha256:731b5681e4bb18be313231579b8efd0296c5b015fa36dc533874b639ba838016 https://download.qemu.org/qemu-11.1.2.tar.xz /opt/qemu.tar.xz
WORKDIR /opt
COPY grader/native/qemu.patch /opt/
RUN tar xf qemu.tar.xz && rm qemu.tar.xz \
 && cd qemu-11.1.2 \
 && patch -p1 --forward < /opt/qemu.patch \
 && ./configure --target-list=x86_64-linux-user --enable-plugins --without-default-features \
      --disable-docs --disable-werror \
 && ninja -C build qemu-x86_64 \
 && install -m 755 build/qemu-x86_64 /opt/qemu-x86_64
COPY grader/native/ /opt/native/
RUN gcc -O2 -Wall -Werror -shared -fPIC $(pkg-config --cflags glib-2.0) -I/opt/qemu-11.1.2/include/plugins \
      /opt/native/insncap.c -o /opt/insncap.so

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* /var/log/apt /var/log/dpkg.log /var/cache/ldconfig/aux-cache
COPY --from=build /opt/qemu-x86_64 /opt/insncap.so /kit/bin/
COPY grader/sandbox.py /kit/
