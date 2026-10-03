# syntax=docker/dockerfile:1
ARG RUST_TOOLCHAIN=1.98.0
# Rust, proc macros and build scripts execute natively on the ARM64 laptop.
FROM --platform=$BUILDPLATFORM rust:${RUST_TOOLCHAIN}-bookworm AS compiler
RUN test "$(dpkg --print-architecture)" = arm64 \
    && dpkg --add-architecture amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 clang libclang-dev lld protobuf-compiler pkg-config \
        binutils-x86-64-linux-gnu g++-x86-64-linux-gnu \
        libssl-dev:arm64 libssl-dev:amd64 \
    && rm -rf /var/lib/apt/lists/* \
    && rustup target add x86_64-unknown-linux-gnu \
    && ln -s /usr/bin/ld.lld /usr/local/bin/x86_64-linux-gnu-ld.lld
ENV CARGO_NET_GIT_FETCH_WITH_CLI=true \
    CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse \
    CARGO_INCREMENTAL=0 \
    SQLX_OFFLINE=true \
    PROTOC=/usr/bin/protoc \
    CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=x86_64-linux-gnu-gcc \
    CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C link-arg=-fuse-ld=lld" \
    CC_x86_64_unknown_linux_gnu=x86_64-linux-gnu-gcc \
    CXX_x86_64_unknown_linux_gnu=x86_64-linux-gnu-g++ \
    AR_x86_64_unknown_linux_gnu=x86_64-linux-gnu-ar \
    X86_64_UNKNOWN_LINUX_GNU_OPENSSL_LIB_DIR=/usr/lib/x86_64-linux-gnu \
    X86_64_UNKNOWN_LINUX_GNU_OPENSSL_INCLUDE_DIR=/usr/include \
    PKG_CONFIG_LIBDIR_x86_64_unknown_linux_gnu=/usr/lib/x86_64-linux-gnu/pkgconfig \
    PKG_CONFIG_ALLOW_CROSS_x86_64_unknown_linux_gnu=1

FROM compiler AS build
# Rust embeds these absolute paths in fixture and helper-binary references.
WORKDIR /home/runner/work/AppFlowy-CI/AppFlowy-CI
COPY --from=ci-tools /cloud_test_binaries.py /ci-tools/cloud_test_binaries.py
COPY . .
ARG SOURCE_SHA
ARG TEST_FEATURES=ai-test-enabled,sync-v2,ci-test
# Retain complete Cargo outputs locally, including linked test binaries. The
# archive helper strips debug sections from temporary copies before transfer;
# this builder/cache remains complete for reuse. Keep it separate from image
# builds and serialize access to each mount.
RUN --mount=type=cache,id=cloud-tests-target,target=/home/runner/work/AppFlowy-CI/AppFlowy-CI/target,sharing=locked \
    --mount=type=cache,id=cloud-tests-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=cloud-tests-git,target=/usr/local/cargo/git,sharing=locked \
    python3 /ci-tools/cloud_test_binaries.py build \
        --target x86_64-unknown-linux-gnu --source-sha "$SOURCE_SHA" \
        --features "$TEST_FEATURES" --archive /out/cloud-tests.tar.gz

# Unpack the archive with native tools. Decompressing gigabytes through QEMU
# would cost more than the loader checks themselves. This is our own generated
# archive, whose members were confined to target/ by the packaging helper.
FROM compiler AS unpack
RUN --mount=type=bind,from=build,source=/out,target=/archives \
    mkdir /verification \
    && tar -xzf /archives/cloud-tests.tar.gz -C /verification

# Only this lightweight loader check uses QEMU. It lists tests without running
# their service-backed bodies, proving the archive can start on GitHub's ABI.
FROM ubuntu:24.04 AS runtime
RUN test "$(dpkg --print-architecture)" = amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3 git libssl3t64 libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

FROM runtime AS verify
WORKDIR /home/runner/work/AppFlowy-CI/AppFlowy-CI
COPY --from=ci-tools /cloud_test_binaries.py /ci-tools/cloud_test_binaries.py
COPY . .
ARG SOURCE_SHA
RUN --mount=type=bind,from=unpack,source=/verification/target,target=/home/runner/work/AppFlowy-CI/AppFlowy-CI/target \
    python3 /ci-tools/cloud_test_binaries.py verify --source-sha "$SOURCE_SHA" \
    && touch /verification-passed

FROM scratch AS archive
COPY --from=verify /verification-passed /verification-passed
COPY --from=build /out/cloud-tests.tar.gz /cloud-tests.tar.gz
