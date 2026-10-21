#!/usr/bin/env bash
# ABOUTME: Prints the Homebrew formula for a slarc release, given its version and the sdist's sha256.
# ABOUTME: The release workflow and setup-homebrew-tap.sh both write Formula/slarc.rb with it.
set -euo pipefail

if [ $# -ne 2 ]; then
    echo "Usage: $0 VERSION SHA256" >&2
    exit 2
fi
version="$1"
sha256="$2"

cat << FORMULA
class Slarc < Formula
  desc "Archive Slack conversations you can see into searchable, re-renderable files"
  homepage "https://github.com/tednaleid/slarc"
  url "https://github.com/tednaleid/slarc/releases/download/v${version}/slarc-${version}.tar.gz"
  sha256 "${sha256}"
  license "MIT"

  depends_on "uv" => :build
  depends_on "python@3.15"

  # Installs the dependency versions locked in the sdist's uv.lock, verified by hash.
  def install
    ENV["UV_PYTHON_DOWNLOADS"] = "never"
    python = libexec/"bin/python"
    system "uv", "venv", libexec, "--python", formula_opt_bin("python@3.15")/"python3.15"
    system "uv", "export", "--frozen", "--no-dev", "--no-emit-project",
           "--output-file", "requirements.txt"
    system "uv", "pip", "install", "--python", python, "--require-hashes",
           "--requirement", "requirements.txt"
    system "uv", "pip", "install", "--python", python, "--no-deps", "."
    bin.install_symlink libexec/"bin/slarc"
  end

  test do
    assert_match "slarc #{version}", shell_output("#{bin}/slarc --version")
  end
end
FORMULA
