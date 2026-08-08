# Derivation for nixpkgs. Destination: pkgs/by-name/hb/hbkit/package.nix
{
  lib,
  stdenv,
  python3Packages,
  fetchPypi,
  lz4,
  makeWrapper,
}:

python3Packages.buildPythonApplication rec {
  pname = "hbkit";
  version = "0.4.3";
  pyproject = true;
  __structuredAttrs = true;

  src = fetchPypi {
    inherit pname version;
    hash = "sha256-J1PSRjqKnE9X2aIHP7CGCRO7qfAvh6kijexMjPCS3WU=";
  };

  build-system = [ python3Packages.hatchling ];

  nativeBuildInputs = [ makeWrapper ];

  dependencies = with python3Packages; [
    textual
    pynacl
    cryptography
  ];

  # hbkit dlopen()s liblz4 by name instead of linking it, and nothing is on a default
  # library search path here, so point it straight at the store path.
  postFixup = ''
    for exe in hbk hbk-tui; do
      wrapProgram "$out/bin/$exe" \
        --set HBK_LZ4 "${lz4.lib}/lib/liblz4${stdenv.hostPlatform.extensions.sharedLibrary}"
    done
  '';

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  pythonImportsCheck = [ "hbkit" ];

  meta = {
    description = "Recover files from Synology Hyper Backup (.hbk) archives";
    longDescription = ''
      hbkit reads Synology Hyper Backup archives directly, without a NAS or any
      Synology software. It restores the original directory tree and mtimes,
      verifies every chunk against the checksums stored in the archive, and can
      read client-side encrypted archives given the password. Includes a
      full-screen browser (hbk-tui) and can work against an archive kept in S3 or
      R2 over an rclone mount.
    '';
    homepage = "https://github.com/YordiLorenzo/hbkit";
    changelog = "https://github.com/YordiLorenzo/hbkit/releases/tag/v${version}";
    license = lib.licenses.mit;
    mainProgram = "hbk";
    maintainers = with lib.maintainers; [ yordilorenzo ];
  };
}
