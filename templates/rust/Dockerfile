# Official Rust client on the current stable Rust image: the 2020 rust:1.48 cargo cannot read the crates
# that the unlocked dependencies resolve to today, and those crates keep raising their minimum rustc.
FROM rust:1
COPY client /project
WORKDIR /project
RUN cargo build --release
