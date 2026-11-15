# A pinned download whose ADD runs over two lines (enwik-fresh's worker fetches zig so): the
# harness joins the continuation and warms the download before a build.
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
ADD --chmod=644 --checksum=sha256:1cbe9df9f27e6b78d14ccbca43b6703a404ef79ef1c463de901d7f088d4e2026 \
    https://ziglang.org/download/0.17.0/zig-x86_64-linux-0.17.0.tar.xz /opt/zig.tar.xz
