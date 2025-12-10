FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV RUST_TOOLCHAIN=1.85.0
ENV FLUTTER_VERSION=3.27.4
ENV CARGO_MAKE_VERSION=0.37.18

# Install system prerequisites
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    git \
    unzip \
    xz-utils \
    zip \
    libglu1-mesa \
    build-essential \
    libssl-dev \
    clang \
    cmake \
    ninja-build \
    pkg-config \
    libgtk-3-dev \
    keybinder-3.0 \
    libnotify-dev \
    network-manager \
    libcurl4-openssl-dev \
    libmpv-dev \
    mpv \
    libasound2-dev \
    libunwind-dev \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Install Rust
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain ${RUST_TOOLCHAIN} \
    && chmod -R a+w $RUSTUP_HOME $CARGO_HOME

# Install cargo-make and duckscript_cli
RUN cargo install cargo-make --version ${CARGO_MAKE_VERSION} \
    && cargo install duckscript_cli

# Install Flutter
ENV FLUTTER_HOME=/usr/local/flutter \
    PATH=/usr/local/flutter/bin:$PATH

RUN git clone --depth 1 --branch ${FLUTTER_VERSION} https://github.com/flutter/flutter.git ${FLUTTER_HOME} \
    && git config --global --add safe.directory ${FLUTTER_HOME} \
    && flutter doctor

# Configure git for permissions issues often seen in CI
RUN git config --global --add safe.directory *
